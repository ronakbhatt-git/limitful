"""Delete a stretch of the Lifelog: what was said in it, and the sound of it.

Deleting a conversation has to take everything with it, or it is not gone:
its lines, title, notes, sections and reminders, its entries in search, and
the audio. A recording that lies wholly inside the stretch is removed; one
that runs past it has that part replaced with silence, so the rest keeps its
timing. Pendant audio is also dropped from the pages it was downloaded as,
and the stretch is remembered, so syncing again cannot bring it back.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .store import Store

MAX_SPAN_MS = 24 * 60 * 60 * 1000  # one stretch is never more than a day
MAX_SPANS = 500  # one request never deletes more stretches than this


@dataclass(frozen=True)
class Forgotten:
    lines: int = 0
    conversations: int = 0
    recordings_removed: int = 0
    recordings_silenced: int = 0


def silence(path: Path, from_s: float, to_s: float) -> None:
    """Replace part of an audio file with silence, keeping its length. Re-encodes it as Ogg Opus."""
    import av
    import numpy as np

    target = path.with_name(f".silencing-{path.name}")
    try:
        with av.open(str(path)) as src:
            stream = next(s for s in src.streams if s.type == "audio")
            with av.open(str(target), "w", format="ogg") as out:
                encoder = out.add_stream("libopus", rate=48000)
                encoder.layout = "mono"
                encoder.bit_rate = 32000
                resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
                lo, hi, at = int(from_s * 48000), int(to_s * 48000), 0

                def write(piece) -> None:
                    nonlocal at
                    first, last = max(lo, at), min(hi, at + piece.samples)
                    if first < last:
                        samples = piece.to_ndarray().copy()
                        samples[:, first - at : last - at] = 0
                        quiet = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples), format="s16", layout="mono")
                        quiet.sample_rate, quiet.pts, quiet.time_base = piece.sample_rate, piece.pts, piece.time_base
                        piece = quiet
                    at += piece.samples
                    for packet in encoder.encode(piece):
                        out.mux(packet)

                for frame in src.decode(stream):
                    for piece in resampler.resample(frame):
                        write(piece)
                for piece in resampler.resample(None):
                    write(piece)
                for packet in encoder.encode(None):
                    out.mux(packet)
        os.replace(target, path)
    finally:
        target.unlink(missing_ok=True)


def forget(store: Store, start_ms: int, end_ms: int) -> Forgotten:
    """Delete everything said from `start_ms` to `end_ms`, both included, and its audio."""
    return forget_all(store, [(start_ms, end_ms)])


def _forget_chats_citing(store: Store, line_ids: set[int]) -> int:
    """Delete saved Ask threads whose answers quoted any of these lines. Returns how many."""
    if not line_ids:
        return 0
    chats = set()
    for chat_id, lines in store.db.execute(
            "SELECT chat_id, lines FROM chat_messages WHERE lines IS NOT NULL").fetchall():
        try:
            cited = {int(key) for key in json.loads(lines) or {}}
        except (ValueError, TypeError):
            continue
        if cited & line_ids:
            chats.add(chat_id)
    for chat_id in chats:
        store.db.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        store.db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    store.db.commit()
    return len(chats)


def forget_all(store: Store, spans: list[tuple[int, int]]) -> Forgotten:
    """Delete several stretches, each as `forget` does, and say what went in total.

    Every stretch is checked before any is touched. If one then fails part way (an audio
    file that cannot be rewritten), the ones before it stay deleted and the error is raised.
    """
    if len(spans) > MAX_SPANS:
        raise ValueError(f"that is more than {MAX_SPANS} at once; delete fewer at a time")
    for start_ms, end_ms in spans:
        if end_ms < start_ms:
            raise ValueError("the end is before the start")
        if end_ms - start_ms > MAX_SPAN_MS:
            raise ValueError("that is more than a day; delete it a day at a time")
    total, days = Forgotten(), set()
    gone_lines: set[int] = set()
    try:
        for start_ms, end_ms in sorted(spans):
            gone_lines.update(uid for (uid,) in store.db.execute(
                "SELECT id FROM utterances WHERE start_ms BETWEEN ? AND ?", (start_ms, end_ms)))
            gone = _forget_span(store, start_ms, end_ms, days)
            total = Forgotten(total.lines + gone.lines, total.conversations + gone.conversations,
                              total.recordings_removed + gone.recordings_removed,
                              total.recordings_silenced + gone.recordings_silenced)
    finally:
        _forget_chats_citing(store, gone_lines)
        # Deleted rows stay readable in the database file and its log until
        # overwritten; secure_delete zeroes them, and the checkpoint empties the log.
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for each in sorted(days):
            store.write_transcript(each)
        if days:
            from . import search

            search.update_if_built(store)  # drops the deleted lines from search by meaning
    return total


def _forget_span(store: Store, start_ms: int, end_ms: int, days: set[str]) -> Forgotten:
    """Delete one stretch from the store and the disk, adding the days it touched to `days`."""
    db = store.db
    day = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")  # noqa: E731

    # The audio first: if a file cannot be rewritten, nothing else has been touched.
    removed, silenced, from_pendant = [], 0, False
    for path, seg_start, duration, first_page, last_page in db.execute(
            "SELECT path, start_ms, duration_ms, first_page, last_page FROM segments "
            "WHERE start_ms <= ? AND start_ms + duration_ms >= ?", (end_ms, start_ms)).fetchall():
        inside = seg_start >= start_ms and seg_start + duration <= end_ms
        file = store.data_dir / path
        if inside:
            removed.append((path, file))
        elif file.is_file():
            silence(file, max(0, start_ms - seg_start) / 1000, min(duration, end_ms - seg_start) / 1000)
            silenced += 1
        if first_page >= 0:
            # A pendant recording is also held as the pages it arrived in.
            from_pendant = True
            if inside:
                db.execute("DELETE FROM pages WHERE id BETWEEN ? AND ?", (first_page, last_page))
            else:
                db.execute("DELETE FROM pages WHERE id BETWEEN ? AND ? AND timestamp_ms BETWEEN ? AND ?",
                           (first_page, last_page, start_ms, end_ms))

    talks = [row[0] for row in db.execute(
        "SELECT id FROM conversations WHERE start_ms >= ? AND end_ms <= ?", (start_ms, end_ms))]
    for talk in talks:
        db.execute("DELETE FROM reminders WHERE conversation_id = ?", (talk,))
        db.execute("DELETE FROM sections WHERE conversation_id = ?", (talk,))
        db.execute("DELETE FROM conversations WHERE id = ?", (talk,))
    # A reminder kept from another conversation may point at a line that is going.
    db.execute("UPDATE reminders SET utterance_id = NULL WHERE utterance_id IN "
               "(SELECT id FROM utterances WHERE start_ms BETWEEN ? AND ?)", (start_ms, end_ms))
    lines = db.execute("DELETE FROM utterances WHERE start_ms BETWEEN ? AND ?", (start_ms, end_ms)).rowcount
    for path, _file in removed:
        db.execute("DELETE FROM transcriptions WHERE segment = ?", (path,))
        db.execute("DELETE FROM added WHERE path = ?", (path,))  # its key stays in `imports`: it is not added again
        db.execute("DELETE FROM segments WHERE path = ?", (path,))
    if from_pendant:
        # Sync keeps nothing recorded during a pause; this stretch is now one, for good.
        db.execute("INSERT INTO pauses (start_ms, end_ms) VALUES (?, ?)", (start_ms, end_ms))
    db.execute("DELETE FROM day_recaps WHERE day IN (?, ?)", (day(start_ms), day(end_ms)))  # it may say what was deleted
    db.commit()
    days.update((day(start_ms), day(end_ms)))
    for _path, file in removed:
        file.unlink(missing_ok=True)
    return Forgotten(lines, len(talks), len(removed), silenced)
