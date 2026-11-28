"""Long-running upkeep the Lifelog can start, watch and stop: re-timing imported
lines, building the search-by-meaning index, and grouping the voices of imported
conversations.

Both jobs work through a queue one item at a time and save as they go, so
stopping loses nothing and the next start carries on where this one left off.
They use the Lifelog's single background-job slot, so nothing else (a sync, an
import) can start while one runs, and they cannot start while something else is.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .web import Library


def _stop_flag(library: "Library") -> threading.Event:
    flag = getattr(library, "stop_requested", None)
    if flag is None:
        flag = library.stop_requested = threading.Event()
    return flag


def status(library: "Library") -> dict:
    """What is left to do, for the Settings page. Touches no model."""
    from . import search

    def read(store):
        pending = store.pending_retime()
        return {
            "retime": {"recordings": len(pending),
                       "hours": round(sum(duration for _p, _s, duration in pending) / 3_600_000, 1)},
            "index": search.Index(library.data_dir).progress(store),
        }

    return library._with_store(read)


def _begin(library: "Library", what: str, work: Callable[[], str]) -> None:
    """Claim the job slot and run `work` in the background. It returns the closing note."""
    with library._lock:
        if library.job["running"]:
            raise RuntimeError("Something is already running. Wait for it to finish.")
        library.job.update(running=True, what=what, done=0, total=0, error=None, note=None, stoppable=True)
    _stop_flag(library).clear()

    def run() -> None:
        try:
            library.job["note"] = work()
        except Exception as err:  # shown in the page; finished items are already saved
            library.job["error"] = f"{what} stopped: {err}"
        finally:
            library.job.update(running=False, what="", stoppable=False)

    threading.Thread(target=run, daemon=True).start()


def stop(library: "Library") -> None:
    """Ask the running upkeep job to finish its current item and stop."""
    if not (library.job["running"] and library.job.get("stoppable")):
        raise ValueError("nothing that can be stopped is running")
    _stop_flag(library).set()
    library.job["what"] = library.job["what"].split(" (")[0] + " (stopping after this one)"


def start_retime(library: "Library") -> None:
    """Move imported lines to where their words are heard, one recording at a time."""
    from . import transcribe
    from .store import Store

    def work() -> str:
        store = Store(library.data_dir)
        try:
            pending = store.pending_retime()
            if not pending:
                return "Every imported recording is already re-timed."
            library.job.update(total=len(pending))
            # Always local Whisper: this needs word timings from the audio itself.
            whisper = transcribe.WhisperTranscriber()
            done = lines = 0
            for path, _start, _duration in pending:
                if _stop_flag(library).is_set():
                    break
                _found, of = store.retime_recording(path, whisper.words(library.data_dir / path))
                done, lines = done + 1, lines + of
                library.job.update(done=done)
            if done:
                store.rewrite_all_transcripts()
            left = len(pending) - done
            return (f"Re-timed {lines} line{'s' if lines != 1 else ''} "
                    f"in {done} recording{'s' if done != 1 else ''}."
                    + (f" {left} left; start again to continue." if left else " All done."))
        finally:
            store.close()

    _begin(library, "Fixing the timing of imported lines", work)


def start_index(library: "Library") -> None:
    """Build the search-by-meaning index for lines that are not in it yet."""
    from . import search
    from .store import Store

    def work() -> str:
        store = Store(library.data_dir)
        try:
            index = search.Index(library.data_dir)
            added = index.build(
                store, search.make_embedder(),
                on_progress=lambda done, total: library.job.update(done=done, total=total),
                should_stop=_stop_flag(library).is_set)
            left = index.progress(store)["left"]
            return (f"Indexed {added} line{'s' if added != 1 else ''} for search by meaning."
                    + (f" {left} left; start again to continue." if left else " All done."))
        finally:
            store.close()

    _begin(library, "Building the search index", work)


def start_group_voices(library: "Library", since_ms: int | None, until_ms: int | None) -> None:
    """Group the per-conversation speakers of imported conversations in a date window
    into voices, so the owner can name the biggest ones. Names already given stay."""
    from .speakers import TitanetEmbedder, load_audio
    from .store import Store

    def work() -> str:
        store = Store(library.data_dir)
        try:
            linked, _conflicts = store.link_imported_speakers(
                TitanetEmbedder(), load_audio,
                lambda done, total: library.job.update(done=done, total=total),
                since_ms=since_ms, until_ms=until_ms, should_stop=_stop_flag(library).is_set)
            if not linked:
                return "No new voices to group in those dates."
            voices = len(store.unnamed_voices(limit=1000))
            return (f"Grouped {linked} speaker{'s' if linked != 1 else ''} from imported conversations. "
                    f"{voices} voice{'s are' if voices != 1 else ' is'} waiting for a name on the People page.")
        finally:
            store.close()

    _begin(library, "Grouping voices from imported recordings", work)


def ungroup_voices(library: "Library") -> int:
    """Undo voice grouping for imported speakers nobody named. Named ones are kept."""
    with library._lock:
        if library.job["running"]:
            raise RuntimeError("Something is already running. Wait for it to finish.")
    return library._with_store(lambda store: store.unlink_imported_speakers())
