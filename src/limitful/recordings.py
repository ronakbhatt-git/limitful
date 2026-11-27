"""Add a recording made somewhere else to the Lifelog.

A Voice Memo from a phone, a meeting recording, any audio file. It is
re-encoded as Ogg Opus, the format every other recording here is kept in,
filed under the moment it was made, and then transcribed like the rest.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .store import Store

# What a browser may send. Anything the decoder cannot read is refused when it is opened.
SUFFIXES = (".m4a", ".mp3", ".wav", ".aac", ".mp4", ".mov", ".caf", ".aif", ".aiff",
            ".flac", ".ogg", ".opus", ".webm", ".qta", ".amr", ".3gp", ".wma")

# "20261114 093012-1A2B3C4D.m4a" is how Voice Memos names its files on a Mac; other
# recorders write the date and time the same way with different punctuation.
_NAMED = re.compile(r"(?<!\d)(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)[ _T-]+(\d\d)[-_.:h]?(\d\d)[-_.:m]?(\d\d)(?!\d)")
_EARLIEST_MS = 946_684_800_000  # 1 January 2000: older than this is an unset clock


@dataclass(frozen=True)
class Added:
    path: str          # relative to the data folder; "" when nothing was added
    start_ms: int
    duration_ms: int
    new: bool          # False when this exact file was added before
    dated_by: str      # "name", "file", "modified" or "now"


class NotAudio(ValueError):
    """The file could not be read as audio."""


def _from_name(name: str) -> int | None:
    found = _NAMED.search(name)
    if not found:
        return None
    try:
        return int(datetime(*(int(part) for part in found.groups())).timestamp() * 1000)
    except ValueError:
        return None


def _from_metadata(value: str | None) -> int | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp() * 1000)


def recorded_at(name: str, created: str | None, modified_ms: int | None, duration_ms: int,
                now_ms: int | None = None) -> tuple[int, str]:
    """When a recording began, and what that was read from.

    The time in its name wins, then the time written inside the file, then the
    time the file was last changed (a recording is last changed when it ends).
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    for start, how in ((_from_name(name), "name"), (_from_metadata(created), "file")):
        if start is not None and _EARLIEST_MS <= start <= now_ms:
            return start, how
    if modified_ms is not None and _EARLIEST_MS <= modified_ms - duration_ms and modified_ms <= now_ms + 60_000:
        return modified_ms - duration_ms, "modified"
    return now_ms - duration_ms, "now"


def to_ogg(source: Path, target: Path) -> tuple[int, str | None]:
    """Re-encode any audio file as mono Ogg Opus. Returns (its length in ms, the creation time it carries)."""
    import av

    try:
        with av.open(str(source)) as src:
            stream = next((s for s in src.streams if s.type == "audio"), None)
            if stream is None:
                raise NotAudio("there is no audio in that file")
            created = src.metadata.get("creation_time") or stream.metadata.get("creation_time")
            seconds = 0.0
            with av.open(str(target), "w", format="ogg") as out:
                encoder = out.add_stream("libopus", rate=48000)
                encoder.layout = "mono"
                encoder.bit_rate = 32000  # speech; about 14 MB an hour
                resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
                for frame in src.decode(stream):
                    for piece in resampler.resample(frame):
                        seconds += piece.samples / 48000
                        for packet in encoder.encode(piece):
                            out.mux(packet)
                for piece in resampler.resample(None):
                    seconds += piece.samples / 48000
                    for packet in encoder.encode(piece):
                        out.mux(packet)
                for packet in encoder.encode(None):
                    out.mux(packet)
    except NotAudio:
        target.unlink(missing_ok=True)
        raise
    except (av.error.FFmpegError, ValueError, IndexError) as err:
        target.unlink(missing_ok=True)
        raise NotAudio("that file could not be read as audio") from err
    if seconds < 0.1:
        target.unlink(missing_ok=True)
        raise NotAudio("there is no audio in that file")
    return round(seconds * 1000), created


def add_recording(store: Store, source: Path, name: str, modified_ms: int | None = None,
                  now_ms: int | None = None, start_ms: int | None = None, origin: str = "file",
                  also_key: str | None = None, title: str | None = None) -> Added:
    """File one recording in the data folder, ready to be transcribed. Adding the same file twice does nothing.

    `start_ms` is for a caller that knows when it began; `also_key` is a second
    name the recording is remembered by (the id Voice Memos gives it).
    """
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    key = f"recording:{digest.hexdigest()}"
    known = [key] + ([also_key] if also_key else [])
    if store.db.execute(
            f"SELECT 1 FROM imports WHERE key IN ({','.join('?' * len(known))})", known).fetchone():
        for each in known:  # seen under one name: remember the other too
            store.db.execute("INSERT OR IGNORE INTO imports VALUES (?)", (each,))
        store.db.commit()
        return Added("", 0, 0, False, "")

    audio = store.data_dir / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    scratch = audio / f".adding-{digest.hexdigest()[:12]}.ogg"
    duration_ms, created = to_ogg(source, scratch)
    if start_ms is not None:
        dated_by = "given"
    else:
        start_ms, dated_by = recorded_at(name, created, modified_ms, duration_ms, now_ms)
    local = datetime.fromtimestamp(start_ms / 1000)
    # A name we chose: nothing from the upload's own name reaches the disk.
    rel = Path("audio") / local.strftime("%Y-%m-%d") / f"added_{local.strftime('%H-%M-%S')}_{digest.hexdigest()[:8]}.ogg"
    target = store.data_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch.replace(target)
    store.db.execute(
        "INSERT OR REPLACE INTO segments (path, start_ms, duration_ms, first_page, last_page) "
        "VALUES (?, ?, ?, -1, -1)", (rel.as_posix(), start_ms, duration_ms))
    for each in known:
        store.db.execute("INSERT OR IGNORE INTO imports VALUES (?)", (each,))
    store.db.execute("INSERT OR REPLACE INTO added (path, name, source, key) VALUES (?, ?, ?, ?)",
                     (rel.as_posix(), (title or Path(name).stem)[:200] or "Recording", origin, also_key or key))
    store.db.commit()
    return Added(rel.as_posix(), start_ms, duration_ms, True, dated_by)


def _is_added(store: Store, path: str) -> tuple[int, int]:
    row = store.db.execute(
        "SELECT start_ms, duration_ms FROM segments WHERE path = ? AND first_page < 0 "
        "AND path LIKE 'audio/%/added_%'", (path,)).fetchone()
    if not row:
        raise ValueError("that is not a recording that was added")
    return row


def added_recordings(store: Store) -> list[dict]:
    """Every recording added from a file or from Voice Memos, newest first."""
    rows = store.db.execute(
        "SELECT s.path, s.start_ms, s.duration_ms, a.name, a.source, "
        "(SELECT COUNT(*) FROM utterances u WHERE u.segment = s.path), "
        "EXISTS (SELECT 1 FROM transcriptions t WHERE t.segment = s.path) "
        "FROM segments s LEFT JOIN added a ON a.path = s.path "
        "WHERE s.first_page < 0 AND s.path LIKE 'audio/%/added_%' ORDER BY s.start_ms DESC").fetchall()
    return [{"path": path, "start_ms": start, "duration_ms": duration, "name": name or "Recording",
             "source": source or "file", "lines": lines, "transcribed": bool(done)}
            for path, start, duration, name, source, lines, done in rows]


def _own_conversations(store: Store, path: str) -> list[int]:
    """Conversations made only of this recording's lines."""
    return [row[0] for row in store.db.execute(
        "SELECT c.id FROM conversations c WHERE "
        "EXISTS (SELECT 1 FROM utterances u WHERE u.segment = ? AND u.start_ms BETWEEN c.start_ms AND c.end_ms) "
        "AND NOT EXISTS (SELECT 1 FROM utterances u WHERE u.segment != ? "
        "AND u.start_ms BETWEEN c.start_ms AND c.end_ms)", (path, path))]


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def remove_recording(store: Store, path: str) -> None:
    """Delete an added recording: its audio, its lines and the conversations made from it alone.

    One from Voice Memos stays remembered, so the next sync does not bring it
    back. One added from a file is forgotten, so the file can be added again.
    """
    start_ms, _duration = _is_added(store, path)
    days = {_day(start_ms)} | {_day(row[0]) for row in store.db.execute(
        "SELECT start_ms FROM utterances WHERE segment = ?", (path,))}
    for conversation in _own_conversations(store, path):
        store.db.execute("DELETE FROM reminders WHERE conversation_id = ?", (conversation,))
        store.db.execute("DELETE FROM sections WHERE conversation_id = ?", (conversation,))
        store.db.execute("DELETE FROM conversations WHERE id = ?", (conversation,))
    store.db.execute(
        "UPDATE reminders SET utterance_id = NULL WHERE utterance_id IN "
        "(SELECT id FROM utterances WHERE segment = ?)", (path,))
    store.db.execute("DELETE FROM utterances WHERE segment = ?", (path,))
    store.db.execute("DELETE FROM transcriptions WHERE segment = ?", (path,))
    store.db.execute("DELETE FROM segments WHERE path = ?", (path,))
    store.db.execute("DELETE FROM imports WHERE key IN "
                     "(SELECT key FROM added WHERE path = ? AND source = 'file')", (path,))
    store.db.execute("DELETE FROM added WHERE path = ?", (path,))
    store.db.commit()
    (store.data_dir / path).unlink(missing_ok=True)
    for day in sorted(days):
        store.write_transcript(day)
    from . import search

    search.update_if_built(store)  # drops the removed lines from search by meaning


def move_recording(store: Store, path: str, start_ms: int) -> str:
    """Say when an added recording really began. Its lines, and the conversations made
    from it alone, move with it and keep their titles and summaries. Returns its new path."""
    old_start, _duration = _is_added(store, path)
    if not _EARLIEST_MS <= start_ms <= int(time.time() * 1000):
        raise ValueError("that time is in the future, or too long ago")
    shift = start_ms - old_start
    if not shift:
        return path
    local = datetime.fromtimestamp(start_ms / 1000)
    rel = (Path("audio") / local.strftime("%Y-%m-%d")
           / f"added_{local.strftime('%H-%M-%S')}_{Path(path).stem.rsplit('_', 1)[-1]}.ogg").as_posix()
    if rel != path and store.db.execute("SELECT 1 FROM segments WHERE path = ?", (rel,)).fetchone():
        raise ValueError("another recording is already filed at that moment")
    days = {_day(old_start), _day(start_ms)}
    for conversation in _own_conversations(store, path):
        store.db.execute("UPDATE conversations SET start_ms = start_ms + ?, end_ms = end_ms + ? WHERE id = ?",
                         (shift, shift, conversation))
        store.db.execute("UPDATE sections SET start_ms = start_ms + ? WHERE conversation_id = ?",
                         (shift, conversation))
    store.db.execute("UPDATE utterances SET start_ms = start_ms + ?, end_ms = end_ms + ?, segment = ? "
                     "WHERE segment = ?", (shift, shift, rel, path))
    store.db.execute("UPDATE transcriptions SET segment = ? WHERE segment = ?", (rel, path))
    store.db.execute("UPDATE segments SET path = ?, start_ms = ? WHERE path = ?", (rel, start_ms, path))
    store.db.execute("UPDATE added SET path = ? WHERE path = ?", (rel, path))
    if rel != path:
        (store.data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (store.data_dir / path).replace(store.data_dir / rel)
    store.db.commit()
    for day in sorted(days):
        store.write_transcript(day)
    return rel
