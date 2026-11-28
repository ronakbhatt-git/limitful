"""Command-line interface. `limitful --help` lists the commands; each is a `_cmd_*` function here."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
import zipfile
import time
from pathlib import Path

from bleak.exc import BleakError

from . import __version__, config
from .client import Pendant, PendantError, pair, scan
from .protocol import PAGE_SECONDS
from .store import Store
from .transcribe import DEFAULT_MODEL
from .watch import watch


def default_data_dir() -> Path:
    if os.environ.get("LIMITFUL_DATA"):
        return Path(os.environ["LIMITFUL_DATA"])
    legacy = Path.home() / "PendantSync"  # the folder used before the project was named
    current = Path.home() / "Limitful"
    return legacy if legacy.exists() and not current.exists() else current


async def _resolve(address: str | None, timeout: float):
    if address:
        return address
    pendants = await scan(timeout)
    if not pendants:
        raise PendantError(
            "no pendant found. It only advertises when it is not connected to "
            "another device, so turn off Bluetooth on your phone (or quit the "
            "Limitless app) and try again."
        )
    if len(pendants) > 1:
        print(f"{len(pendants)} pendants found; using the strongest signal.", file=sys.stderr)
    return pendants[0][0]


async def _cmd_scan(args) -> int:
    pendants = await scan(args.timeout)
    if not pendants:
        print("No pendant found.")
        return 1
    for device, rssi in pendants:
        print(f"{device.address}  {device.name or '(unnamed)'}  rssi {rssi} dBm")
    return 0


def _print_state(state) -> None:
    print(f"stored pages:  {state.page_count} ({state.oldest_flash_page}..{state.newest_flash_page})")
    print(f"free pages:    {state.free_capture_pages} of {state.total_capture_pages}")
    print(f"session:       {state.current_storage_session}")


async def _cmd_status(args) -> int:
    device = await _resolve(args.address, args.timeout)
    async with Pendant(device) as pendant:
        state = await pendant.status()
        battery = await pendant.battery()
        if args.brightness is not None:
            await pendant.set_led_brightness(args.brightness)
    print(f"battery:       {battery}%" if battery is not None else "battery:       not reported")
    _print_state(state)
    hours = state.free_capture_pages * PAGE_SECONDS / 3600
    print(f"room for:      about {hours:.0f} more hours of audio")
    if args.brightness is not None:
        config.update(led_brightness=max(0, min(100, args.brightness)))
        print(f"light:         set to {max(0, min(100, args.brightness))}%")
    return 0


async def _cmd_pair(args) -> int:
    print("Put the pendant in pairing mode now: press and release the button, then")
    print("immediately press and hold until the purple light goes out. Blue means ready.")
    print("This is a factory reset: it erases any audio still stored on the pendant.")
    print("(To keep that audio, pair from the Lifelog instead: Settings, Pendant, Pair without erasing.)")
    print(f"Accept the pairing request if your computer shows one. Waiting {args.timeout:.0f} s...")
    state = await pair(args.timeout)
    if state is None:
        print("Gave up: the pendant never accepted this computer.")
        return 1
    print("Paired. The pendant accepted this computer.")
    _print_state(state)
    return 0


async def _cmd_retime(args) -> int:
    store = Store(args.data_dir)
    try:
        if args.undo:
            print(f"Put {store.undo_retime()} line(s) back at their imported times.")
            return 0
        pending = store.pending_retime()
        if not pending:
            print("Every imported recording is already re-timed.")
            return 0
        from .transcribe import WhisperTranscriber

        total_h = sum(duration for _p, _s, duration in pending) / 3_600_000
        print(f"{len(pending)} imported recording(s) to re-time, {total_h:.1f} h of audio. "
              "Newest first; safe to stop and run again.")
        whisper = WhisperTranscriber(args.model, args.language, args.device)
        started = time.monotonic()
        files = found = lines = 0
        audio_ms = 0
        for path, _start, duration in pending:
            if args.minutes and time.monotonic() - started > args.minutes * 60:
                break
            words = await asyncio.to_thread(whisper.words, args.data_dir / path)
            got, of = store.retime_recording(path, words)
            files, found, lines, audio_ms = files + 1, found + got, lines + of, audio_ms + duration
            print(f"\r  {files}/{len(pending)} recordings, {audio_ms / 3_600_000:.1f} h", end="",
                  file=sys.stderr, flush=True)
        print(file=sys.stderr)
        took = time.monotonic() - started
        print(f"Re-timed {lines} line(s) in {files} recording(s) ({audio_ms / 60000:.0f} min of audio, "
              f"{took / 60:.0f} min). {found} were found by their words; the rest moved with a neighbour.")
        left = len(pending) - files
        if left:
            print(f"{left} recording(s) left. Run the command again to continue.")
        store.rewrite_all_transcripts()
        return 0
    finally:
        store.close()


async def _cmd_score_speakers(args) -> int:
    from .hosted import HostedError
    from .scoring import score_service
    from .speakers import load_audio
    from .transcribe import make_transcriber

    try:
        service = make_transcriber("deepgram", language=args.language)
    except HostedError as err:
        raise PendantError(str(err)) from None
    Store(args.data_dir).close()  # make sure the database is up to date
    print(f"Sending up to {args.limit} imported conversation(s) to {service.name} and comparing its "
          "speakers with Limitless's.")
    try:
        rows = await asyncio.to_thread(score_service, args.data_dir, service, load_audio, args.limit)
    except HostedError as err:
        raise PendantError(str(err)) from None
    if not rows:
        print("No imported conversations fit the test (two to four speakers, 50 to 220 lines, with audio).")
        return 1
    for title, lines, speakers, found, score, baseline in rows:
        print(f"  {title[:44]:44s} {lines:3d} lines, {speakers} speakers, service found {found}: "
              f"{score:.0%} (guessing the main speaker: {baseline:.0%})")
    mean = sum(r[4] for r in rows) / len(rows)
    base = sum(r[5] for r in rows) / len(rows)
    print(f"Speaking time given to the right speaker, on average: {mean:.0%}. "
          f"Always guessing the main speaker: {base:.0%}.")
    return 0


async def _cmd_index(args) -> int:
    from . import search

    index = search.Index(args.data_dir)
    if args.remove:
        index.path.unlink(missing_ok=True)
        print("Removed the search-by-meaning index. Transcripts are untouched.")
        return 0
    store = Store(args.data_dir)
    try:
        if args.find:
            if not index.exists():
                raise PendantError("there is no index yet: run `uv run limitful index` first")
            start_of = dict(store.db.execute("SELECT id, start_ms FROM utterances"))
            hits = search.spread(index.search(search.make_embedder(), args.find, limit=200), start_of, 10)
            for uid, score in hits:
                (text,) = store.db.execute("SELECT text FROM utterances WHERE id = ?", (uid,)).fetchone()
                when = datetime.fromtimestamp(start_of[uid] / 1000).strftime("%Y-%m-%d %H:%M")
                print(f"{score:.2f}  {when}  {text}")
            return 0
        left = index.progress(store)["left"]
        if not left:
            print("Every line is already indexed.")
            return 0
        print(f"Indexing {left} line(s) for search by meaning (a 34 MB model downloads on first use). "
              "Safe to stop and run again.")
        embedder = await asyncio.to_thread(search.make_embedder)

        def progress(done: int, total: int) -> None:
            print(f"\r  {done}/{total} lines", end="", file=sys.stderr, flush=True)

        started = time.monotonic()
        added = await asyncio.to_thread(index.build, store, embedder, progress)
        print(file=sys.stderr)
        print(f"Indexed {added} line(s) in {time.monotonic() - started:.0f} s. "
              "New lines are added as they are transcribed.")
        return 0
    finally:
        store.close()


async def _cmd_pause(args) -> int:
    store = Store(args.data_dir)
    try:
        if args.command == "pause":
            since = store.pause()
            print(f"Paused since {datetime.fromtimestamp(since / 1000).strftime('%H:%M')}. The pendant keeps "
                  "recording; audio from now until you resume is deleted when it syncs.")
        elif store.paused_since() is None:
            print("Not paused.")
        else:
            store.resume()
            print("Resumed. Audio from now on is kept.")
        return 0
    finally:
        store.close()


async def _cmd_unpair(args) -> int:
    from .client import unpair

    await unpair(args.timeout)
    print("Unpaired. The pendant has forgotten this computer and keeps its recordings.")
    print("To use it here again, run `uv run limitful pair`.")
    return 0


def _print_segments(segments) -> None:
    total_s = sum(s.duration_ms for s in segments) / 1000
    print(f"Exported {len(segments)} audio file(s), {total_s / 60:.1f} min.")
    for seg in segments:
        print(f"  {seg.path}  ({seg.duration_ms / 1000:.0f} s)")


async def _cmd_sync(args) -> int:
    from . import pendant_lock

    try:
        with pendant_lock.held(args.data_dir):
            return await _sync_once(args)
    except pendant_lock.Busy as err:
        raise PendantError(f"{err}. Wait for it to finish, or quit it from its tray icon.") from None


async def _sync_once(args) -> int:
    store = Store(args.data_dir)
    try:
        device = await _resolve(args.address, args.timeout)

        def progress(received: int, total: int) -> None:
            print(f"\r  {received}/{total} pages", end="", file=sys.stderr, flush=True)

        async with Pendant(device) as pendant:
            result = await pendant.sync(store, ack=args.ack, on_progress=progress)
        if result.pages_received:
            print(file=sys.stderr)

        if result.reason == "empty":
            print("Nothing stored on the pendant.")
        else:
            print(f"Received {result.pages_received} pages ({result.pages_new} new), "
                  f"finished: {result.reason}.")
            if result.pages_discarded:
                print(f"Deleted {result.pages_discarded} page(s) recorded while paused.")
            if result.acked_up_to is not None:
                print(f"Pendant freed pages up to {result.acked_up_to}.")
            elif not args.ack:
                print("Pendant storage untouched (run with --ack to free synced pages).")
        if not args.no_export:
            _print_segments(store.export_audio())
        return 0 if result.reason != "stalled" else 2
    finally:
        store.close()


async def _cmd_export(args) -> int:
    store = Store(args.data_dir)
    try:
        _print_segments(store.export_audio())
    finally:
        store.close()
    return 0


def _label_speakers(store: Store, link_imported: bool = False) -> None:
    from .speakers import TitanetEmbedder, load_audio

    embedder = TitanetEmbedder()
    labelled = store.label_speakers(embedder, load_audio)
    if labelled:
        print(f"Labelled {labelled} line(s) by speaker.")

    def progress(done: int, total: int) -> None:
        print(f"\r  listening to imported speakers: {done}/{total}", end="", file=sys.stderr, flush=True)
        if done == total:
            print(file=sys.stderr)

    if not link_imported:
        return
    linked, conflicts = store.link_imported_speakers(embedder, load_audio, progress)
    if linked:
        print(f"Linked {linked} speaker(s) from imported conversations to people.")
    if conflicts:
        print(f"In {conflicts} case(s) the closest voice was already another speaker in the "
              "same conversation, so a different person was used.")


async def _cmd_speakers(args) -> int:
    store = Store(args.data_dir)
    try:
        if args.rename:
            try:
                store.rename_speaker(int(args.rename[0]), args.rename[1])
            except ValueError as err:
                raise PendantError(str(err)) from None
        elif args.reset:
            print(f"Regrouped {store.reset_speakers()} line(s).")
        elif args.unlink_imported:
            print(f"Unlinked {store.unlink_imported_speakers()} imported speaker(s); named ones were kept.")
        else:
            _label_speakers(store, args.link_imported)
        rows = store.speakers()
        if not rows:
            print("No speakers yet. They appear once about ten lines of speech "
                  "(each 1.5 s or longer) have been transcribed.")
        for speaker_id, name, lines in rows:
            print(f"{speaker_id:4d}  {name}  ({lines} line{'s' if lines != 1 else ''})")
        return 0
    finally:
        store.close()


async def _cmd_demo(args) -> int:
    from . import demo

    try:
        made = demo.build(args.data_dir)
    except ValueError as err:
        raise PendantError(str(err)) from None
    print(f"Wrote {made['conversations']} made-up conversations ({made['lines']} lines over "
          f"{made['days']} days) to {args.data_dir}.")
    print(f"Open them with: uv run limitful serve --data-dir {args.data_dir}")
    return 0


def _index_imported(store: Store) -> None:
    """Add freshly imported lines to search by meaning, if the user has set it up."""
    from . import search

    index = search.Index(store.data_dir)
    if not index.exists():
        return

    def progress(done: int, total: int) -> None:
        print(f"\r  {done}/{total} lines added to search by meaning", end="", file=sys.stderr, flush=True)

    try:
        added = index.build(store, search.make_embedder(), progress)
    except Exception as err:  # the import itself is already saved
        print(f"\nSearch by meaning was not updated ({err}). Run `uv run limitful index` to add the new lines.")
        return
    if added:
        print(file=sys.stderr)
        print(f"Added {added} line(s) to search by meaning.")


async def _cmd_add(args) -> int:
    from .recordings import NotAudio, add_recording

    store = Store(args.data_dir)
    added = 0
    try:
        for file in args.files:
            if not file.is_file():
                raise PendantError(f"no such file: {file}")
            try:
                result = await asyncio.to_thread(
                    add_recording, store, file, file.name, int(file.stat().st_mtime * 1000))
            except NotAudio as err:
                raise PendantError(f"{file}: {err}") from None
            if not result.new:
                print(f"{file.name}: already added.")
                continue
            added += 1
            when = datetime.fromtimestamp(result.start_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{file.name}: added at {when} ({result.duration_ms / 1000:.0f} s).")
    finally:
        store.close()
    if added:
        print("Run `uv run limitful transcribe` to turn them into text.")
    return 0


async def _cmd_voice_memos(args) -> int:
    from . import voicememos

    if not voicememos.supported():
        raise PendantError("Voice Memos are only read on macOS")
    since = 0
    if args.since:
        try:
            since = int(datetime.strptime(args.since, "%Y-%m-%d").timestamp() * 1000)
        except ValueError:
            raise PendantError("--since takes a date such as 2026-11-01") from None
    store = Store(args.data_dir)
    try:
        try:
            found = await asyncio.to_thread(voicememos.sync, store, since)
        except voicememos.NoAccess:
            raise PendantError(
                "macOS is not letting this program read your Voice Memos. In System Settings, Privacy & "
                f"Security, Full Disk Access, add and switch on:\n  {voicememos.program()}") from None
        except voicememos.NoLibrary:
            raise PendantError("no Voice Memos library was found on this Mac") from None
    finally:
        store.close()
    print(f"Added {found.added} Voice Memo(s)." + (f" {found.failed} could not be read." if found.failed else ""))
    if found.added:
        print("Run `uv run limitful transcribe` to turn them into text.")
    return 0


async def _cmd_import(args) -> int:
    from .importer import import_limitless

    if not args.file.is_file():
        raise PendantError(f"no such file: {args.file}")
    store = Store(args.data_dir)
    try:
        try:
            result = await asyncio.to_thread(import_limitless, store, args.file)
        except zipfile.BadZipFile:
            raise PendantError(f"{args.file} is not a zip file") from None
        print(f"Imported {result.conversations} conversation(s), {result.lines} line(s) "
              f"({result.lines_with_audio} with audio), {result.audio_files} recording(s) "
              f"({result.audio_hours:.1f} h) and {result.reminders} reminder(s).")
        if result.skipped_existing:
            print(f"Skipped {result.skipped_existing} conversation(s) imported before.")
        if result.utc_offset_minutes is not None:
            hours = result.utc_offset_minutes / 60
            print(f"Audio file names were read as UTC{hours:+g}h, the best fit to the transcripts.")
        for item in result.not_imported:
            print(f"Not imported yet: {item}")
        if result.lines:
            await asyncio.to_thread(_index_imported, store)
        return 0
    finally:
        store.close()


_CHOOSE_PROVIDER = (
    "choose what writes the summaries: --provider claude (better; sends transcripts to "
    "Anthropic) or --provider ollama (runs on this computer). "
    "Choose once in the Lifelog's Settings to make it stick."
)


def _summarizer(args):
    from .digest import make_summarizer

    provider = args.provider or config.provider()
    if not provider:
        raise PendantError(_CHOOSE_PROVIDER)
    try:
        return make_summarizer(provider, args.llm_model, config.api_key())
    except ValueError as err:
        raise PendantError(str(err)) from None


async def _cmd_mcp(args) -> int:
    from . import mcp_server

    data_dir = Path(args.data_dir).resolve()
    if args.config:
        # The same Python that runs this, by its full path: the app that starts the server has no idea where it lives.
        print(mcp_server.describe(data_dir, [sys.executable, "-m", "limitful"], args.triage))
        return 0
    if not (data_dir / "pendant.db").is_file():
        raise PendantError(f"there is no Lifelog in {data_dir} yet")
    await mcp_server.build(data_dir, args.triage).run_stdio_async()
    return 0


async def _cmd_notes(args) -> int:
    from datetime import datetime, timedelta

    from .digest import DigestError

    summarizer = _summarizer(args)
    store = Store(args.data_dir)
    try:
        total = 0
        for day in args.day:
            try:
                start = datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                raise PendantError(f"{day} is not a day; write it as YYYY-MM-DD") from None
            lo, hi = int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)

            def progress(done: int, of: int, day: str = day) -> None:
                print(f"\r  {day}: {done}/{of} conversations", end="", file=sys.stderr, flush=True)

            try:
                total += await asyncio.to_thread(store.write_notes, summarizer, lo, hi, args.redo, progress)
            except DigestError as err:
                print(file=sys.stderr)
                raise PendantError(f"{err}. Notes already written were kept.") from None
            print(file=sys.stderr)
        print(f"Wrote notes for {total} conversation(s) with {summarizer.name}.")
        return 0
    finally:
        store.close()


async def _cmd_digest(args) -> int:
    from .digest import DigestError

    summarizer = _summarizer(args)
    store = Store(args.data_dir)
    try:
        if args.redo:
            store.forget_digests()

        def progress(done: int, total: int) -> None:
            print(f"\r  {done}/{total} conversations", end="", file=sys.stderr, flush=True)

        recaps = "SELECT day, signature FROM day_recaps"
        before = set(store.db.execute(recaps))
        try:
            done = await asyncio.to_thread(store.digest_pending, summarizer, progress)
        except DigestError as err:
            print(file=sys.stderr)
            raise PendantError(f"{err}. Conversations already summarised were kept.") from None
        print(file=sys.stderr)
        print(f"Summarised {done} conversation(s) with {summarizer.name}.")
        if args.days:
            def day_progress(n: int, total: int) -> None:
                print(f"\r  {n}/{total} days", end="", file=sys.stderr, flush=True)

            try:
                await asyncio.to_thread(store.recap_pending, summarizer, True, day_progress)
            except DigestError as err:
                print(file=sys.stderr)
                raise PendantError(f"{err}. Recaps already written were kept.") from None
            print(file=sys.stderr)
        written = len(set(store.db.execute(recaps)) - before)
        if written or args.days:
            print(f"Wrote a recap for {written} day(s).")
        waiting = [r for r in store.reminders() if r["status"] == "suggested"]
        if waiting:
            print(f"{len(waiting)} suggested reminder(s) to review in the Lifelog.")
        return 0
    finally:
        store.close()


async def _cmd_ask(args) -> int:
    from . import ask
    from .digest import DigestError

    if (args.provider or config.provider()) != "claude":
        raise PendantError("ask needs Claude: pass --provider claude or choose it in Settings. "
                           "Your question and the transcripts it reads are sent to Anthropic.")
    Store(args.data_dir).close()  # make sure the database is up to date
    written = []

    def on_text(text: str | None) -> None:
        if text is None:  # a remark before another lookup: start the answer again
            if written:
                print("\n", flush=True)
            written.clear()
        else:
            written.append(text)
            print(text, end="", flush=True)

    try:
        await asyncio.to_thread(
            ask.answer, args.data_dir, [{"role": "user", "content": args.question}], config.api_key(),
            args.llm_model or ask.CLAUDE_MODEL, lambda step: print(f"  {step}", file=sys.stderr), on_text)
    except DigestError as err:
        raise PendantError(str(err)) from None
    print()
    return 0


async def _cmd_transcribe(args) -> int:
    store = Store(args.data_dir)
    try:
        if args.redo:
            store.forget_transcriptions()
        pending = store.pending_transcription()
        if not pending:
            print("Nothing new to transcribe.")
            return 0
        from .hosted import HostedError
        from .transcribe import make_transcriber

        engine = args.engine or config.transcriber()
        if engine == "local":
            print(f"Loading {args.model} (downloads on first use)...", file=sys.stderr)
        else:
            print(f"Sending audio to {engine.capitalize()}, a conversation at a time...", file=sys.stderr)
        try:
            transcriber = make_transcriber(engine, args.model, args.language, args.device)
        except HostedError as err:
            raise PendantError(str(err)) from None

        def progress(done: int, total: int) -> None:
            what = "files" if engine == "local" else "conversations"
            print(f"\r  {done}/{total} {what}", end="", file=sys.stderr, flush=True)

        started = time.monotonic()
        try:
            added, files = store.transcribe_pending(transcriber, progress)
        except HostedError as err:
            print(file=sys.stderr)
            raise PendantError(f"{err}. Conversations already transcribed were kept.") from None
        print(file=sys.stderr)
        waiting = store.pending_transcription()  # a hosted service leaves the newest conversation
        audio_s = (sum(duration for _, _, duration in pending) - sum(duration for _, _, duration in waiting)) / 1000
        took = time.monotonic() - started
        print(f"Transcribed {audio_s / 60:.1f} min of audio in {took:.0f} s: {added} line(s).")
        if waiting:
            print("The most recent conversation is sent once it has been quiet for five minutes, "
                  "so that it goes as one piece.")
        elif not added:
            print("No speech was detected in the new audio.")
        if args.speakers:
            _label_speakers(store)
        for path in files:
            print(f"  {path}")
        return 0
    finally:
        store.close()


async def _cmd_watch(args) -> int:
    store = Store(args.data_dir)
    cached: list = []

    def get_transcriber():
        if not cached:
            from .transcribe import make_transcriber

            engine = config.transcriber()
            if engine == "local":
                print(f"Loading {args.model} (downloads on first use)...", flush=True)
            cached.append(make_transcriber(engine, args.model, args.language, args.device))
        return cached[0]

    embedders: list = []

    def get_embedder():
        if not embedders:
            from .speakers import TitanetEmbedder

            embedders.append(TitanetEmbedder())
        return embedders[0]

    summarizer = None
    if args.provider or config.provider():
        summarizer = _summarizer(args)

    print(f"Watching for the pendant every {args.interval:.0f} s. Data: {args.data_dir}")
    print("Synced audio is " + ("kept on" if args.keep else "freed from") + " the pendant. "
          "Press Ctrl-C to stop.")
    try:
        await watch(store, interval=args.interval, ack=not args.keep,
                    get_transcriber=None if args.no_transcribe else get_transcriber,
                    get_embedder=get_embedder if args.speakers and not args.no_transcribe else None,
                    summarizer=summarizer)
    finally:
        store.close()
    return 0


async def _cmd_autostart(args) -> int:
    import socket

    from . import autostart

    try:
        if args.action == "on":
            # On Windows, stop the copy the old login task started, so the new one takes its place now.
            if sys.platform == "win32" and autostart.status().get("running"):
                autostart.stop_running()
                await asyncio.sleep(3)
            # If the Lifelog is already open on that port, the login item waits for the next login.
            with socket.socket() as probe:
                free = probe.connect_ex(("127.0.0.1", args.port)) != 0
            autostart.enable(args.data_dir, args.port, start_now=free)
            print(f"Limitful will start when you log in and sync the pendant every "
                  f"{autostart.SYNC_EVERY_S // 60} minutes. Lifelog: http://localhost:{args.port}")
            if sys.platform == "win32":
                print("Limitful is also in the Start menu. It opens in its own window and sits in the "
                      "system tray; it updates itself from GitHub.")
            print("It is running now." if free else
                  f"Port {args.port} is in use (is `uv run limitful serve` open?), so it starts at your next login.")
        elif args.action == "off":
            autostart.disable()
            print("Limitful no longer starts at login.")
        else:
            state = autostart.status()
            if not state["supported"]:
                print("Starting at login is only supported on macOS and Windows so far.")
            else:
                print("Starts at login: " + ("yes" if state["enabled"] else "no")
                      + (", and it is running now." if state["running"] else "."))
    except autostart.AutostartError as err:
        raise PendantError(str(err)) from None
    return 0


async def _cmd_brain(args) -> int:
    from . import brain

    try:
        if args.action == "keygen":
            public, private = brain.new_key()
            print("Public key (this computer keeps it; it can lock audio but not open it):")
            print(f"  {public}")
            print()
            print("Private key: save it in your password manager now, then clear this window.")
            print("Do not keep it on this computer. Without it the audio cannot be opened again.")
            print(f"  {private}")
            return 0
        if args.action == "setup":
            if not args.repo:
                raise PendantError("give --repo, the folder where your private lifelog repo is cloned")
            current = brain.settings()
            recipient = args.recipient or (current.recipient if current else None)
            audio = args.audio or (current.audio if current else None)
            saved = brain.save_settings(args.repo, audio, recipient, args.delay_hours)
            print(f"Transcripts go to {saved.repo}")
            print(f"Encrypted audio goes to {saved.audio}" if saved.audio else "Audio is not exported.")
            print(f"Every transcript is exported {saved.delay_hours:g} hours after its day ends; "
                  "audio only for conversations Claude tiers keep.")
            return 0
        if args.action == "restore":
            if not (args.audio and args.to):
                raise PendantError("give --audio (the encrypted folder) and --to (where to put the recordings)")
            identity = os.environ.get("LIMITFUL_AGE_KEY") or input("Private key (AGE-SECRET-KEY-...): ")
            count = brain.restore(args.audio, identity, args.to)
            print(f"Decrypted {count} recordings into {args.to}")
            return 0
        if args.action in ("exclude", "include"):
            if not args.ids:
                raise PendantError(f"give the conversation ids to {args.action}, such as `limitful brain {args.action} 4146`")
            saved = brain.set_excluded(args.ids, args.action == "exclude")
            print(f"Kept out of the repo: {', '.join(map(str, sorted(saved.exclude))) or 'nothing'}. "
                  "This takes effect at the next export; git history still holds anything pushed earlier.")
            return 0
        saved = brain.settings()
        if saved is None:
            raise PendantError("the second brain is not set up yet: run `limitful brain setup --repo ...` first")
        if args.action == "status":
            store = Store(args.data_dir)
            try:
                ready, held = brain.ready_days(store, saved.delay_hours)
            finally:
                store.close()
            print(f"Repo: {saved.repo}\nAudio: {saved.audio or 'not exported'}")
            print(f"{len(ready)} days ready to export; {len(held)} with conversations Claude has not triaged yet"
                  + (f" ({', '.join(held[-5:])})" if held else "") + ". Their audio follows once triaged.")
            return 0
        store = Store(args.data_dir)
        try:
            result = brain.export(store, saved, git=not args.no_git)
        finally:
            store.close()
        print(f"Exported {len(result.days)} days: {result.conversations} conversations, {result.lines} lines.")
        if saved.audio:
            print(f"Audio: {result.audio_added} recordings encrypted, {result.audio_removed} removed.")
        if result.waiting_days:
            print(f"Audio waits for Claude's triage on: {', '.join(result.waiting_days[-10:])}")
        if not args.no_git:
            print("Committed and pushed." if result.pushed else "Committed." if result.committed else "Nothing new to commit.")
    except brain.BrainError as err:
        raise PendantError(str(err)) from None
    return 0


async def _cmd_app(args) -> int:
    from . import app as app_mod
    from .web import make_server

    url = f"http://localhost:{args.port}"
    try:
        server = make_server(args.data_dir, args.port, sync_every=args.sync_every)
    except OSError as err:
        if app_mod.already_running(args.port):
            # Opened again from the Start menu, say: show the copy that is running.
            if not args.background:
                app_mod.open_window(url)
            return 0
        raise PendantError(f"could not use port {args.port}: {err.strerror or err}") from None
    print(f"Limitful is running. Lifelog: {url}  (data: {args.data_dir})", flush=True)
    app = app_mod.App(server, url, app_mod.relaunch_argv(),
                      auto_update=not args.no_update and not os.environ.get("LIMITFUL_NO_UPDATE"))
    return app.run(not args.background)  # the tray wants the main thread


async def _cmd_serve(args) -> int:
    from .web import make_server

    try:
        server = make_server(args.data_dir, args.port, sync_every=args.sync_every)
    except OSError as err:
        raise PendantError(f"could not use port {args.port}: {err.strerror or err}") from None
    print(f"Lifelog is at http://localhost:{server.server_port}  (data: {args.data_dir})")
    if args.sync_every:
        print(f"Syncing the pendant by itself every {int(args.sync_every)} seconds.")
    print("Press Ctrl-C to stop.")
    try:
        await asyncio.to_thread(server.serve_forever)
    finally:
        server.shutdown()
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    # On Windows, output sent to a file or a scheduled task's log uses the old
    # code page; a transcript line with a character outside it must not crash the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        prog="limitful",
        description="Limitful: sync a Limitless Pendant over Bluetooth, transcribe it, and browse, search, summarise and ask about your days.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--log", type=Path, help="append output to this file (used when started at login)")
    sub = parser.add_subparsers(dest="command", required=True)

    def device_args(p) -> None:
        p.add_argument("--address", help="pendant address from `scan` (default: auto-detect)")
        p.add_argument("--timeout", type=float, default=8.0, help="scan time in seconds")

    def data_args(p) -> None:
        p.add_argument("--data-dir", type=Path, default=default_data_dir(),
                       help="where recordings are stored (default: $LIMITFUL_DATA, else ~/Limitful, "
                            "else an existing ~/PendantSync from before the rename)")

    p = sub.add_parser("scan", help="list pendants advertising nearby")
    p.add_argument("--timeout", type=float, default=8.0)
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("pair", help="pair this computer with the pendant (first use)")
    p.add_argument("--timeout", type=float, default=180.0, help="how long to wait, in seconds")
    p.set_defaults(func=_cmd_pair)

    p = sub.add_parser("retime", help="fix the timing of imported lines against their audio")
    data_args(p)
    p.add_argument("--minutes", type=float, default=0,
                   help="start no new recording after this many minutes (default: run to the end)")
    p.add_argument("--undo", action="store_true", help="put re-timed lines back at their imported times")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Whisper model (default: {DEFAULT_MODEL})")
    p.add_argument("--language", help="language code such as en (default: detect per file)")
    p.add_argument("--device", default="auto", help="auto, cpu, cuda, or mps (experimental Apple GPU support)")
    p.set_defaults(func=_cmd_retime)

    p = sub.add_parser("score-speakers",
                       help="check a hosted service's speaker labels against Limitless's (sends audio)")
    data_args(p)
    p.add_argument("--limit", type=int, default=5, help="how many imported conversations to send (default: 5)")
    p.add_argument("--language", help="language code such as en (default: let the service decide)")
    p.set_defaults(func=_cmd_score_speakers)

    p = sub.add_parser("index", help="build the search-by-meaning index, or try a search with it")
    data_args(p)
    p.add_argument("--find", metavar="WORDS", help="show the lines closest in meaning to WORDS, then exit")
    p.add_argument("--remove", action="store_true", help="delete the index (transcripts are untouched)")
    p.set_defaults(func=_cmd_index)

    for name, text in (("pause", "stop keeping audio: what the pendant records from now is deleted at sync"),
                       ("resume", "keep audio again after a pause")):
        p = sub.add_parser(name, help=text)
        data_args(p)
        p.set_defaults(func=_cmd_pause)

    p = sub.add_parser("unpair", help="make the pendant forget this computer (keeps its recordings)")
    p.add_argument("--timeout", type=float, default=8.0, help="scan time in seconds")
    p.set_defaults(func=_cmd_unpair)

    p = sub.add_parser("status", help="show the pendant's battery and storage; set its light")
    device_args(p)
    p.add_argument("--brightness", type=int, metavar="PERCENT",
                   help="set the brightness of the pendant's light, 0 to 100")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("sync", help="download stored audio from the pendant")
    device_args(p)
    data_args(p)
    p.add_argument("--ack", action="store_true",
                   help="free synced pages on the pendant (deletes its copy)")
    p.add_argument("--no-export", action="store_true", help="skip writing audio files")
    p.set_defaults(func=_cmd_sync)

    p = sub.add_parser("export", help="write downloaded pages as Ogg Opus files")
    data_args(p)
    p.set_defaults(func=_cmd_export)

    p = sub.add_parser("transcribe", help="turn downloaded audio into text (locally, unless Deepgram is chosen)")
    data_args(p)
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Whisper model (default: {DEFAULT_MODEL})")
    p.add_argument("--language", help="language code such as en (default: detect per file)")
    p.add_argument("--device", default="auto",
                   help="auto, cpu, cuda, or mps (experimental Apple GPU support)")
    p.add_argument("--redo", action="store_true",
                   help="transcribe pendant recordings again, replacing their transcripts")
    p.add_argument("--engine", choices=list(config.TRANSCRIBERS),
                   help="local Whisper, or Deepgram for speaker labels (default: the choice saved in Settings)")
    p.add_argument("--speakers", action="store_true",
                   help="experimental: guess speakers by voice (unreliable on real recordings)")
    p.set_defaults(func=_cmd_transcribe)

    p = sub.add_parser("speakers", help="label lines by speaker; list, name or merge speakers")
    data_args(p)
    p.add_argument("--rename", nargs=2, metavar=("ID", "NAME"),
                   help="name a speaker; using an existing name merges the two")
    p.add_argument("--reset", action="store_true",
                   help="forget all speakers and names, then group the pendant's lines again by voice")
    p.add_argument("--link-imported", action="store_true",
                   help="experimental: match imported speakers across conversations by voice")
    p.add_argument("--unlink-imported", action="store_true",
                   help="undo --link-imported for speakers that were never named")
    p.set_defaults(func=_cmd_speakers)

    def llm_args(p, what="what writes summaries") -> None:
        p.add_argument("--provider", choices=list(config.PROVIDERS),
                       help=f"{what} (default: the choice saved in Settings)")
        p.add_argument("--llm-model", help="model name for that provider")

    p = sub.add_parser("digest", help="title and summarise conversations, and suggest reminders")
    data_args(p)
    llm_args(p)
    p.add_argument("--redo", action="store_true",
                   help="discard existing summaries and reminders and write them again")
    p.add_argument("--days", action="store_true",
                   help="also write a recap for every finished day that lacks one, imported history "
                        "included (sends those days' titles and summaries to the provider)")
    p.set_defaults(func=_cmd_digest)

    p = sub.add_parser("mcp", help="let another app read the Lifelog: a Model Context Protocol server on stdin and stdout")
    data_args(p)
    p.add_argument("--config", action="store_true",
                   help="print what to paste into the app's MCP settings, and stop")
    p.add_argument("--triage", action="store_true",
                   help="also offer the two tools that let a scheduled Claude task write digests and tiers")
    p.set_defaults(func=_cmd_mcp)

    p = sub.add_parser("notes", help="write notes for a day's conversations: what each came to, and what is still to do")
    data_args(p)
    llm_args(p)
    p.add_argument("day", nargs="+", help="the day or days, as YYYY-MM-DD")
    p.add_argument("--redo", action="store_true", help="write them again for conversations that have notes")
    p.set_defaults(func=_cmd_notes)

    p = sub.add_parser("demo", help="make a folder of made-up conversations to try the Lifelog with")
    p.add_argument("--data-dir", type=Path, default=Path.home() / "Limitful-demo",
                   help="an empty folder to write them to (default: ~/Limitful-demo)")
    p.set_defaults(func=_cmd_demo)

    p = sub.add_parser("import", help="import a data export (.zip) from the Limitless service")
    p.add_argument("file", type=Path, help="the export zip")
    data_args(p)
    p.set_defaults(func=_cmd_import)

    p = sub.add_parser("add", help="add audio recorded elsewhere (a Voice Memo, any audio file)")
    p.add_argument("files", type=Path, nargs="+", help="audio files: .m4a, .mp3, .wav and most others")
    data_args(p)
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("voice-memos", help="add the Voice Memos this Mac has synced from your iPhone (macOS)")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only memos recorded on or after this day (default: all)")
    data_args(p)
    p.set_defaults(func=_cmd_voice_memos)

    p = sub.add_parser("ask", help="ask a question about what was said (uses Claude)")
    p.add_argument("question")
    data_args(p)
    llm_args(p, "what answers; only claude can")
    p.set_defaults(func=_cmd_ask)

    p = sub.add_parser("watch", help="keep syncing and transcribing in the background")
    data_args(p)
    p.add_argument("--interval", type=float, default=120.0,
                   help="seconds between sync attempts (default: 120)")
    p.add_argument("--keep", action="store_true",
                   help="do not free synced audio on the pendant")
    p.add_argument("--no-transcribe", action="store_true", help="download audio only")
    p.add_argument("--speakers", action="store_true",
                   help="experimental: guess speakers by voice (unreliable on real recordings)")
    llm_args(p)
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Whisper model (default: {DEFAULT_MODEL})")
    p.add_argument("--language", help="language code such as en (default: detect per file)")
    p.add_argument("--device", default="auto",
                   help="auto, cpu, cuda, or mps (experimental Apple GPU support)")
    p.set_defaults(func=_cmd_watch)

    p = sub.add_parser("app", help="run Limitful as a desktop app: the Lifelog in its own window, "
                                   "a tray icon, syncing, and updates")
    data_args(p)
    p.add_argument("--port", type=int, default=4747)
    p.add_argument("--sync-every", type=float, default=120,
                   help="sync the pendant by itself every this many seconds (0 is off; default 120)")
    p.add_argument("--background", action="store_true", help="start in the tray without opening the window")
    p.add_argument("--no-update", action="store_true", help="do not update from GitHub by itself")
    p.set_defaults(func=_cmd_app)

    p = sub.add_parser("serve", help="open the Lifelog: browse transcripts and play audio")
    data_args(p)
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 4747),
                   help="port to listen on (default: $PORT or 4747)")
    p.add_argument("--sync-every", type=float, default=0, metavar="SECONDS",
                   help="also sync the pendant by itself this often, freeing synced audio on it "
                        "(default: 0, only when asked)")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("autostart", help="start Limitful at login and keep the pendant synced (macOS and Windows)")
    p.add_argument("action", choices=["on", "off", "status"])
    data_args(p)
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 4747),
                   help="port the Lifelog listens on (default: $PORT or 4747)")
    p.set_defaults(func=_cmd_autostart)

    p = sub.add_parser("brain", help="export transcripts to a private git repo and encrypted audio to a synced folder")
    p.add_argument("action", choices=["setup", "keygen", "export", "status", "restore", "exclude", "include"])
    p.add_argument("ids", nargs="*", type=int, help="exclude/include: conversation ids to keep out of the repo, or let back in")
    data_args(p)
    p.add_argument("--repo", type=Path, help="setup: folder holding a clone of your private lifelog repo")
    p.add_argument("--audio", type=Path,
                   help="setup: folder a sync tool uploads, such as Google Drive; restore: the encrypted folder")
    p.add_argument("--recipient", help="setup: the age public key (age1...) from `brain keygen`")
    p.add_argument("--delay-hours", type=float, default=24,
                   help="setup: export a day this many hours after it ends (default: 24)")
    p.add_argument("--no-git", action="store_true", help="export: write the files but do not commit or push")
    p.add_argument("--to", type=Path, help="restore: where to write the decrypted recordings")
    p.set_defaults(func=_cmd_brain)

    args = parser.parse_args(argv)
    if args.log:
        # Started at login with no console (pythonw on Windows): keep a log instead.
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log = open(args.log, "a", encoding="utf-8", errors="backslashreplace", buffering=1)
        sys.stdout = sys.stderr = log
    try:
        return asyncio.run(args.func(args))
    except PendantError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except (BleakError, asyncio.TimeoutError, OSError) as err:
        # Bluetooth switched off or not allowed, a port in use, a file that cannot be written.
        from .web import pendant_advice

        message = str(err) or type(err).__name__
        advice = pendant_advice(message)
        print(f"error: {message}" + (f"\n{advice}" if advice else ""), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
