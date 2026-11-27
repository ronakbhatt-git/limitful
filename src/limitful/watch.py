"""Background sync: keep pulling audio off the pendant whenever it is in range."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from bleak.exc import BleakError

from . import client
from .digest import DigestError, Summarizer
from .hosted import HostedError
from .speakers import Embedder, load_audio
from .store import Store
from .transcribe import Transcriber


@dataclass(frozen=True)
class CycleResult:
    found: bool
    pages_new: int = 0
    audio_files: int = 0
    lines: int = 0
    error: str | None = None
    pages_discarded: int = 0


def _stamp(message: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')}  {message}", flush=True)


async def run_cycle(
    store: Store,
    *,
    ack: bool,
    get_transcriber: Callable[[], Transcriber] | None,
    get_embedder: Callable[[], Embedder] | None = None,
    summarizer: Summarizer | None = None,
    scan_timeout: float = 5.0,
    on_stage: Callable[[str], None] = lambda stage: None,
    on_audio: Callable[[int, int], None] | None = None,
    on_indexing: Callable[[int, int], None] | None = None,
) -> CycleResult:
    """One pass: find the pendant, download, export audio, transcribe."""
    pendants = await client.scan(scan_timeout)
    device = pendants[0][0] if pendants else await client.remembered()
    if device is None:
        return CycleResult(found=False)
    on_stage("Downloading from the pendant")
    try:
        # Pairing is a one-off, interactive step; do not wait a minute for it here.
        async with client.Pendant(device, pairing_timeout=10) as pendant:
            result = await pendant.sync(store, ack=ack)
    except (client.PendantError, BleakError, asyncio.TimeoutError):
        if pendants:
            raise
        return CycleResult(found=False)  # the remembered pendant is out of range
    segments = store.export_audio()
    lines = 0
    if get_transcriber is not None and store.pending_transcription():
        on_stage("Transcribing")
        # Whisper is CPU-heavy and blocking; keep the event loop free.
        try:
            lines, _files = await asyncio.to_thread(
                store.transcribe_pending, get_transcriber(), None, on_audio, on_indexing)
        except HostedError as err:
            # The audio is safe on disk; transcription is retried next cycle.
            return CycleResult(True, result.pages_new, len(segments), 0,
                               error=f"transcription: {err}", pages_discarded=result.pages_discarded)
        if lines and get_embedder is not None:
            await asyncio.to_thread(store.label_speakers, get_embedder(), load_audio)
    if summarizer is not None:
        on_stage("Summarising conversations")
        try:
            await asyncio.to_thread(store.digest_pending, summarizer)
        except DigestError as err:
            # Audio and transcripts are safe; summaries are retried next cycle.
            return CycleResult(True, result.pages_new, len(segments), lines,
                               error=f"summaries: {err}", pages_discarded=result.pages_discarded)
    return CycleResult(True, result.pages_new, len(segments), lines,
                       pages_discarded=result.pages_discarded)


async def watch(
    store: Store,
    *,
    interval: float,
    ack: bool,
    get_transcriber: Callable[[], Transcriber] | None,
    get_embedder: Callable[[], Embedder] | None = None,
    summarizer: Summarizer | None = None,
    log: Callable[[str], None] = _stamp,
    cycles: int | None = None,
) -> None:
    """Run cycles forever (or `cycles` times), surviving any single failure."""
    was_found: bool | None = None
    done = 0
    while cycles is None or done < cycles:
        done += 1
        try:
            result = await run_cycle(store, ack=ack, get_transcriber=get_transcriber,
                                     get_embedder=get_embedder, summarizer=summarizer)
        except Exception as err:  # a busy database or a failed download must not end the loop
            result = CycleResult(found=True, error=str(err) or type(err).__name__)
        if result.error and result.error.startswith(("summaries:", "transcription:")):
            log(f"synced, but {result.error}; will retry")
        elif result.error:
            log(f"sync failed, will retry: {result.error}")
        elif not result.found:
            if was_found is not False:
                log("pendant not in range; waiting")
        elif result.pages_new or result.lines or result.pages_discarded:
            log(f"synced {result.pages_new} new page(s), {result.audio_files} audio file(s), "
                f"{result.lines} transcript line(s)"
                + (f"; deleted {result.pages_discarded} page(s) recorded while paused"
                   if result.pages_discarded else ""))
        elif was_found is not True:
            log("pendant connected; nothing new")
        was_found = result.found
        if cycles is None or done < cycles:
            await asyncio.sleep(interval)
