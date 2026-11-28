"""Local web app: browse each day's transcript and play the audio behind any line.

Serves on 127.0.0.1 only. The data is private, so requests are also checked
for a local Host header, which stops other websites reaching it through DNS
tricks in the browser.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import asyncio

from . import __version__
from . import ask as ask_mod
from . import client as client_mod
from . import config
from . import digest as digest_mod
from . import hosted as hosted_mod
from . import upkeep

_INDEX = Path(__file__).parent / "web" / "index.html"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_SEARCH_LIMIT = 200
_MEANING_LIMIT = 8  # moments offered as "close in meaning" under the exact matches
_CONTEXT_MS = 60_000  # a neighbouring line this close counts as context for a search hit
_MAX_UPLOAD = 50 * 1024**3
BRAIN_EVERY_S = 30 * 60  # how often a background sync also exports to the second brain
# Imported lines can sit a second or two after their audio; start playback early.
_IMPORT_LEAD_MS = 1500


def _day_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _utterance(row) -> dict:
    uid, start_ms, end_ms, text, segment, segment_start, speaker_id, speaker, label, retimed, imported = row
    if speaker_id:
        who = {"id": speaker_id, "name": speaker, "line": uid}
    elif label:  # a numbered speaker: the number only holds within its conversation
        who = {"id": None, "name": f"Speaker {label}", "line": uid}
    else:
        who = None
    has_audio = bool(segment) and segment_start is not None
    return {
        "speaker": who,
        "id": uid,
        "start": start_ms,
        "end": end_ms,
        "text": text,
        "audio": "/" + segment if has_audio else None,
        # Imported lines that have not been re-timed run late, so start those early.
        "offset": max(0, start_ms - segment_start - (_IMPORT_LEAD_MS if imported and retimed is None else 0))
        if has_audio else 0,
    }


_SELECT = (
    "SELECT u.id, u.start_ms, u.end_ms, u.text, u.segment, s.start_ms, u.speaker_id, p.name, "
    "u.source_speaker, u.orig_start_ms, u.source IS NOT NULL "
    "FROM utterances u LEFT JOIN segments s ON s.path = u.segment "
    "LEFT JOIN speakers p ON p.id = u.speaker_id "
)


def pendant_advice(message: str | None) -> str | None:
    """What to try, in plain words, for the ways reaching the pendant usually fails."""
    text = (message or "").lower()
    if not text:
        return None
    if "not authorized" in text or "privacy settings" in text:
        return ("This program is not allowed to use Bluetooth. On a Mac, open System Settings, "
                "Privacy & Security, Bluetooth, and switch on the app you started Limitful from "
                "(Terminal, iTerm or similar), then quit and reopen that app. On Windows, open "
                "Settings, Privacy & security, and check that apps may use Bluetooth.")
    if "turned off" in text:
        return "Bluetooth is off on this computer. Switch it on and try again."
    if "unsupported" in text:
        return "This computer has no Bluetooth that Limitful can use."
    if "not paired" in text:
        return "This computer has not been paired with the pendant yet, or the pendant was reset since."
    if "dropping the connection" in text:
        return ("Either something else is connected to the pendant, or this computer is holding an old "
                "pairing the pendant has since forgotten. For the first, turn off Bluetooth on your phone. "
                "For the second, remove Pendant under Bluetooth in System Settings (on Windows: Settings, "
                "Bluetooth & devices), turn Bluetooth off and on, then pair again.")
    if "not in range" in text or "is busy" in text:
        return ("The pendant only answers when nothing else is connected to it. Turn off Bluetooth on "
                "your phone (or quit the Limitless app), check the pendant is switched on and within a "
                "few metres, and try again. Holding its button for two seconds switches it on or off.")
    return None


class Library:
    """Everything the Lifelog page can ask for: the stored days, and the jobs and settings it can change."""

    def __init__(self, data_dir: Path, link_speakers: bool = False) -> None:
        self.data_dir = Path(data_dir).resolve()
        self._link_speakers = link_speakers
        self._lock = threading.Lock()
        # What the server is busy with, shown in the Lifelog.
        # `percent` is set while a step can say how far through it is; otherwise done of total, or neither.
        self.job = {"running": False, "what": "", "done": 0, "total": 0, "error": None, "note": None,
                    "percent": None}
        # One Bluetooth conversation with the pendant at a time.
        self._radio = threading.Lock()
        self._pendant: dict = {"checked": None, "error": None}
        self.port = 0
        # Syncing by itself: how often (0 is off), and how the last attempt went.
        self.auto: dict = {"every": 0, "last": None, "found": None}
        self._auto_thread: threading.Thread | None = None

    def import_export(self, stream, length: int) -> dict:
        """Save an uploaded export zip, import it, then start linking speakers."""
        from .importer import import_limitless

        with self._lock:
            if self.job["running"]:
                raise RuntimeError("Something is already running. Wait for it to finish.")
            self.job.update(running=True, what="Importing the export", done=0, total=0, error=None, note=None)
        upload = self.data_dir / "imports" / f"upload-{int(time.time())}.zip"
        try:
            upload.parent.mkdir(parents=True, exist_ok=True)
            with open(upload, "wb") as out:
                left = length
                while left > 0:
                    chunk = stream.read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError("the upload was cut short")
                    out.write(chunk)
                    left -= len(chunk)
            result = self._with_store(
                lambda store: import_limitless(store, upload, log=lambda message: None))
        except Exception:
            self.job.update(running=False, what="")
            raise
        finally:
            upload.unlink(missing_ok=True)
        indexing = False
        if self._link_speakers and result.lines:
            threading.Thread(target=self._link_job, daemon=True).start()
        else:
            self.job.update(running=False, what="")
            indexing = bool(result.lines) and self._index_new_lines()
        return {
            "conversations": result.conversations, "lines": result.lines,
            "recordings": result.audio_files, "hours": round(result.audio_hours, 1),
            "reminders": result.reminders, "skipped": result.skipped_existing,
            "indexing": indexing,
        }

    def add_recording(self, stream, length: int, name: str, modified_ms: int | None) -> dict:
        """Save an uploaded audio file as a recording. It is transcribed by `start_transcribing`."""
        from .recordings import add_recording

        upload = self.data_dir / "imports" / f"upload-{time.time_ns()}{Path(name).suffix.lower()}"
        try:
            upload.parent.mkdir(parents=True, exist_ok=True)
            with open(upload, "wb") as out:
                left = length
                while left > 0:
                    chunk = stream.read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError("the upload was cut short")
                    out.write(chunk)
                    left -= len(chunk)
            added = self._with_store(lambda store: add_recording(store, upload, name, modified_ms))
        finally:
            upload.unlink(missing_ok=True)
        day = datetime.fromtimestamp(added.start_ms / 1000).strftime("%Y-%m-%d") if added.new else None
        return {"added": added.new, "day": day, "start_ms": added.start_ms,
                "duration_ms": added.duration_ms, "dated_by": added.dated_by}

    def start_transcribing(self) -> dict:
        """Transcribe and summarise whatever is waiting, in the background. No pendant is needed."""
        waiting = self._with_store(lambda store: len(store.pending_transcription()))
        if not waiting:
            return {"waiting": 0}
        with self._lock:
            if self.job["running"]:
                raise RuntimeError("Something is already running. Wait for it to finish.")
            self.job.update(running=True, what="Transcribing", done=0, total=0, error=None, note=None)
        threading.Thread(target=self._transcribe_job, daemon=True).start()
        return {"waiting": waiting}

    def _transcribe_waiting(self, store) -> tuple[int, int]:
        """Transcribe what is waiting, then summarise if a model is chosen. Returns (recordings, lines)."""
        waiting = len(store.pending_transcription())
        self.job.update(what="Transcribing", done=0, total=0, percent=None)
        transcriber = self._transcriber()
        self.job.update(done=0, total=waiting)
        try:
            lines, _files = store.transcribe_pending(
                transcriber,
                lambda done, total: self.job.update(done=done, total=total),
                on_audio=lambda ms, of: self.job.update(percent=min(99, ms * 100 // of)),
                on_indexing=lambda done, total: self.job.update(
                    what="Adding the new lines to search", done=done, total=total, percent=None))
            provider = config.provider()
            if provider:
                self.job.update(what="Summarising conversations", done=0, total=0, percent=None)
                store.digest_pending(
                    digest_mod.make_summarizer(provider, None, config.api_key()),
                    lambda done, total: self.job.update(done=done, total=total))
        finally:
            self.job.update(percent=None)
        return waiting, lines

    def _transcribe_job(self) -> None:
        from .store import Store

        try:
            store = Store(self.data_dir)
            try:
                try:
                    waiting, lines = self._transcribe_waiting(store)
                except digest_mod.DigestError as err:
                    self.job["note"] = f"Transcribed, but summaries are waiting: {err}."
                else:
                    self.job["note"] = (f"Transcribed {waiting} recording{'' if waiting == 1 else 's'}: "
                                        f"{lines} line{'' if lines == 1 else 's'} of speech.")
            finally:
                store.close()
        except Exception as err:  # shown in the page; the audio is kept and can be tried again
            self.job["error"] = f"Transcribing stopped: {err}"
            self.job["note"] = None
        finally:
            self.job.update(running=False, what="")

    # --- recordings made elsewhere, and Voice Memos on this Mac -------------------------

    def recordings(self) -> dict:
        from . import voicememos
        from .recordings import added_recordings

        enabled, since = config.voice_memos()
        state = (self._with_store(lambda store: voicememos.status(store=store)) if voicememos.supported()
                 else {"state": "unsupported", "memos": 0})
        return {
            "recordings": self._with_store(added_recordings),
            "voice_memos": dict(state, enabled=enabled, since=since, program=voicememos.program()),
        }

    def forget(self, spans: list[tuple[int, int]]) -> dict:
        """Delete what was said in each stretch, and its audio. Not while a job is rewriting lines."""
        from .forget import forget_all

        with self._lock:
            if self.job["running"]:
                raise RuntimeError("Something is already running. Wait for it to finish.")
        gone = self._with_store(lambda store: forget_all(store, spans))
        return {"lines": gone.lines, "conversations": gone.conversations,
                "recordings_removed": gone.recordings_removed, "recordings_silenced": gone.recordings_silenced}

    def remove_recording(self, path: str) -> dict:
        from .recordings import remove_recording

        self._with_store(lambda store: remove_recording(store, path))
        return self.recordings()

    def move_recording(self, path: str, start_ms: int) -> dict:
        from .recordings import move_recording

        self._with_store(lambda store: move_recording(store, path, start_ms))
        return self.recordings()

    def set_voice_memos(self, enabled: bool, older: bool = False) -> dict:
        """Turn bringing in Voice Memos on or off. Unless `older`, only memos recorded from now on."""
        was, since = config.voice_memos()
        if enabled and older:
            since = 0
        elif enabled and not was:
            since = int(time.time() * 1000)
        config.update(voice_memos=enabled, voice_memos_since=since)
        return self.recordings()

    def _voice_memos_step(self) -> str | None:
        """Add new Voice Memos and transcribe them. Returns a note for the page, if any arrived."""
        from . import voicememos
        from .store import Store

        enabled, since = config.voice_memos()
        if not enabled or not voicememos.supported():
            return None
        store = Store(self.data_dir)
        try:
            try:
                found = voicememos.sync(store, since, on_progress=lambda done, total: self.job.update(
                    what="Bringing in Voice Memos", done=done, total=total))
            except (voicememos.NoAccess, voicememos.NoLibrary):
                return None  # Settings says why; a sync should not fail over it
            if not found.added:
                return None
            self.job.update(what="Transcribing Voice Memos", done=0, total=0)
            _waiting, lines = self._transcribe_waiting(store)
        finally:
            store.close()
        return (f"Added {found.added} Voice Memo{'' if found.added == 1 else 's'}: "
                f"{lines} line{'' if lines == 1 else 's'} of speech.")

    def _index_new_lines(self) -> bool:
        """Start adding new lines to search by meaning, if the user has set it up.

        A big import can take minutes to index, so it runs as the usual background
        job, with progress and a Stop button, and not inside the import itself.
        """
        from . import search as search_mod

        if not search_mod.index_for(self.data_dir).exists():
            return False
        try:
            upkeep.start_index(self)
        except RuntimeError:
            return False  # something else took the job slot; Settings still offers "Index the rest"
        return True

    # --- first run -------------------------------------------------------------------

    def welcome(self) -> dict:
        """What the setup guide needs to know to pick where to start."""
        from . import demo, speech_model

        db = self._db()
        try:
            empty = db.execute("SELECT 1 FROM utterances LIMIT 1").fetchone() is None
        finally:
            db.close()
        return {"empty": empty, "demo": demo.present(self.data_dir),
                "model_ready": speech_model.ready(), "provider": config.provider()}

    def demo(self, action: str) -> dict:
        from . import demo

        if action == "load":
            demo.build(self.data_dir)
        elif action == "remove":
            demo.remove(self.data_dir)
        else:
            raise ValueError("unknown action")
        return self.welcome()

    def _transcriber(self):
        """The chosen transcriber, reporting the speech model's download if it has to be fetched."""
        from . import speech_model
        from .transcribe import DEFAULT_MODEL, make_transcriber

        engine = config.transcriber()
        if engine != "local" or speech_model.ready():
            return make_transcriber(engine)
        megabytes = 1024 * 1024
        total = speech_model.KNOWN_BYTES.get(DEFAULT_MODEL, 0) // megabytes
        self.job.update(what="Downloading the speech model (first time only), megabytes", done=0, total=total)
        finished = threading.Event()

        def report():
            while not finished.wait(1.0):
                self.job.update(done=min(speech_model.downloaded_bytes() // megabytes, total or 10**9))

        threading.Thread(target=report, daemon=True).start()
        try:
            return make_transcriber(engine)
        finally:
            finished.set()
            self.job.update(what="Transcribing", done=0, total=0)

    # --- the pendant -----------------------------------------------------------------

    def pendant(self) -> dict:
        """What was last learned about the pendant, without touching the radio."""
        return dict(self._pendant, brightness=config.load().get("led_brightness"),
                    advice=pendant_advice(self._pendant.get("error")),
                    paused_since=self._with_store(lambda store: store.paused_since()))

    def read_usb(self) -> dict:
        """Read the pendant over its USB cable, if it is plugged in. Read-only and quick."""
        from . import usb

        try:
            wired = usb.read()
        except usb.UsbError as err:
            return dict(self.pendant(), usb={"error": str(err)})
        if wired and "brightness" in wired:
            config.update(led_brightness=wired["brightness"])  # the true value, not our last guess
        return dict(self.pendant(), usb=wired)

    def forget_over_usb(self) -> dict:
        """Tell a plugged-in pendant to forget its old computers, so it can pair here unreset."""
        from . import usb

        try:
            if not usb.forget():
                raise ValueError("No pendant is plugged in. Connect it to this computer with its "
                                 "USB cable, and use a cable that carries data, not only power.")
        except usb.UsbError as err:
            raise ValueError(f"Could not reach the pendant over USB: {err}") from None
        self._pendant = {"checked": None, "error": None}
        return dict(self.pendant(), forgotten=True)

    def set_paused(self, paused: bool) -> dict:
        self._with_store(lambda store: store.pause() if paused else store.resume())
        return self.pendant()

    def unpair_pendant(self) -> dict:
        def unpair():
            try:
                asyncio.run(client_mod.unpair())
            except (client_mod.PendantError, client_mod.BleakError, asyncio.TimeoutError, OSError) as err:
                raise ValueError(f"Could not unpair: {err}") from None
            self._pendant = {"checked": None, "error": None}
            return self.pendant()

        return self._with_radio(unpair)

    def start_pairing(self) -> None:
        """Wait, in the background, for the pendant to be put in pairing mode."""
        with self._lock:
            if self.job["running"]:
                raise RuntimeError("Something is already running. Wait for it to finish.")
            self.job.update(running=True, what="Waiting for the pendant in pairing mode",
                            done=0, total=0, error=None, note=None)
        threading.Thread(target=self._pair_job, daemon=True).start()

    def _pair_job(self) -> None:
        try:
            if not self._radio.acquire(timeout=20):
                raise RuntimeError("the pendant is busy")
            try:
                state = asyncio.run(client_mod.pair(timeout=180, log=lambda message: None))
            finally:
                self._radio.release()
            if state is None:
                raise RuntimeError("the pendant never accepted this computer. Check it showed "
                                   "a blue light, and that you accepted the connection request")
            # A check made before pairing will have failed with "not paired"; that is over now.
            self._pendant = {"checked": None, "error": None}
            self.job["note"] = "Paired. The pendant accepted this computer."
        except Exception as err:  # shown in the page
            self.job["error"] = f"Pairing stopped: {err}"
        finally:
            self.job.update(running=False, what="")

    def _with_radio(self, action):
        if not self._radio.acquire(blocking=False):
            raise RuntimeError("Already talking to the pendant. Try again in a moment.")
        try:
            return action()
        finally:
            self._radio.release()

    def check_pendant(self) -> dict:
        def check():
            try:
                state = asyncio.run(client_mod.read_state())
            except (client_mod.PendantError, client_mod.BleakError, asyncio.TimeoutError, OSError) as err:
                self._pendant = {"checked": int(time.time() * 1000), "error": str(err) or type(err).__name__}
            else:
                self._pendant = dict(state, checked=int(time.time() * 1000), error=None)
            return self.pendant()

        return self._with_radio(check)

    def set_brightness(self, percent) -> dict:
        if not isinstance(percent, int) or isinstance(percent, bool) or not 0 <= percent <= 100:
            raise ValueError("Brightness is a whole number from 0 to 100.")

        def apply():
            try:
                asyncio.run(client_mod.set_brightness(percent))
            except (client_mod.PendantError, client_mod.BleakError, asyncio.TimeoutError, OSError) as err:
                raise ValueError(f"Could not reach the pendant: {err}") from None
            config.update(led_brightness=percent)
            return self.pendant()

        return self._with_radio(apply)

    def start_sync(self, quiet: bool = False, memos_only: bool = False) -> None:
        """Pull new audio off the pendant, then transcribe and summarise it, in the background.

        `quiet` is for syncing by itself: a pendant that is away is normal, not an error.
        """
        with self._lock:
            if self.job["running"]:
                raise RuntimeError("Something is already running. Wait for it to finish.")
            self.job.update(running=True, what="Checking Voice Memos" if memos_only else "Syncing the pendant",
                            done=0, total=0, error=None, note=None)
        threading.Thread(target=self._sync_job, args=(quiet, memos_only), daemon=True).start()

    def set_auto_sync(self, every: float) -> None:
        """Sync by itself every `every` seconds; 0 stops it."""
        self.auto["every"] = max(0.0, float(every))
        with self._lock:  # two requests at once must not start two loops
            if self.auto["every"] and self._auto_thread is None:
                self._auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
                self._auto_thread.start()

    def _auto_loop(self) -> None:
        waited = 0.0
        while True:
            time.sleep(1)
            waited += 1
            every = self.auto["every"]
            if not every or waited < every:
                continue
            waited = 0.0
            try:
                self.start_sync(quiet=True)
            except RuntimeError:
                pass  # something else is running; try again next time

    def autostart(self) -> dict:
        from . import autostart

        return dict(autostart.status(), syncing_every=self.auto["every"], last=self.auto["last"],
                    found=self.auto["found"])

    def set_autostart(self, enabled: bool) -> dict:
        """Add or remove the login item. This running app starts or stops syncing by itself to match."""
        from . import autostart

        try:
            if enabled:
                # Written now, started at the next login: this app already holds the port.
                autostart.enable(self.data_dir, self.port or 4747, start_now=False)
                self.set_auto_sync(autostart.SYNC_EVERY_S)
            else:
                self.set_auto_sync(0)
                # If the login item is what is running this app, removing it stops us:
                # answer the page first.
                threading.Timer(0.5, autostart.disable).start()
                return dict(self.autostart(), enabled=False)
        except autostart.AutostartError as err:
            raise ValueError(str(err)) from None
        return self.autostart()

    def _sync_job(self, quiet: bool = False, memos_only: bool = False) -> None:
        """The pendant first, then Voice Memos if they are turned on. One failing does not stop the other."""
        try:
            try:
                if not memos_only:
                    self._pendant_sync(quiet)
            except Exception as err:  # shown in the page
                self.job["error"] = f"Sync stopped: {err}"
                self.job["note"] = None
            try:
                memos = self._voice_memos_step()
            except Exception as err:
                if not self.job["error"]:
                    self.job["error"] = f"Voice Memos stopped: {err}"
            else:
                self.job.update(percent=None)
                if memos and not self.job["error"]:
                    self.job["note"] = f"{self.job['note']} {memos}" if self.job["note"] else memos
                elif memos_only and not memos:
                    self.job["note"] = "No new Voice Memos."
            self._brain_step()
        finally:
            self.job.update(running=False, what="")

    def _brain_step(self) -> None:
        """Export to the second brain, if it is set up, at most every BRAIN_EVERY_S. Never fails the sync."""
        from . import brain
        from .store import Store

        now = time.monotonic()
        if now - getattr(self, "_brain_last", -BRAIN_EVERY_S) < BRAIN_EVERY_S:
            return
        saved = brain.settings()
        if saved is None:
            return
        self._brain_last = now
        try:
            store = Store(self.data_dir)
            try:
                result = brain.export(store, saved)
            finally:
                store.close()
            print(f"Second brain: {len(result.days)} days, {result.audio_added} recordings encrypted, "
                  + ("pushed." if result.pushed else "committed." if result.committed else "no change."), flush=True)
        except Exception as err:  # retried at the next sync
            print(f"Second brain export failed: {err}", flush=True)

    def _pendant_sync(self, quiet: bool = False) -> None:
        from . import watch as watch_mod
        from .store import Store

        try:
            if not self._radio.acquire(timeout=20):
                raise RuntimeError("the pendant is busy")
            try:
                provider = config.provider()
                summarizer = (digest_mod.make_summarizer(provider, None, config.api_key())
                              if provider else None)
                store = Store(self.data_dir)
                try:
                    result = asyncio.run(watch_mod.run_cycle(
                        store, ack=True,
                        get_transcriber=self._transcriber,
                        get_embedder=None, summarizer=summarizer,  # voice labels are opt-in: see docs/experiments.md
                        on_stage=lambda stage: self.job.update(what=stage, done=0, total=0, percent=None),
                        on_audio=lambda ms, of: self.job.update(percent=min(99, ms * 100 // of)),
                        on_indexing=lambda done, total: self.job.update(
                            what="Adding the new lines to search", done=done, total=total, percent=None)))
                finally:
                    store.close()
            finally:
                self._radio.release()
            if result.busy:
                if quiet:
                    return
                raise RuntimeError("another copy of Limitful is syncing the pendant. "
                                   "Close it, or let it finish")
            self.auto.update(last=int(time.time() * 1000), found=result.found)
            if not result.found:
                if quiet:
                    return
                raise RuntimeError("the pendant is not in range, is switched off, "
                                   "or is connected to another device")
            if result.error:
                raise RuntimeError(result.error)
            if quiet and not result.pages_new:
                return
            self.job["note"] = (f"Synced {result.pages_new} new pages: {result.audio_files} recordings, "
                                f"{result.lines} lines of speech."
                                + (f" Deleted {result.pages_discarded} pages recorded while paused."
                                   if result.pages_discarded else ""))
        except Exception:
            self.job["note"] = None
            raise

    def settings(self) -> dict:
        """What the Settings page shows. The key itself never leaves the server."""
        key = config.api_key()
        return {
            "version": __version__,
            "provider": config.load().get("provider") or None,
            "has_key": bool(key),
            "key_hint": self._hint(key),
            "claude_model": config.claude_model(),
            "claude_models": list(config.CLAUDE_MODELS),
            "ollama_model": digest_mod.OLLAMA_MODEL,
            "transcriber": config.load().get("transcriber") or "local",
            "has_deepgram_key": bool(config.deepgram_api_key()),
            "deepgram_key_hint": self._hint(config.deepgram_api_key()),
            "deepgram_model": hosted_mod.DEEPGRAM_MODEL,
        }

    @staticmethod
    def _hint(key: str | None) -> str | None:
        """Enough to recognise a saved key by, never the key itself."""
        if not key:
            return None
        return f"ends in {key[-4:]}" if len(key) >= 12 else "saved"

    def _save_transcription(self, body: dict) -> None:
        engine = body.get("transcriber", config.load().get("transcriber") or "local")
        key = body.get("deepgram_api_key")
        if key is not None and not isinstance(key, str):
            raise ValueError("the key must be text")
        if engine not in config.TRANSCRIBERS:
            raise ValueError("choose local transcription or Deepgram")
        candidate = key.strip() if key else (config.deepgram_api_key() if key is None else None)
        if engine == "deepgram":
            if not candidate:
                raise ValueError("Deepgram needs an API key. Nothing was saved.")
            # Check before saving, so a mistyped key is caught here and not at the next sync.
            try:
                hosted_mod.check_deepgram(candidate)
            except hosted_mod.HostedError as err:
                raise ValueError(f"{err}. Nothing was saved.") from None
        if key is None:
            config.update(transcriber=engine)
        else:
            config.update(transcriber=engine, deepgram_api_key=key)

    def save_settings(self, body: dict) -> dict:
        if "transcriber" in body or "deepgram_api_key" in body:
            self._save_transcription(body)
            if "provider" not in body and "api_key" not in body:
                return self.settings()
        model = body.get("claude_model", config.claude_model())
        if model not in config.CLAUDE_MODELS:
            raise ValueError(f"choose one of {', '.join(config.CLAUDE_MODELS)}")
        provider = body.get("provider", config.load().get("provider"))
        if provider in ("", "none"):
            provider = None
        key = body.get("api_key")
        if key is not None and not isinstance(key, str):
            raise ValueError("the key must be text")
        if provider is not None and provider not in config.PROVIDERS:
            raise ValueError("choose Claude, Ollama or no summaries")
        if provider == "claude":
            # Check before saving, so a mistyped key is caught here and not hours later.
            candidate = key.strip() if key else (config.api_key() if key is None else None)
            try:
                digest_mod.check_claude(candidate or None, model)
            except digest_mod.DigestError as err:
                raise ValueError(f"{err}. Nothing was saved.") from None
        if provider == "ollama":
            try:  # the same early check: is it running, and does it have the model
                digest_mod.check_ollama()
            except digest_mod.DigestError as err:
                raise ValueError(f"{err}. Nothing was saved.") from None
        chosen = {"claude_model": model} if "claude_model" in body else {}
        if key is None:
            config.update(provider=provider, **chosen)
        else:
            config.update(provider=provider, api_key=key, **chosen)
        return self.settings()

    def start_digest(self) -> None:
        """Summarise new conversations in the background."""
        provider = config.provider()
        if not provider:
            raise ValueError("Choose what writes summaries first, under Settings.")
        with self._lock:
            if self.job["running"]:
                raise RuntimeError("Something is already running. Wait for it to finish.")
            self.job.update(running=True, what="Summarising conversations", done=0, total=0, error=None, note=None)
        threading.Thread(target=self._digest_job, args=(provider,), daemon=True).start()

    def _digest_job(self, provider: str) -> None:
        from .store import Store

        try:
            summarizer = digest_mod.make_summarizer(provider, None, config.api_key())
            store = Store(self.data_dir)
            try:
                store.digest_pending(
                    summarizer, lambda done, total: self.job.update(done=done, total=total))
            finally:
                store.close()
        except Exception as err:  # shown in the page; whatever was summarised is kept
            self.job["error"] = f"Summaries stopped: {err}"
        finally:
            self.job.update(running=False, what="")

    def _link_job(self) -> None:
        """Recognise the voices in imported conversations. Slow, so it runs in the background."""
        from .speakers import TitanetEmbedder, load_audio
        from .store import Store

        self.job.update(running=True, what="Recognising speakers", done=0, total=0, error=None, note=None)
        try:
            store = Store(self.data_dir)
            try:
                store.link_imported_speakers(
                    TitanetEmbedder(), load_audio,
                    lambda done, total: self.job.update(done=done, total=total))
            finally:
                store.close()
        except Exception as err:  # report it in the page; the import itself is already saved
            self.job["error"] = f"Could not recognise speakers: {err}"
        finally:
            self.job.update(running=False, what="")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect((self.data_dir / "pendant.db").as_uri() + "?mode=ro", uri=True)

    def days(self) -> list[dict]:
        db = self._db()
        try:
            counts = Counter(_day_of(ms) for (ms,) in db.execute("SELECT start_ms FROM utterances"))
            talks = Counter(_day_of(ms) for (ms,) in db.execute(
                "SELECT start_ms FROM conversations WHERE status = 'done'"))
            headlines = dict(db.execute("SELECT day, headline FROM day_recaps"))
        except sqlite3.OperationalError:
            counts, talks, headlines = Counter(), Counter(), {}  # no recordings yet
        finally:
            db.close()
        # Enough for a calendar: how much was said, in how many conversations, and the recap's headline.
        return [{"day": day, "lines": n, "talks": talks.get(day, 0), "headline": headlines.get(day)}
                for day, n in sorted(counts.items())]

    def day(self, day: str) -> list[dict]:
        start = datetime.strptime(day, "%Y-%m-%d")
        lo = int(start.timestamp() * 1000)
        hi = int((start + timedelta(days=1)).timestamp() * 1000)
        db = self._db()
        try:
            rows = db.execute(
                _SELECT + "WHERE u.start_ms >= ? AND u.start_ms < ? ORDER BY u.start_ms, u.id",
                (lo, hi),
            ).fetchall()
        finally:
            db.close()
        return [_utterance(r) for r in rows]

    def search(self, query: str, by: str | None = None) -> list[dict]:
        """Lines containing `query`, newest first. `by` keeps only what that named person said;
        with no query it lists everything they said."""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        said_by, who = ("AND p.name = ? COLLATE NOCASE ", (by,)) if by else ("", ())
        db = self._db()
        try:
            rows = db.execute(
                _SELECT + "WHERE u.text LIKE ? ESCAPE '\\' " + said_by + "ORDER BY u.start_ms DESC LIMIT ?",
                (f"%{escaped}%", *who, _SEARCH_LIMIT),
            ).fetchall()
            out = []
            for r in rows:
                hit = dict(_utterance(r), day=_day_of(r[1]))
                # What was said just before and after, and where, so a hit can be judged without opening it.
                before = db.execute(
                    "SELECT text FROM utterances WHERE start_ms < ? AND start_ms >= ? ORDER BY start_ms DESC, id DESC LIMIT 1",
                    (r[1], r[1] - _CONTEXT_MS)).fetchone()
                after = db.execute(
                    "SELECT text FROM utterances WHERE start_ms > ? AND start_ms <= ? ORDER BY start_ms, id LIMIT 1",
                    (r[1], r[1] + _CONTEXT_MS)).fetchone()
                talk = db.execute(
                    "SELECT title FROM conversations WHERE status = 'done' AND start_ms <= ? AND end_ms >= ? LIMIT 1",
                    (r[1], r[1])).fetchone()
                hit.update(before=before[0] if before else None, after=after[0] if after else None,
                           talk=talk[0] if talk else None)
                out.append(hit)
        finally:
            db.close()
        return out

    def search_meaning(self, query: str, by: str | None = None) -> dict:
        """Lines close in meaning to `query`, best first, once the index has been built.
        `by` keeps only moments where that named person said the matching line.

        Served apart from the exact-word search so those results never wait for,
        or change because of, this one; the page lists these underneath.
        """
        from . import search as search_mod

        index = search_mod.index_for(self.data_dir)
        if not index.exists():
            return {"ready": False, "lines": []}
        db = self._db()
        try:
            start_of = dict(db.execute("SELECT id, start_ms FROM utterances"))
            # No cut-off by score: measured on real transcripts, right answers (0.57 to
            # 0.79) and unrelated lines (median 0.68) score alike, so a floor would
            # hide as many good hits as bad. The page labels these as near, not exact.
            only = None
            if by:
                only = {uid for (uid,) in db.execute(
                    "SELECT u.id FROM utterances u JOIN speakers p ON p.id = u.speaker_id "
                    "WHERE p.name = ? COLLATE NOCASE", (by,))}
            hits = search_mod.spread(index.search(search_mod.make_embedder(), query, limit=200, only=only),
                                     start_of, _MEANING_LIMIT, apart_ms=search_mod.NEIGHBOUR_GAP_MS)
            # A hit is a line that matched together with its neighbours, and on its own
            # it can be a bare "Yeah.", so each is shown as that moment: the same few lines.
            lines, seen = [], set()  # a line two moments share is listed with the closer one
            for moment, (uid, _score) in enumerate(hits):
                at = start_of[uid]
                reach = search_mod.NEIGHBOUR_GAP_MS
                before = db.execute(
                    _SELECT + "WHERE u.start_ms <= ? AND u.start_ms > ? AND u.id != ? "
                    "ORDER BY u.start_ms DESC, u.id DESC LIMIT ?", (at, at - reach, uid, search_mod.NEIGHBOURS)).fetchall()
                after = db.execute(
                    _SELECT + "WHERE u.start_ms >= ? AND u.start_ms < ? AND u.id != ? "
                    "ORDER BY u.start_ms, u.id LIMIT ?", (at, at + reach, uid, search_mod.NEIGHBOURS)).fetchall()
                here = db.execute(_SELECT + "WHERE u.id = ?", (uid,)).fetchall()
                for row in sorted([*before, *here, *after], key=lambda r: (r[1], r[0])):
                    if row[0] not in seen:
                        seen.add(row[0])
                        lines.append(dict(_utterance(row), day=_day_of(row[1]), moment=moment))
        finally:
            db.close()
        return {"ready": True, "lines": lines}

    def _with_store(self, action):
        from .store import Store

        store = Store(self.data_dir)
        try:
            return action(store)
        finally:
            store.close()

    def rename_speaker(self, speaker_id: int, name: str) -> int:
        return self._with_store(lambda store: store.rename_speaker(speaker_id, name))

    def name_suggestions(self) -> list[dict]:
        from . import naming

        return self._with_store(naming.suggestions)

    def reject_name(self, speaker_id: int, name: str) -> None:
        from . import naming

        self._with_store(lambda store: naming.reject(store, speaker_id, name))

    def rename_conversation(self, conversation_id: int, title: str) -> str:
        return self._with_store(lambda store: store.rename_conversation(conversation_id, title))

    def chats(self) -> list[dict]:
        return self._with_store(lambda store: store.chats())

    def chat(self, chat_id: int) -> list[dict]:
        return self._with_store(lambda store: store.chat_messages(chat_id))

    def delete_chat(self, chat_id: int) -> None:
        self._with_store(lambda store: store.delete_chat(chat_id))

    def save_exchange(self, chat_id, question: str, answer: str, lines: dict) -> int:
        return self._with_store(lambda store: store.save_exchange(chat_id, question, answer, lines))

    def assign_line_speaker(self, utterance_id: int, name: str) -> int:
        return self._with_store(lambda store: store.assign_line_speaker(utterance_id, name))

    def conversations(self, day: str) -> list[dict]:
        start = datetime.strptime(day, "%Y-%m-%d")
        lo = int(start.timestamp() * 1000)
        hi = int((start + timedelta(days=1)).timestamp() * 1000)
        return self._with_store(lambda store: store.conversations(lo, hi))

    def recap(self, day: str) -> dict:
        """The day's recap, and whether one could be written now."""
        from .store import MIN_RECAP_CONVERSATIONS

        def look(store):
            enough = store.day_material(day)[2] >= MIN_RECAP_CONVERSATIONS
            return {"recap": store.day_recap(day), "can_write": enough and bool(config.provider())}
        return self._with_store(look)

    def write_recap(self, day: str) -> dict:
        provider = config.provider()
        if not provider:
            raise ValueError("Choose what writes summaries first, under Settings.")
        summarizer = digest_mod.make_summarizer(provider, None, config.api_key())
        try:
            written = self._with_store(lambda store: store.recap_day(day, summarizer))
        except (digest_mod.DigestError, digest_mod.DigestDeclined) as err:
            raise ValueError(f"Could not write the recap: {err}") from None
        if written is None:
            raise ValueError("This day has too few summarised conversations to recap.")
        return {"recap": written, "can_write": True}

    def reminders(self, include_done: bool = False) -> list[dict]:
        return self._with_store(lambda store: store.reminders(include_done))

    def people(self) -> list[dict]:
        """The speakers who have been named, for suggesting a name while one is typed."""
        return [{"name": name, "lines": lines, "last": last}
                for name, lines, last in self._with_store(lambda store: store.named_speakers())]

    def people_page(self) -> list[dict]:
        return self._with_store(lambda store: store.people())

    def person(self, speaker_id: int) -> dict:
        return self._with_store(lambda store: store.person(speaker_id))

    def set_reminder_status(self, reminder_id: int, status: str) -> None:
        self._with_store(lambda store: store.set_reminder_status(reminder_id, status))

    def audio_path(self, url_path: str) -> Path | None:
        """Resolve /audio/... to a file, refusing anything outside the audio folder."""
        root = self.data_dir / "audio"
        target = (self.data_dir / url_path.lstrip("/")).resolve()
        if root.resolve() not in target.parents or target.suffix != ".ogg" or not target.is_file():
            return None
        return target


def make_handler(library: Library):
    # Read once: a page newer than the running server would call addresses it lacks.
    index = _INDEX.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "limitful"

        def log_message(self, format, *args) -> None:  # keep the terminal quiet
            pass

        def _safely(self, answer) -> None:
            """Answer a request, and still answer when something unforeseen goes wrong."""
            try:
                answer()
            except (BrokenPipeError, ConnectionError):
                pass  # the page went away
            except sqlite3.OperationalError as err:
                self._fail(HTTPStatus.SERVICE_UNAVAILABLE, f"The data is busy just now ({err}). Try again in a moment.")
            except Exception as err:
                self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, f"Something went wrong: {err or type(err).__name__}")

        def _fail(self, status, message: str) -> None:
            try:
                self._text(status, message)
            except Exception:
                pass  # the reply was already under way; nothing more can be said

        def do_GET(self) -> None:
            self._safely(self._get)

        def do_POST(self) -> None:
            self._safely(self._post)

        def _get(self) -> None:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in _LOCAL_HOSTS:
                return self._text(HTTPStatus.FORBIDDEN, "This app only answers on localhost.")
            url = urlparse(self.path)
            path = unquote(url.path)
            try:
                if path == "/":
                    return self._send(HTTPStatus.OK, index, "text/html; charset=utf-8")
                if path == "/api/days":
                    return self._json(library.days())
                if path.startswith("/api/day/"):
                    day = path.removeprefix("/api/day/")
                    if not _DAY.match(day):
                        return self._text(HTTPStatus.BAD_REQUEST, "Day must be YYYY-MM-DD.")
                    return self._json(library.day(day))
                if path.startswith("/api/conversations/"):
                    day = path.removeprefix("/api/conversations/")
                    if not _DAY.match(day):
                        return self._text(HTTPStatus.BAD_REQUEST, "Day must be YYYY-MM-DD.")
                    return self._json(library.conversations(day))
                if path.startswith("/api/recap/"):
                    day = path.removeprefix("/api/recap/")
                    if not _DAY.match(day):
                        return self._text(HTTPStatus.BAD_REQUEST, "Day must be YYYY-MM-DD.")
                    return self._json(library.recap(day))
                if path == "/api/reminders":
                    done = parse_qs(url.query).get("done", ["0"])[0] == "1"
                    return self._json(library.reminders(include_done=done))
                if path == "/api/people":
                    return self._json(library.people())
                if path == "/api/status":
                    return self._json(dict(library.job, advice=pendant_advice(library.job.get("error"))))
                if path == "/api/autostart":
                    return self._json(library.autostart())
                if path == "/api/welcome":
                    return self._json(library.welcome())
                if path == "/api/settings":
                    return self._json(library.settings())
                if path == "/api/recordings":
                    return self._json(library.recordings())
                if path == "/api/pendant":
                    return self._json(library.pendant())
                if path == "/api/upkeep":
                    return self._json(upkeep.status(library))
                if path == "/api/chats":
                    return self._json(library.chats())
                found = re.match(r"^/api/chats/(\d+)$", path)
                if found:
                    return self._json(library.chat(int(found.group(1))))
                if path == "/api/persons":
                    return self._json(library.people_page())
                if path == "/api/names/suggested":
                    return self._json(library.name_suggestions())
                found = re.match(r"^/api/persons/(\d+)$", path)
                if found:
                    return self._json(library.person(int(found.group(1))))
                if path == "/api/search":
                    asked = parse_qs(url.query)
                    query, by = asked.get("q", [""])[0].strip(), asked.get("by", [""])[0].strip() or None
                    return self._json(library.search(query, by) if query or by else [])
                if path == "/api/search/meaning":
                    asked = parse_qs(url.query)
                    query, by = asked.get("q", [""])[0].strip(), asked.get("by", [""])[0].strip() or None
                    return self._json(library.search_meaning(query, by) if query else {"ready": True, "lines": []})
                if path.startswith("/audio/"):
                    return self._audio(library.audio_path(path))
            except ValueError as err:
                return self._text(HTTPStatus.BAD_REQUEST, f"That request cannot be answered: {err}")
            except sqlite3.Error as err:
                return self._text(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not read the data: {err}")
            self._text(HTTPStatus.NOT_FOUND, "Not found.")

        def _post(self) -> None:
            """POST /api/speakers/<id> {"name": ...}, /api/reminders/<id> {"status": ...}
            or /api/conversations/<id> {"title": ...}."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in _LOCAL_HOSTS:
                return self._text(HTTPStatus.FORBIDDEN, "This app only answers on localhost.")
            # JSON and zip content types cannot be sent by another website without
            # our permission, which this server never gives.
            kind = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if kind not in ("application/json", "application/zip", "application/octet-stream"):
                return self._text(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Send JSON.")
            path = urlparse(self.path).path
            expected = {"/api/import": "application/zip", "/api/recordings": "application/octet-stream"}
            if kind != expected.get(path, "application/json"):
                return self._text(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Wrong content type for this address.")
            if path == "/api/import":
                return self._import()
            if path == "/api/recordings":
                return self._recording()
            if path == "/api/forget":
                # Delete for good: one stretch as {"start_ms": ..., "end_ms": ...}, or several
                # as {"spans": [[start_ms, end_ms], ...]}.
                try:
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 65536))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    body = body if isinstance(body, dict) else {}
                    spans = body["spans"] if "spans" in body else [[body.get("start_ms"), body.get("end_ms")]]
                    if not isinstance(spans, list) or not spans or any(
                            not isinstance(span, list) or len(span) != 2
                            or any(isinstance(ms, bool) or not isinstance(ms, int) or ms < 0 for ms in span)
                            for span in spans):
                        raise ValueError("say when it starts and ends")
                    return self._json(library.forget([(start, end) for start, end in spans]))
                except RuntimeError as err:
                    return self._text(HTTPStatus.CONFLICT, str(err))
                except (ValueError, OSError, sqlite3.Error) as err:
                    return self._text(HTTPStatus.BAD_REQUEST, str(err))
            if path in ("/api/recordings/remove", "/api/recordings/move", "/api/voicememos"):
                try:
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 4096))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("send an object")
                    if path == "/api/voicememos":
                        return self._json(library.set_voice_memos(
                            body.get("enabled") is True, body.get("older") is True))
                    if not isinstance(body.get("path"), str):
                        raise ValueError("say which recording")
                    if path == "/api/recordings/remove":
                        return self._json(library.remove_recording(body["path"]))
                    start = body.get("start_ms")
                    if isinstance(start, bool) or not isinstance(start, int):
                        raise ValueError("say when it began")
                    return self._json(library.move_recording(body["path"], start))
                except (ValueError, OSError, sqlite3.Error) as err:
                    return self._text(HTTPStatus.BAD_REQUEST, str(err))
            if path == "/api/voicememos/sync":
                try:
                    library.start_sync(memos_only=True)
                    return self._json({"started": True})
                except RuntimeError as err:
                    return self._text(HTTPStatus.CONFLICT, str(err))
            if path == "/api/transcribe":
                try:
                    return self._json(library.start_transcribing())
                except RuntimeError as err:
                    return self._text(HTTPStatus.CONFLICT, str(err))
            if path == "/api/ask":
                return self._ask()
            found = re.match(r"^/api/chats/(\d+)/delete$", path)
            if found:
                library.delete_chat(int(found.group(1)))
                return self._json({"deleted": int(found.group(1))})
            if path == "/api/pendant":
                try:
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 4096))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    action = body.get("action") if isinstance(body, dict) else None
                    if action == "check":
                        return self._json(library.check_pendant())
                    if action == "usb":
                        return self._json(library.read_usb())
                    if action == "forget":
                        return self._json(library.forget_over_usb())
                    if action == "brightness":
                        return self._json(library.set_brightness(body.get("value")))
                    if action == "sync":
                        library.start_sync()
                        return self._json({"started": True})
                    if action in ("pause", "resume"):
                        return self._json(library.set_paused(action == "pause"))
                    if action == "pair":
                        library.start_pairing()
                        return self._json({"started": True})
                    if action == "unpair":
                        return self._json(library.unpair_pendant())
                    raise ValueError("unknown action")
                except RuntimeError as err:
                    return self._text(HTTPStatus.CONFLICT, str(err))
                except ValueError as err:
                    return self._text(HTTPStatus.BAD_REQUEST, str(err))
            if path == "/api/upkeep":
                # Re-timing and index building: {"action": "retime" | "index" | "stop"}.
                try:
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 4096))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    action = body.get("action") if isinstance(body, dict) else None
                    if action == "retime":
                        upkeep.start_retime(library)
                    elif action == "index":
                        upkeep.start_index(library)
                    elif action == "stop":
                        upkeep.stop(library)
                    else:
                        raise ValueError("unknown action")
                    return self._json({"ok": True})
                except RuntimeError as err:
                    return self._text(HTTPStatus.CONFLICT, str(err))
                except ValueError as err:
                    return self._text(HTTPStatus.BAD_REQUEST, str(err))
            if path.startswith("/api/recap/"):
                day = path.removeprefix("/api/recap/")
                if not _DAY.match(day):
                    return self._text(HTTPStatus.BAD_REQUEST, "Day must be YYYY-MM-DD.")
                try:
                    return self._json(library.write_recap(day))
                except ValueError as err:
                    return self._text(HTTPStatus.BAD_REQUEST, str(err))
            if path == "/api/autostart":
                try:
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 4096))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
                        raise ValueError('send {"enabled": true} or {"enabled": false}')
                    return self._json(library.set_autostart(body["enabled"]))
                except ValueError as err:
                    return self._text(HTTPStatus.BAD_REQUEST, str(err))
            if path == "/api/demo":
                try:
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 4096))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    return self._json(library.demo(body.get("action") if isinstance(body, dict) else ""))
                except ValueError as err:
                    return self._text(HTTPStatus.BAD_REQUEST, str(err))
            if path in ("/api/settings", "/api/digest"):
                try:
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 8192))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("send a JSON object")
                    if path == "/api/settings":
                        return self._json(library.save_settings(body))
                    library.start_digest()
                    return self._json({"started": True})
                except RuntimeError as err:
                    return self._text(HTTPStatus.CONFLICT, str(err))
                except ValueError as err:
                    return self._text(HTTPStatus.BAD_REQUEST, str(err))
            match = (re.match(r"^/api/(speakers|reminders|conversations)/(\d+)$", path)
                     or re.match(r"^/api/(lines)/(\d+)/speaker$", path)
                     or re.match(r"^/api/speakers/(\d+)/(not)$", path))
            if not match:
                return self._text(HTTPStatus.NOT_FOUND, "Not found.")
            if match.group(2) == "not":
                kind, item = "not", int(match.group(1))
            else:
                kind, item = match.group(1), int(match.group(2))
            try:
                length = max(0, min(int(self.headers.get("Content-Length") or 0), 4096))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("send a JSON object")
                if kind == "not":  # the owner says this voice is not that person
                    name = body.get("name", "")
                    if not isinstance(name, str):
                        raise ValueError("say which name")
                    library.reject_name(item, name[:80])
                    return self._json({"id": item, "not": name})
                if kind in ("speakers", "lines"):
                    name = body.get("name", "")
                    if not isinstance(name, str):
                        raise ValueError("a speaker needs a name")
                    change = library.rename_speaker if kind == "speakers" else library.assign_line_speaker
                    return self._json({"id": change(item, name[:80])})
                if kind == "conversations":
                    title = body.get("title", "")
                    if not isinstance(title, str):
                        raise ValueError("a conversation needs a title")
                    return self._json({"id": item, "title": library.rename_conversation(item, title[:200])})
                status = body.get("status")
                if not isinstance(status, str):
                    raise ValueError("a reminder needs a status")
                library.set_reminder_status(item, status)
                return self._json({"id": item, "status": status})
            except ValueError as err:
                return self._text(HTTPStatus.BAD_REQUEST, str(err))
            except sqlite3.Error as err:
                return self._text(HTTPStatus.INTERNAL_SERVER_ERROR, str(err))

        def _ask(self) -> None:
            """Answer a question, sending progress as it happens.

            The reply is one JSON object per line: {"step": ...} while Claude looks
            things up, {"text": ...} for each piece of the answer as it is written
            ({"text": null} means discard what came so far), then the whole
            {"answer": ..., "lines": ..., "chat": thread id}, or {"error": ...}.
            Send {"question": ..., "chat": thread id or null}.
            """
            try:
                length = max(0, min(int(self.headers.get("Content-Length") or 0), 1 << 20))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("send a JSON object")
                question = body.get("question")
                if not isinstance(question, str) or not question.strip():
                    raise ValueError("ask a question first")
                question = question.strip()[:4000]
                chat_id = body.get("chat")
                if chat_id is not None and (not isinstance(chat_id, int) or isinstance(chat_id, bool)):
                    raise ValueError("chat must be a thread id")
            except ValueError as err:
                return self._text(HTTPStatus.BAD_REQUEST, str(err))
            if config.provider() != "claude":
                return self._text(HTTPStatus.BAD_REQUEST,
                                  "Ask needs Claude. Choose Claude in Settings first; "
                                  "your questions and the transcripts it reads are sent to Anthropic.")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()  # no length: the reply ends when the connection closes

            def emit(item: dict) -> None:
                try:
                    self.wfile.write(json.dumps(item).encode() + b"\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the reader left; let the answer finish quietly

            try:
                # The thread so far comes from the database, not from the page.
                earlier = library.chat(chat_id) if chat_id is not None else []
                history = [{"role": m["role"], "content": m["content"]} for m in earlier][-40:]
                history.append({"role": "user", "content": question})
                result = ask_mod.answer(library.data_dir, history, config.api_key(),
                                        on_step=lambda step: emit({"step": step}),
                                        on_text=lambda text: emit({"text": text}))
                # Saved only once answered, so a failed question leaves no half-thread.
                result["chat"] = library.save_exchange(
                    chat_id, question, result["answer"], result["lines"])
                emit(result)
            except digest_mod.DigestError as err:
                emit({"error": str(err)})
            except Exception as err:  # never leave the page waiting on a broken stream
                emit({"error": f"Something went wrong: {err}"})

        def _import(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= _MAX_UPLOAD:
                return self._text(HTTPStatus.BAD_REQUEST, "Choose an export zip to import.")
            try:
                return self._json(library.import_export(self.rfile, length))
            except RuntimeError as err:
                return self._text(HTTPStatus.CONFLICT, str(err))
            except zipfile.BadZipFile:
                return self._text(HTTPStatus.BAD_REQUEST, "That file is not a zip archive.")
            except (ValueError, OSError, sqlite3.Error) as err:
                return self._text(HTTPStatus.BAD_REQUEST, str(err))

        def _recording(self) -> None:
            from .recordings import SUFFIXES, NotAudio

            try:
                length = int(self.headers.get("Content-Length") or 0)
                modified = int(self.headers.get("X-Modified") or 0) or None
            except ValueError:
                length, modified = 0, None
            name = Path(unquote(self.headers.get("X-Name") or "")).name[:200]
            if not 0 < length <= _MAX_UPLOAD:
                return self._text(HTTPStatus.BAD_REQUEST, "Choose an audio file to add.")
            if Path(name).suffix.lower() not in SUFFIXES:
                return self._text(HTTPStatus.BAD_REQUEST,
                                  "That is not a kind of audio file this can read. Try .m4a, .mp3 or .wav.")
            try:
                return self._json(library.add_recording(self.rfile, length, name, modified))
            except NotAudio as err:
                return self._text(HTTPStatus.BAD_REQUEST, str(err))
            except (ValueError, OSError, sqlite3.Error) as err:
                return self._text(HTTPStatus.BAD_REQUEST, str(err))

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser stopped listening, e.g. while seeking

        def _json(self, payload) -> None:
            self._send(HTTPStatus.OK, json.dumps(payload).encode(), "application/json")

        def _text(self, status: int, message: str) -> None:
            self._send(status, message.encode(), "text/plain; charset=utf-8")

        def _audio(self, file: Path | None) -> None:
            if file is None:
                return self._text(HTTPStatus.NOT_FOUND, "No such recording.")
            data = file.read_bytes()
            size = len(data)
            headers = {"Accept-Ranges": "bytes"}
            match = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
            if not match or not (match.group(1) or match.group(2)):
                return self._send(HTTPStatus.OK, data, "audio/ogg", headers)
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            else:  # "bytes=-N": the last N bytes
                start, end = max(0, size - int(match.group(2))), size - 1
            if start >= size or start > end:
                headers["Content-Range"] = f"bytes */{size}"
                return self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", "audio/ogg", headers)
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            self._send(HTTPStatus.PARTIAL_CONTENT, data[start : end + 1], "audio/ogg", headers)

    return Handler


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second program bind the same port silently,
    # so a second copy would split requests with the first instead of failing.
    allow_reuse_address = sys.platform != "win32"


def make_server(data_dir: Path, port: int, link_speakers: bool = False,
                sync_every: float = 0) -> ThreadingHTTPServer:
    from .store import Store

    # Opening the store creates the database if needed and applies any upgrades,
    # so the read-only queries below always see the current layout.
    Store(data_dir).close()
    library = Library(data_dir, link_speakers)
    server = _Server(("127.0.0.1", port), make_handler(library))
    library.port = server.server_port
    server.library = library  # the app's tray menu uses it
    library.set_auto_sync(sync_every)
    return server
