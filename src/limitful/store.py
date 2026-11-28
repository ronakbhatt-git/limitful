"""Local storage: raw flash pages in SQLite, audio exported as Ogg Opus files.

Raw page bytes are the source of truth. Audio files are derived from them, so a
bug in audio extraction can be fixed and the export re-run without the pendant.

Pages are identified by a hash of their content, not by their index: the
pendant restarts its page and session counters from zero when it is reset
(entering pairing mode does this), so an index can be reused for new audio.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import numpy as np

from .digest import DigestDeclined, Summarizer
from .ogg import packet_samples, write_ogg_opus
from .protocol import MIN_VALID_TIMESTAMP_MS, FlashPage, extract_opus_frames
from .speakers import MATCH_THRESHOLD, MIN_FINGERPRINTS, MIN_SPEAKER_MS, SAMPLE_RATE, Embedder
from .transcribe import Transcriber

# A jump this large between consecutive pages starts a new audio file.
SEGMENT_GAP_MS = 3000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id           INTEGER PRIMARY KEY,   -- arrival order = recording order
    hash         BLOB    NOT NULL UNIQUE,
    session      INTEGER NOT NULL,
    idx          INTEGER NOT NULL,
    timestamp_ms INTEGER NOT NULL,      -- pendant clock; not valid until it is set
    uptime_ms    INTEGER NOT NULL,      -- time since the pendant booted
    received_ms  INTEGER NOT NULL,
    raw          BLOB    NOT NULL,
    exported     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS segments (
    path        TEXT PRIMARY KEY,
    start_ms    INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    first_page  INTEGER NOT NULL,
    last_page   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transcriptions (
    segment        TEXT PRIMARY KEY,    -- segments.path
    engine         TEXT NOT NULL,
    transcribed_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS utterances (
    id       INTEGER PRIMARY KEY,
    segment  TEXT    NOT NULL,          -- segments.path
    start_ms INTEGER NOT NULL,          -- wall-clock time
    end_ms   INTEGER NOT NULL,
    text     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS utterances_start ON utterances (start_ms);
CREATE TABLE IF NOT EXISTS speakers (
    id        INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL,
    voice_sum BLOB    NOT NULL,         -- no longer read; kept so older databases still open
    lines     INTEGER NOT NULL          -- likewise
);
CREATE TABLE IF NOT EXISTS imports (
    key TEXT PRIMARY KEY               -- one row per imported file, so imports can be re-run
);
CREATE TABLE IF NOT EXISTS conversations (
    id       INTEGER PRIMARY KEY,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER NOT NULL,
    status   TEXT    NOT NULL,          -- pending, done, short (too little said), skipped
    title    TEXT,
    summary  TEXT,                      -- JSON list of sentences
    engine   TEXT
);
CREATE INDEX IF NOT EXISTS conversations_start ON conversations (start_ms);
CREATE TABLE IF NOT EXISTS sections (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL,
    start_ms        INTEGER NOT NULL,   -- the first line under this heading
    level           INTEGER NOT NULL,   -- 2 for a section, 3 for a subsection
    title           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS sections_conversation ON sections (conversation_id);
CREATE TABLE IF NOT EXISTS pauses (
    id       INTEGER PRIMARY KEY,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER                   -- NULL while still paused
);
CREATE TABLE IF NOT EXISTS chats (
    id         INTEGER PRIMARY KEY,
    title      TEXT    NOT NULL,        -- the first question, shortened
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL,        -- user or assistant
    content    TEXT    NOT NULL,
    lines      TEXT,                    -- JSON: where each cited line is, for an answer
    created_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages (chat_id);
CREATE TABLE IF NOT EXISTS day_recaps (
    day       TEXT PRIMARY KEY,            -- local date, YYYY-MM-DD
    headline  TEXT NOT NULL,
    points    TEXT NOT NULL,               -- JSON list
    engine    TEXT NOT NULL,
    signature TEXT NOT NULL,               -- of the conversations it was written from
    made_ms   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS added (
    path   TEXT PRIMARY KEY,           -- segments.path of a recording made elsewhere
    name   TEXT NOT NULL,              -- its file name, or the title it had in Voice Memos
    source TEXT NOT NULL,              -- file or voicememos
    key    TEXT NOT NULL               -- imports.key: what stops it being added twice
);
CREATE TABLE IF NOT EXISTS reminders (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL,
    text            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'suggested',  -- suggested, accepted, done, dismissed
    created_ms      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS name_votes (     -- clues to whose voice is whose (see naming.py)
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER,                -- NULL for the owner's own "not them"
    utterance_id    INTEGER,                -- the line the clue is in
    speaker_id      INTEGER,                -- only for the owner's "not them"
    name            TEXT    NOT NULL,
    kind            TEXT    NOT NULL,       -- introduction, addressed, mentioned or rejected
    created_ms      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS name_votes_conversation ON name_votes (conversation_id);
"""

# A silence this long ends a conversation.
CONVERSATION_GAP_MS = 5 * 60 * 1000
# Conversations with fewer words than this are not worth summarising.
MIN_DIGEST_WORDS = 25
# A day needs this many summarised conversations before a recap says anything new.
MIN_RECAP_CONVERSATIONS = 2
REMINDER_STATUSES = ("suggested", "accepted", "done", "dismissed")

# Columns added after the first release; applied to existing databases on open.
_MIGRATIONS = [
    ("utterances", "embedding", "BLOB"),       # voice fingerprint, float32
    ("utterances", "speaker_id", "INTEGER"),
    ("utterances", "source", "TEXT"),          # imported conversation this line came from
    ("utterances", "source_speaker", "TEXT"),  # that import's own label for who spoke
    ("reminders", "utterance_id", "INTEGER"),  # the line a reminder came from, when known
    ("utterances", "orig_start_ms", "INTEGER"),  # an imported line's own timestamp, once re-timed
    ("segments", "retimed", "INTEGER"),          # 1 once its imported lines have been re-timed
    ("conversations", "source", "TEXT"),         # the import it came from, once known
    ("sections", "orig_start_ms", "INTEGER"),    # a heading's own position, once re-timed
    # Which lines share a set of per-conversation speaker numbers: an imported
    # conversation, or one request to a hosted transcription service.
    ("utterances", "speaker_group", "TEXT"),
    # A conversation's notes, once written: JSON [{"text", "line": utterance id or null}].
    ("conversations", "notes", "TEXT"),
    # Triage (see triage.py): keep, text or hold; JSON list of tags; 1 if sensitive.
    ("conversations", "tier", "TEXT"),
    ("conversations", "tags", "TEXT"),
    ("conversations", "sensitive", "INTEGER"),
    ("conversations", "triaged_ms", "INTEGER"),
    # "clues" when the name came from what was said (naming.py); NULL when the owner typed it.
    ("speakers", "named_by", "TEXT"),
]

# A hosted service's speaker numbers only hold within one request, so recordings
# are sent a conversation at a time. This caps how much goes in one request.
SESSION_MAX_MS = 3 * 60 * 60 * 1000

# Imported timestamps are rounded to the second and run about a second late.
# Re-timing looks for each line's words in a local transcript of the same audio.
RETIME_MIN_WORDS = 4       # shorter lines cannot be located reliably on their own
RETIME_MATCH = 0.75        # how alike the words must be to count as found
RETIME_WINDOW_S = 10.0     # how far from its timestamp a line may really be
RETIME_TYPICAL_LATE_S = 1.0  # imported timestamps run about this late (measured)


def _plain_words(text: str) -> list[str]:
    return "".join(ch.lower() if ch.isalnum() or ch == "'" else " " for ch in text).split()


def locate_lines(lines: list[tuple[float, str]], words: list[tuple[float, str]]) -> list[float | None]:
    """Where each line (its claimed second, its text) really starts, given the
    words heard in the recording and when. None where it could not be found."""
    import bisect
    import difflib

    heard = [(at, plain[0]) for at, word in words for plain in [_plain_words(word)] if plain]
    times = [at for at, _ in heard]
    found: list[float | None] = []
    for claimed, text in lines:
        want = _plain_words(text)
        candidates: list[tuple[float, float]] = []  # (how alike, when)
        if len(want) >= RETIME_MIN_WORDS:
            lo = bisect.bisect_left(times, claimed - RETIME_WINDOW_S)
            hi = bisect.bisect_right(times, claimed + RETIME_WINDOW_S)
            for i in range(lo, hi):
                got = [w for _, w in heard[i : i + len(want)]]
                candidates.append(
                    (difflib.SequenceMatcher(None, want, got, autojunk=False).ratio(), heard[i][0]))
        top = max((score for score, _at in candidates), default=0.0)
        if top < RETIME_MATCH:
            found.append(None)
            continue
        # Several places can match about equally (a repeated phrase, or the same
        # words one position along). Take the one nearest where the line is
        # expected: taking the first skews every correction early.
        expected = claimed - RETIME_TYPICAL_LATE_S
        close = [at for score, at in candidates if score >= top - 0.05]
        found.append(min(close, key=lambda at: abs(at - expected)))
    return found

_COMMON_WORDS = frozenset("""
a about after again all also an and any are as ask at be been before but buy by call can check
could did do does don for from get go going got had has have he her here him his how i if in
into is it its just know let like look make me more my need no not now of on one or our out
over said say see she should so some take tell than that the their them then there these they
thing things think this to too try up us very want was we well were what when where which
who why will with would yeah you your
""".split())


def _stems(text: str) -> set[str]:
    """Content words of a phrase, cut to a rough stem so "spraying" meets "spray"."""
    words = "".join(ch.lower() if ch.isalnum() else " " for ch in text).split()
    return {w[:5] for w in words if len(w) >= 3 and w not in _COMMON_WORDS}


def best_matching_line(phrase: str, lines: list[tuple[int, str]]) -> int | None:
    """The id of the line a reminder most plausibly came from, or None if unclear.

    Words that are rare in the conversation count for more. A match needs two
    shared content words, or one that appears in a single line only.
    """
    import math

    wanted = _stems(phrase)
    if not wanted or not lines:
        return None
    stems = [(line_id, _stems(text)) for line_id, text in lines]
    seen: dict[str, int] = {}
    for _id, found in stems:
        for stem in found:
            seen[stem] = seen.get(stem, 0) + 1
    best, best_score = None, 0.0
    for line_id, found in stems:
        shared = wanted & found
        if not shared:
            continue
        if len(shared) < 2 and not any(seen[stem] == 1 and len(stem) >= 5 for stem in shared):
            continue
        score = sum(math.log(1 + len(lines) / seen[stem]) for stem in shared)
        if score > best_score:
            best, best_score = line_id, score
    return best

# Too short to hold a word; Whisper only produces noise on these.
MIN_TRANSCRIBE_MS = 500


@dataclass(frozen=True)
class Segment:
    path: Path
    start_ms: int
    duration_ms: int


def _resolve_times(rows: list[tuple]) -> list[int]:
    """Wall-clock start time for each row of (timestamp_ms, uptime_ms, received_ms).

    Pages recorded before the clock was set only carry time-since-boot. Recover
    their wall time from the nearest page of the same boot that has both.
    """
    def valid(i: int) -> bool:
        return rows[i][0] >= MIN_VALID_TIMESTAMP_MS

    times = []
    for i, (ts, uptime, received) in enumerate(rows):
        if valid(i):
            times.append(ts)
            continue
        resolved = None
        for j in range(i + 1, len(rows)):  # later page, same boot: uptime keeps rising
            if rows[j][1] < uptime:
                break
            if valid(j):
                resolved = rows[j][0] - rows[j][1] + uptime
                break
        if resolved is None:  # nothing later in this boot: look at earlier pages of it
            for j in range(i - 1, -1, -1):
                if rows[j][1] > uptime:
                    break
                if valid(j):
                    resolved = rows[j][0] - rows[j][1] + uptime
                    break
        times.append(received if resolved is None else resolved)
    return times


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Transcription runs in a worker thread; callers never use one store from
        # two threads at once, so sharing its connection is safe. Other stores may
        # have the same file open (the web server opens one per request).
        self.db = sqlite3.connect(self.data_dir / "pendant.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")  # commit() must mean "on disk"
        self.db.execute("PRAGMA secure_delete=ON")  # deleted text and audio are overwritten, not left in free pages
        self.db.executescript(_SCHEMA)
        added = set()
        for table, column, kind in _MIGRATIONS:
            have = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                added.add((table, column))
        self.db.commit()
        self.discarded_pages = 0  # pages dropped this session because they fell in a pause
        if ("utterances", "speaker_group") in added:
            # Imported lines were grouped by their import before this column existed.
            self.db.execute("UPDATE utterances SET speaker_group = source WHERE source IS NOT NULL")
            self.db.commit()
        if ("reminders", "utterance_id") in added:
            self.link_reminders_to_lines()  # reminders saved before lines were tracked
        self._posix_paths()

    def _posix_paths(self) -> None:
        """Store audio paths with forward slashes on every system.

        Paths were once saved as str(Path), so on Windows they held backslashes,
        which every `LIKE 'audio/%/...'` query missed. Rewrite any that did.
        """
        if self.db.execute("SELECT 1 FROM segments WHERE instr(path, '\\') > 0 LIMIT 1").fetchone() is None:
            return
        for table, column in (("segments", "path"), ("utterances", "segment"),
                              ("transcriptions", "segment"), ("added", "path")):
            self.db.execute(f"UPDATE {table} SET {column} = replace({column}, '\\', '/') "
                            f"WHERE instr({column}, '\\') > 0")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- pausing -------------------------------------------------------------------
    # The pendant cannot be told to stop recording. A pause is kept here instead:
    # audio from a paused period is thrown away when it arrives, never stored.

    def pause(self, now_ms: int | None = None) -> int:
        """Start a pause (or return the start of the one already running)."""
        since = self.paused_since()
        if since is not None:
            return since
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        self.db.execute("INSERT INTO pauses (start_ms) VALUES (?)", (now_ms,))
        self.db.commit()
        return now_ms

    def resume(self, now_ms: int | None = None) -> None:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        self.db.execute("UPDATE pauses SET end_ms = ? WHERE end_ms IS NULL", (now_ms,))
        self.db.commit()

    def paused_since(self) -> int | None:
        row = self.db.execute("SELECT start_ms FROM pauses WHERE end_ms IS NULL ORDER BY id LIMIT 1").fetchone()
        return row[0] if row else None

    def _was_paused(self, ms: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM pauses WHERE start_ms <= ? AND (end_ms IS NULL OR end_ms >= ?) LIMIT 1",
            (ms, ms)).fetchone() is not None

    def add_page(self, page: FlashPage) -> bool:
        """Queue a page for the next commit. Returns False if already stored,
        or if it was recorded during a pause and so is not kept."""
        if page.timestamp_ms >= MIN_VALID_TIMESTAMP_MS and self._was_paused(page.timestamp_ms):
            self.discarded_pages += 1
            return False
        cur = self.db.execute(
            "INSERT OR IGNORE INTO pages "
            "(hash, session, idx, timestamp_ms, uptime_ms, received_ms, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                hashlib.sha256(page.data).digest(),
                page.session,
                page.index,
                page.timestamp_ms,
                page.uptime_ms,
                int(time.time() * 1000),
                page.data,
            ),
        )
        return cur.rowcount > 0

    def commit(self) -> None:
        self.db.commit()

    def page_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

    def export_audio(self) -> list[Segment]:
        """Write not-yet-exported pages as Ogg Opus files, split at gaps."""
        rows = self.db.execute(
            "SELECT id, idx, timestamp_ms, uptime_ms, received_ms, raw FROM pages "
            "WHERE exported = 0 ORDER BY id"
        ).fetchall()
        times = _resolve_times([(r[2], r[3], r[4]) for r in rows])

        segments: list[Segment] = []
        current: dict | None = None

        def finish() -> None:
            nonlocal current
            if current is None:
                return
            seg = current
            current = None
            start = datetime.fromtimestamp(seg["start_ms"] / 1000)
            rel = Path("audio") / start.strftime("%Y-%m-%d") / (
                f"{start.strftime('%H-%M-%S')}_{seg['first_page']}.ogg"
            )
            path = self.data_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                duration_ms = write_ogg_opus(f, seg["frames"])
            self.db.execute(
                "INSERT OR REPLACE INTO segments (path, start_ms, duration_ms, first_page, last_page) "
                "VALUES (?, ?, ?, ?, ?)",
                (rel.as_posix(), seg["start_ms"], duration_ms, seg["first_page"], seg["last_page"]),
            )
            segments.append(Segment(path, seg["start_ms"], duration_ms))

        for (page_id, idx, _ts, _uptime, _received, raw), ts in zip(rows, times):
            if _ts < MIN_VALID_TIMESTAMP_MS and self._was_paused(ts):
                # Recorded before the pendant's clock was set, so the pause could only
                # be checked now that its time is worked out. Not kept, like the rest.
                finish()
                self.db.execute("DELETE FROM pages WHERE id = ?", (page_id,))
                self.discarded_pages += 1
                continue
            frames = extract_opus_frames(raw)
            if not frames:
                continue  # diagnostic page, or audio we could not parse; raw is kept
            if current is not None:
                expected = current["start_ms"] + current["samples"] // 48
                contiguous = (
                    idx == current["last_idx"] + 1
                    and abs(ts - expected) <= SEGMENT_GAP_MS
                )
                if not contiguous:
                    finish()
            if current is None:
                current = {"first_page": page_id, "start_ms": ts, "frames": [], "samples": 0}
            current["frames"].extend(frames)
            current["samples"] += sum(packet_samples(f) for f in frames)
            current["last_idx"] = idx
            current["last_page"] = page_id
        finish()

        self.db.execute("UPDATE pages SET exported = 1 WHERE exported = 0")
        self.db.commit()
        return segments

    def forget_transcriptions(self) -> None:
        """Mark recordings from the pendant as not transcribed, so the next run redoes them.

        Imported recordings keep the transcript they came with.
        """
        self.db.execute("DELETE FROM transcriptions WHERE engine != 'limitless'")
        self.db.commit()

    def pending_transcription(self) -> list[tuple[str, int, int]]:
        """(path, start_ms, duration_ms) of audio files not yet transcribed."""
        return self.db.execute(
            "SELECT path, start_ms, duration_ms FROM segments "
            "WHERE path NOT IN (SELECT segment FROM transcriptions) ORDER BY start_ms"
        ).fetchall()

    def transcribe_pending(
        self,
        transcriber: Transcriber,
        on_progress: Callable[[int, int], None] | None = None,
        on_audio: Callable[[int, int], None] | None = None,
        on_indexing: Callable[[int, int], None] | None = None,
    ) -> tuple[int, list[Path]]:
        """Transcribe new audio files. Returns (utterances added, transcript files written).

        `on_progress(n, of)` counts recordings; `on_audio(ms, of_ms)` follows the work
        through the audio itself, for a transcriber that can say; `on_indexing(n, of)`
        counts lines added to search by meaning afterwards.
        """
        if hasattr(transcriber, "transcribe_session"):
            # A hosted service with speaker labels works a conversation at a time.
            return self.transcribe_sessions(transcriber, on_progress, on_indexing=on_indexing)
        pending = self.pending_transcription()
        added = 0
        days: set[str] = set()
        all_ms = sum(duration for _path, _start, duration in pending) or 1
        heard_ms = 0
        follows = on_audio is not None and getattr(transcriber, "reports_progress", False)
        for n, (path, start_ms, duration_ms) in enumerate(pending, 1):
            utterances = []
            if duration_ms >= MIN_TRANSCRIBE_MS and follows:
                utterances = transcriber.transcribe(
                    self.data_dir / path,
                    on_heard=lambda s, base=heard_ms, length=duration_ms:
                        on_audio(base + min(int(s * 1000), length), all_ms))
            elif duration_ms >= MIN_TRANSCRIBE_MS:
                utterances = transcriber.transcribe(self.data_dir / path)
            heard_ms += duration_ms
            if on_audio:
                on_audio(heard_ms, all_ms)
            # Replace rather than append, so an interrupted run cannot duplicate text.
            self.db.execute("DELETE FROM utterances WHERE segment = ?", (path,))
            self.db.executemany(
                "INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES (?, ?, ?, ?)",
                [(path, start_ms + u.start_ms, start_ms + u.end_ms, u.text) for u in utterances],
            )
            self.db.execute(
                "INSERT OR REPLACE INTO transcriptions VALUES (?, ?, ?)",
                (path, transcriber.name, int(time.time() * 1000)),
            )
            self.db.commit()
            added += len(utterances)
            # Rewrite the day even with no lines: a redo can leave it with fewer.
            days.add(datetime.fromtimestamp(start_ms / 1000).strftime("%Y-%m-%d"))
            if on_progress:
                on_progress(n, len(pending))
        if added:
            from . import search

            search.update_if_built(self, on_indexing)  # keeps search by meaning complete, if it is in use
        return added, [self.write_transcript(day) for day in sorted(days)]

    # --- hosted transcription, a conversation at a time -----------------------------

    def pending_sessions(self, now_ms: int | None = None) -> list[list[tuple[str, int, int]]]:
        """Untranscribed pendant recordings grouped into conversations.

        The pendant stops recording during silence, so a conversation is many
        short recordings. A silence of CONVERSATION_GAP_MS ends a group. The
        newest group is held back until it has been quiet that long: sending
        half a conversation would give its two halves unrelated speaker numbers.
        """
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        pending = self.db.execute(
            "SELECT path, start_ms, duration_ms FROM segments WHERE first_page >= 0 "
            "AND path NOT IN (SELECT segment FROM transcriptions) ORDER BY start_ms").fetchall()
        sessions: list[list[tuple[str, int, int]]] = []
        for item in pending:
            _path, start, _duration = item
            last = sessions[-1] if sessions else None
            if (last and start - (last[-1][1] + last[-1][2]) <= CONVERSATION_GAP_MS
                    and start + _duration - last[0][1] <= SESSION_MAX_MS):
                last.append(item)
            else:
                sessions.append([item])
        if sessions:
            _path, start, duration = sessions[-1][-1]
            if now_ms - (start + duration) <= CONVERSATION_GAP_MS:
                sessions.pop()  # may still be going on
        # A recording added from a file is whole already: one session each.
        sessions += [[item] for item in self.db.execute(
            "SELECT path, start_ms, duration_ms FROM segments WHERE first_page < 0 "
            "AND path NOT IN (SELECT segment FROM transcriptions) ORDER BY start_ms").fetchall()]
        return sessions

    def session_audio(self, session: list[tuple[str, int, int]]) -> tuple[bytes, list[tuple[str, int, float, float]]]:
        """One Ogg Opus stream of a session's recordings end to end.

        Also returns, for each recording: (path, the clock time it starts at in ms,
        where it starts in the joined audio in seconds, how long it is in seconds).
        The stream is rebuilt from the stored pages, so nothing is re-encoded.
        A recording added from a file is sent as the file it is.
        """
        import io

        if len(session) == 1 and self.db.execute(
                "SELECT first_page FROM segments WHERE path = ?", (session[0][0],)).fetchone()[0] < 0:
            path, start_ms, duration_ms = session[0]
            return (self.data_dir / path).read_bytes(), [(path, start_ms, 0.0, duration_ms / 1000)]
        packets: list[bytes] = []
        pieces: list[tuple[str, int, float, float]] = []
        at = 0.0
        for path, start_ms, _duration in session:
            first, last = self.db.execute(
                "SELECT first_page, last_page FROM segments WHERE path = ?", (path,)).fetchone()
            frames = [frame for (raw,) in self.db.execute(
                "SELECT raw FROM pages WHERE id BETWEEN ? AND ? ORDER BY id", (first, last))
                for frame in extract_opus_frames(raw)]
            length = sum(packet_samples(frame) for frame in frames) / 48000
            pieces.append((path, start_ms, at, length))
            at += length
            packets += frames
        out = io.BytesIO()
        write_ogg_opus(out, packets)
        return out.getvalue(), pieces

    def transcribe_sessions(
        self,
        service,
        on_progress: Callable[[int, int], None] | None = None,
        now_ms: int | None = None,
        on_indexing: Callable[[int, int], None] | None = None,
    ) -> tuple[int, list[Path]]:
        """Send each waiting conversation to a hosted service and store its lines.

        Every line keeps the service's speaker number, which holds within that
        conversation only, exactly like an imported conversation's speakers.
        """
        import bisect

        sessions = self.pending_sessions(now_ms)
        added = 0
        days: set[str] = set()
        for n, session in enumerate(sessions, 1):
            audio, pieces = self.session_audio(session)
            total_s = pieces[-1][2] + pieces[-1][3]
            # Too short to hold speech: not worth a request.
            lines = service.transcribe_session(audio) if total_s >= MIN_TRANSCRIBE_MS / 1000 else []
            group = f"{service.name.split(':')[0]}:{session[0][1]}"
            starts = [piece[2] for piece in pieces]
            rows = []
            for line in lines:
                # Which recording was playing at that moment of the joined audio.
                path, wall_ms, base, length = pieces[max(0, bisect.bisect_right(starts, line.start_s) - 1)]
                offset = min(max(0.0, line.start_s - base), length)
                start_ms = wall_ms + round(offset * 1000)
                label = "" if line.speaker is None else str(line.speaker + 1)
                rows.append((path, start_ms, start_ms + max(1, round((line.end_s - line.start_s) * 1000)),
                             line.text, group, label))
            for path, start_ms, _duration in session:
                # Replace rather than append, so an interrupted run cannot duplicate text.
                self.db.execute("DELETE FROM utterances WHERE segment = ?", (path,))
                self.db.execute("INSERT OR REPLACE INTO transcriptions VALUES (?, ?, ?)",
                                (path, service.name, int(time.time() * 1000)))
                days.add(datetime.fromtimestamp(start_ms / 1000).strftime("%Y-%m-%d"))
            self.db.executemany(
                "INSERT INTO utterances (segment, start_ms, end_ms, text, speaker_group, source_speaker) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
            self.db.commit()
            added += len(rows)
            if on_progress:
                on_progress(n, len(sessions))
        if added:
            from . import search

            search.update_if_built(self, on_indexing)  # keeps search by meaning complete, if it is in use
        return added, [self.write_transcript(day) for day in sorted(days)]

    def write_transcript(self, day: str) -> Path:
        """Rewrite the Markdown transcript for one local-time day (YYYY-MM-DD)."""
        start = datetime.strptime(day, "%Y-%m-%d")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(start.replace(hour=23, minute=59, second=59).timestamp() * 1000) + 1000
        rows = self.db.execute(
            "SELECT u.start_ms, u.text, COALESCE(s.name, 'Speaker ' || NULLIF(u.source_speaker, '')) "
            "FROM utterances u LEFT JOIN speakers s ON s.id = u.speaker_id "
            "WHERE u.start_ms >= ? AND u.start_ms < ? ORDER BY u.start_ms, u.id",
            (start_ms, end_ms),
        ).fetchall()
        digests = self.db.execute(
            "SELECT start_ms, end_ms, title, summary FROM conversations "
            "WHERE status = 'done' AND end_ms >= ? AND start_ms < ? ORDER BY start_ms",
            (start_ms, end_ms),
        ).fetchall()
        lines = [f"# {day}", ""]
        for ts, text, speaker in rows:
            clock = datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")
            while digests and digests[0][0] <= ts:
                d_start, d_end, title, summary = digests.pop(0)
                if ts > d_end:
                    continue
                lines += ["", f"## {clock[:5]} {title}", ""]
                lines += [f"> {point}" for point in json.loads(summary or "[]")]
                lines.append("")
            who = f"{speaker}: " if speaker else ""
            lines.append(f"- **{clock}** {who}{text}")
        path = self.data_dir / "transcripts" / f"{day}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return path

    # --- speakers ---------------------------------------------------------------

    def _rewrite_days(self, where: str, params: tuple) -> list[Path]:
        days = {
            datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
            for (ms,) in self.db.execute(f"SELECT start_ms FROM utterances WHERE {where}", params)
        }
        return [self.write_transcript(day) for day in sorted(days)]

    def rewrite_all_transcripts(self) -> list[Path]:
        return self._rewrite_days("1 = 1", ())

    def label_speakers(self, embedder: Embedder, load_audio: Callable[[Path], "np.ndarray"]) -> int:
        """Fingerprint new lines and group them into speakers. Returns lines labelled."""
        import numpy as np

        todo = self.db.execute(
            "SELECT u.id, u.segment, u.start_ms - s.start_ms, u.end_ms - u.start_ms "
            "FROM utterances u JOIN segments s ON s.path = u.segment "
            "WHERE u.embedding IS NULL AND u.source IS NULL AND u.speaker_group IS NULL "
            "AND u.end_ms - u.start_ms >= ? "
            "ORDER BY u.segment",
            (MIN_SPEAKER_MS,),
        ).fetchall()
        loaded: tuple[str, "np.ndarray"] | None = None
        for uid, segment, offset_ms, duration_ms in todo:
            if loaded is None or loaded[0] != segment:
                loaded = (segment, load_audio(self.data_dir / segment))
            first = offset_ms * SAMPLE_RATE // 1000
            clip = loaded[1][first : first + duration_ms * SAMPLE_RATE // 1000]
            if len(clip) < MIN_SPEAKER_MS * SAMPLE_RATE // 1000:
                continue  # the file is shorter than the transcript claims
            voice = np.asarray(embedder.embed(clip), dtype=np.float32)
            self.db.execute("UPDATE utterances SET embedding = ? WHERE id = ?", (voice.tobytes(), uid))
            self.db.commit()  # keep write locks short: the Lifelog may be saving too
        return self._assign_speakers()

    def _voice_book(self):
        """(average fingerprint, {speaker id: [sum of fingerprints, count]}), or None.

        Speaker centroids are worked out from the lines assigned to each speaker,
        so merging or reassigning lines never leaves a stale centroid behind.
        """
        import numpy as np

        rows = self.db.execute(
            "SELECT speaker_id, embedding FROM utterances WHERE embedding IS NOT NULL").fetchall()
        if len(rows) < MIN_FINGERPRINTS:
            return None
        total = None
        sums: dict[int, list] = {}
        for speaker_id, blob in rows:
            voice = np.frombuffer(blob, dtype=np.float32).astype(np.float64)
            total = voice if total is None else total + voice
            if speaker_id is not None:
                entry = sums.setdefault(speaker_id, [np.zeros_like(voice), 0])
                entry[0] = entry[0] + voice
                entry[1] += 1
        # The average fingerprint is mostly the microphone and the room, not
        # anyone's voice. Subtracting it is what makes voices separable.
        return total / len(rows), sums

    @staticmethod
    def _match(voice, book, exclude=()) -> int | None:
        """The known speaker this fingerprint belongs to, if any is close enough."""
        import numpy as np

        mean, sums = book

        def unit(v):
            norm = np.linalg.norm(v)
            return v / norm if norm else v

        centred = unit(voice - mean)
        best, best_score = None, MATCH_THRESHOLD
        for speaker_id, (voice_sum, count) in sums.items():
            if speaker_id in exclude or not count:
                continue
            score = float(centred @ unit(voice_sum / count - mean))
            if score >= best_score:
                best, best_score = speaker_id, score
        return best

    def _new_speaker(self, name: str | None = None) -> int:
        cur = self.db.execute("INSERT INTO speakers (name, voice_sum, lines) VALUES ('', x'', 0)")
        speaker_id = cur.lastrowid
        self.db.execute("UPDATE speakers SET name = ? WHERE id = ?",
                        (name or f"Speaker {speaker_id}", speaker_id))
        return speaker_id

    def _assign_speakers(self) -> int:
        """Group fingerprinted lines recorded from the pendant into speakers."""
        import numpy as np

        book = self._voice_book()
        if book is None:
            return 0
        pending = self.db.execute(
            "SELECT id, embedding FROM utterances WHERE embedding IS NOT NULL "
            "AND speaker_id IS NULL AND source IS NULL AND speaker_group IS NULL ORDER BY start_ms, id"
        ).fetchall()
        for uid, blob in pending:
            voice = np.frombuffer(blob, dtype=np.float32).astype(np.float64)
            speaker_id = self._match(voice, book)
            if speaker_id is None:
                speaker_id = self._new_speaker()
            entry = book[1].setdefault(speaker_id, [np.zeros_like(voice), 0])
            entry[0] = entry[0] + voice
            entry[1] += 1
            self.db.execute("UPDATE utterances SET speaker_id = ? WHERE id = ?", (speaker_id, uid))
        self.db.commit()
        ids = [uid for uid, _ in pending]
        for at in range(0, len(ids), 20000):  # SQLite takes at most 32,766 values in one query
            some = ids[at:at + 20000]
            self._rewrite_days(f"id IN ({','.join('?' * len(some))})", tuple(some))
        return len(pending)

    # Imported conversations already say which lines share a speaker, so a few
    # long lines per speaker are enough to recognise the voice.
    _PICKS_PER_SPEAKER = 5
    _PICK_MAX_MS = 15000

    def link_imported_speakers(
        self,
        embedder: Embedder,
        load_audio: Callable[[Path], "np.ndarray"],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int]:
        """Link the per-conversation speakers of imported conversations to people.

        Returns (speakers linked, conversations where the closest voice was already
        taken by another speaker of the same conversation).
        """
        import numpy as np

        # Every imported speaker without a fingerprint yet, including ones the user
        # has already named: a named voice is what the others get matched against.
        groups = self.db.execute(
            "SELECT source, source_speaker FROM utterances "
            "WHERE source IS NOT NULL AND source_speaker != '' "
            "GROUP BY source, source_speaker HAVING SUM(embedding IS NOT NULL) = 0").fetchall()
        picks = []
        for source, label in groups:
            picks += self.db.execute(
                "SELECT u.id, u.segment, u.start_ms - s.start_ms, u.end_ms - u.start_ms "
                "FROM utterances u JOIN segments s ON s.path = u.segment "
                "WHERE u.source = ? AND u.source_speaker = ? AND u.end_ms - u.start_ms >= ? "
                "AND u.embedding IS NULL ORDER BY u.end_ms - u.start_ms DESC LIMIT ?",
                (source, label, MIN_SPEAKER_MS, self._PICKS_PER_SPEAKER)).fetchall()
        picks.sort(key=lambda row: row[1])  # one decode per audio file
        loaded: tuple[str, "np.ndarray"] | None = None
        for n, (uid, segment, offset_ms, duration_ms) in enumerate(picks, 1):
            if loaded is None or loaded[0] != segment:
                loaded = (segment, load_audio(self.data_dir / segment))
            first = max(0, offset_ms) * SAMPLE_RATE // 1000
            clip = loaded[1][first : first + min(duration_ms, self._PICK_MAX_MS) * SAMPLE_RATE // 1000]
            if len(clip) >= MIN_SPEAKER_MS * SAMPLE_RATE // 1000:
                voice = np.asarray(embedder.embed(clip), dtype=np.float32)
                self.db.execute("UPDATE utterances SET embedding = ? WHERE id = ?", (voice.tobytes(), uid))
                self.db.commit()  # keep write locks short: the Lifelog may be saving too
            if on_progress:
                on_progress(n, len(picks))
        return self._link_imported()

    def _link_imported(self) -> tuple[int, int]:
        import numpy as np

        book = self._voice_book()
        if book is None:
            return 0, 0
        taken: dict[str, set[int]] = {}
        for source, speaker_id in self.db.execute(
                "SELECT DISTINCT source, speaker_id FROM utterances "
                "WHERE source IS NOT NULL AND speaker_id IS NOT NULL"):
            taken.setdefault(source, set()).add(speaker_id)
        # Speakers who said the most first: their fingerprints are the most reliable.
        groups = self.db.execute(
            "SELECT source, source_speaker FROM utterances "
            "WHERE source IS NOT NULL AND source_speaker != '' AND speaker_id IS NULL "
            "GROUP BY source, source_speaker ORDER BY SUM(end_ms - start_ms) DESC").fetchall()
        linked = conflicts = 0
        for source, label in groups:
            voices = [np.frombuffer(blob, dtype=np.float32).astype(np.float64)
                      for (blob,) in self.db.execute(
                          "SELECT embedding FROM utterances WHERE source = ? AND source_speaker = ? "
                          "AND embedding IS NOT NULL", (source, label))]
            if not voices:
                continue  # no audio, or nothing long enough to judge
            voice = sum(voices) / len(voices)
            used = taken.setdefault(source, set())
            # Two speakers of one conversation are different people, whatever the voices say.
            speaker_id = self._match(voice, book, exclude=used)
            if used and self._match(voice, book) in used:
                conflicts += 1
            if speaker_id is None:
                speaker_id = self._new_speaker()
            entry = book[1].setdefault(speaker_id, [np.zeros_like(voice), 0])
            entry[0] = entry[0] + sum(voices)
            entry[1] += len(voices)
            used.add(speaker_id)
            self.db.execute(
                "UPDATE utterances SET speaker_id = ? WHERE source = ? AND source_speaker = ?",
                (speaker_id, source, label))
            linked += 1
        self.db.commit()
        if linked:
            self.rewrite_all_transcripts()
        return linked, conflicts

    def unlink_imported_speakers(self) -> int:
        """Undo automatic linking: imported speakers that were never given a real
        name go back to their per-conversation labels. Named ones are kept.

        Returns how many per-conversation speakers were unlinked.
        """
        auto = "SELECT id FROM speakers WHERE name = 'Speaker ' || id"
        (count,) = self.db.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM utterances WHERE source IS NOT NULL "
            f"AND speaker_id IN ({auto}) GROUP BY source, source_speaker)").fetchone()
        self.db.execute(
            f"UPDATE utterances SET speaker_id = NULL WHERE source IS NOT NULL AND speaker_id IN ({auto})")
        self.db.execute(
            "DELETE FROM speakers WHERE id NOT IN "
            "(SELECT speaker_id FROM utterances WHERE speaker_id IS NOT NULL)")
        self.db.commit()
        if count:
            self.rewrite_all_transcripts()
        return count

    def speakers(self) -> list[tuple[int, str, int]]:
        """(id, name, lines) for every speaker, most lines first."""
        return self.db.execute(
            "SELECT s.id, s.name, COUNT(u.id) FROM speakers s "
            "LEFT JOIN utterances u ON u.speaker_id = s.id "
            "GROUP BY s.id ORDER BY COUNT(u.id) DESC, s.id").fetchall()

    def _named_lines(self, speaker_id: int | None = None) -> list[tuple[int, str, int, int, int | None]]:
        """(speaker id, name, start, end, conversation id or None) for every line of a named
        person, oldest first. A line belongs to the titled conversation whose span holds it."""
        from bisect import bisect_right

        talks = self.db.execute(
            "SELECT start_ms, end_ms, id FROM conversations WHERE status = 'done' ORDER BY start_ms").fetchall()
        starts = [talk[0] for talk in talks]
        only, who = ("AND s.id = ? ", (speaker_id,)) if speaker_id is not None else ("", ())
        out = []
        for sid, name, start, end in self.db.execute(
                "SELECT s.id, s.name, u.start_ms, u.end_ms FROM utterances u JOIN speakers s ON s.id = u.speaker_id "
                "WHERE s.name NOT GLOB 'Speaker [0-9]*' " + only + "ORDER BY u.start_ms", who):
            at = bisect_right(starts, start) - 1
            talk = talks[at][2] if at >= 0 and start <= talks[at][1] else None
            out.append((sid, name, start, end, talk))
        return out

    def people(self) -> list[dict]:
        """Every person somebody named, most recently heard first: how much they said, in how
        many conversations and on how many days, and when they were first and last heard.

        Only lines carrying the name count. A name given in one recording does not carry to
        that voice elsewhere, so these are floors, not totals.
        """
        found: dict[int, dict] = {}
        for sid, name, start, end, talk in self._named_lines():
            p = found.setdefault(sid, {"id": sid, "name": name, "lines": 0, "ms": 0, "first": start, "last": start,
                                       "talks": set(), "days": set()})
            p["lines"] += 1
            p["ms"] += max(0, end - start)
            p["last"] = max(p["last"], start)
            if talk is not None:
                p["talks"].add(talk)
            p["days"].add(datetime.fromtimestamp(start / 1000).strftime("%Y-%m-%d"))
        from_clues = {row[0] for row in self.db.execute("SELECT id FROM speakers WHERE named_by = 'clues'")}
        return [
            {"id": p["id"], "name": p["name"], "lines": p["lines"], "ms": p["ms"], "first": p["first"],
             "last": p["last"], "conversations": len(p["talks"]), "days": len(p["days"]),
             "from_clues": p["id"] in from_clues}
            for p in sorted(found.values(), key=lambda p: (-p["last"], p["name"]))]

    def person(self, speaker_id: int) -> dict:
        """One named person: the conversations they were heard in, newest first, with how long
        they spoke in each."""
        row = self.db.execute("SELECT name FROM speakers WHERE id = ?", (speaker_id,)).fetchone()
        if row is None:
            raise ValueError(f"there is no speaker {speaker_id}")
        spoke: dict[int, list[int]] = {}
        for _sid, _name, start, end, talk in self._named_lines(speaker_id):
            if talk is not None:
                entry = spoke.setdefault(talk, [0, 0])
                entry[0] += max(0, end - start)
                entry[1] += 1
        talks = []
        for cid, (ms, lines) in spoke.items():
            start, end, title = self.db.execute(
                "SELECT start_ms, end_ms, title FROM conversations WHERE id = ?", (cid,)).fetchone()
            talks.append({"id": cid, "title": title, "start": start, "end": end, "ms": ms, "lines": lines,
                          "day": datetime.fromtimestamp(start / 1000).strftime("%Y-%m-%d")})
        return {"id": speaker_id, "name": row[0], "conversations": sorted(talks, key=lambda t: -t["start"])}

    def named_speakers(self) -> list[tuple[str, int, int | None]]:
        """(name, lines, when last heard) for every speaker somebody named, most recently heard first.

        Speakers still carrying a number ("Speaker 2") are left out.
        """
        return self.db.execute(
            "SELECT s.name, COUNT(u.id), MAX(u.start_ms) FROM speakers s "
            "LEFT JOIN utterances u ON u.speaker_id = s.id "
            "WHERE s.name NOT GLOB 'Speaker [0-9]*' "
            "GROUP BY s.id ORDER BY MAX(u.start_ms) DESC, s.name").fetchall()

    @staticmethod
    def _clean_name(name: str) -> str:
        name = " ".join(name.split())
        if not name:
            raise ValueError("a speaker needs a name")
        return name

    def rename_speaker(self, speaker_id: int, name: str) -> int:
        """Name a speaker everywhere. Giving it another speaker's name merges the two.

        Returns the id that now holds those lines.
        """
        name = self._clean_name(name)
        if self.db.execute("SELECT 1 FROM speakers WHERE id = ?", (speaker_id,)).fetchone() is None:
            raise ValueError(f"there is no speaker {speaker_id}")
        other = self.db.execute(
            "SELECT id FROM speakers WHERE id != ? AND name = ? COLLATE NOCASE",
            (speaker_id, name)).fetchone()
        keep = speaker_id
        if other is None:
            self.db.execute("UPDATE speakers SET name = ?, named_by = NULL WHERE id = ?", (name, speaker_id))
        else:
            keep = other[0]
            self.db.execute("UPDATE utterances SET speaker_id = ? WHERE speaker_id = ?", (keep, speaker_id))
            self.db.execute("UPDATE name_votes SET speaker_id = ? WHERE speaker_id = ?", (keep, speaker_id))
            self.db.execute("UPDATE speakers SET named_by = NULL WHERE id = ?", (keep,))
            self.db.execute("DELETE FROM speakers WHERE id = ?", (speaker_id,))
        self.db.commit()
        self._rewrite_days("speaker_id = ?", (keep,))
        return keep

    def rename_conversation(self, conversation_id: int, title: str) -> str:
        """Give a conversation the title the user typed. Returns the title as kept.

        Summarising only titles conversations it has not seen, so this is never overwritten.
        """
        title = " ".join(title.split())
        if not title:
            raise ValueError("a conversation needs a title")
        found = self.db.execute(
            "SELECT start_ms, end_ms FROM conversations WHERE id = ? AND status = 'done'",
            (conversation_id,)).fetchone()
        if found is None:
            raise ValueError(f"there is no conversation {conversation_id}")
        self.db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
        self.db.commit()
        self._rewrite_days("start_ms >= ? AND start_ms <= ?", found)  # the title heads its lines in the transcript
        return title

    def assign_line_speaker(self, utterance_id: int, name: str) -> int:
        """Say who spoke a line, here only.

        Where the conversation has numbered speakers (an imported one, or one
        transcribed by a hosted service) this covers every line of that speaker
        in that conversation; otherwise just the one line. The name is matched
        to an existing speaker or creates a new one.
        """
        name = self._clean_name(name)
        row = self.db.execute(
            "SELECT speaker_group, source_speaker FROM utterances WHERE id = ?", (utterance_id,)).fetchone()
        if row is None:
            raise ValueError(f"there is no line {utterance_id}")
        found = self.db.execute(
            "SELECT id FROM speakers WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        target = found[0] if found else self._new_speaker(name)
        if row[0] and row[1]:
            where, params = "speaker_group = ? AND source_speaker = ?", (row[0], row[1])
        else:
            where, params = "id = ?", (utterance_id,)
        self.db.execute(f"UPDATE utterances SET speaker_id = ? WHERE {where}", (target, *params))
        # Drop speakers this left with no lines.
        self.db.execute(
            "DELETE FROM speakers WHERE id != ? AND id NOT IN "
            "(SELECT speaker_id FROM utterances WHERE speaker_id IS NOT NULL)", (target,))
        self.db.commit()
        self._rewrite_days(where, params)
        return target

    def reset_speakers(self) -> int:
        """Forget all speakers and names, and group every stored fingerprint again."""
        self.db.execute("UPDATE utterances SET speaker_id = NULL")
        self.db.execute("DELETE FROM speakers")
        self.db.execute("DELETE FROM name_votes WHERE kind = 'rejected'")  # those voices are gone
        self.db.commit()
        labelled = self._assign_speakers()
        self.rewrite_all_transcripts()
        return labelled

    # --- conversations, summaries and reminders ------------------------------------

    def build_conversations(self, now_ms: int | None = None) -> int:
        """Group lines newer than the last conversation into conversations.

        A conversation ends at a silence of CONVERSATION_GAP_MS. The most recent
        one is left alone until that much time has passed, in case it continues.
        """
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        (after,) = self.db.execute("SELECT COALESCE(MAX(end_ms), 0) FROM conversations").fetchone()
        rows = self.db.execute(
            "SELECT start_ms, end_ms FROM utterances WHERE start_ms > ? ORDER BY start_ms, id",
            (after,),
        ).fetchall()
        groups: list[list[int]] = []
        for start, end in rows:
            if groups and start - groups[-1][1] <= CONVERSATION_GAP_MS:
                groups[-1][1] = max(groups[-1][1], end)
            else:
                groups.append([start, end])
        if groups and now_ms - groups[-1][1] <= CONVERSATION_GAP_MS:
            groups.pop()  # may still be going on
        # A recording added from a file can be older than everything above: last
        # week's Voice Memo. Its lines become conversations too, wherever they fall.
        late: list[list[int]] = []
        for start, end in self.db.execute(
                "SELECT u.start_ms, u.end_ms FROM utterances u WHERE u.start_ms <= ? "
                "AND u.segment IN (SELECT path FROM segments WHERE first_page < 0 AND path LIKE 'audio/%/added_%') "
                "AND NOT EXISTS (SELECT 1 FROM conversations c WHERE u.start_ms BETWEEN c.start_ms AND c.end_ms) "
                "ORDER BY u.start_ms, u.id", (after,)):
            if late and start - late[-1][1] <= CONVERSATION_GAP_MS:
                late[-1][1] = max(late[-1][1], end)
            else:
                late.append([start, end])
        groups += late
        self.db.executemany(
            "INSERT INTO conversations (start_ms, end_ms, status) VALUES (?, ?, 'pending')", groups)
        self.db.commit()
        return len(groups)

    def conversation_text(self, start_ms: int, end_ms: int) -> tuple[str, int, list[int]]:
        """The transcript handed to the summariser, how many words were said, and
        the id of each numbered line."""
        rows = self.db.execute(
            "SELECT u.id, u.start_ms, u.text, COALESCE(s.name, 'Speaker ' || NULLIF(u.source_speaker, '')) "
            "FROM utterances u LEFT JOIN speakers s ON s.id = u.speaker_id "
            "WHERE u.start_ms >= ? AND u.start_ms <= ? ORDER BY u.start_ms, u.id",
            (start_ms, end_ms),
        ).fetchall()
        transcript = "\n".join(
            f"{n}. [{datetime.fromtimestamp(ts / 1000).strftime('%H:%M')}] {name or 'Unknown'}: {text}"
            for n, (_id, ts, text, name) in enumerate(rows, 1)
        )
        return transcript, sum(len(row[2].split()) for row in rows), [row[0] for row in rows]

    def write_notes(
        self,
        summarizer: Summarizer,
        lo_ms: int,
        hi_ms: int,
        redo: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Write notes for the conversations that began in [lo_ms, hi_ms) and have none.

        Returns how many were written. Titles and summaries are left alone. What
        is still to be done becomes suggested reminders, but only for a
        conversation that has none, so nothing is suggested twice. A DigestError
        stops the run; notes already written are kept.
        """
        rows = self.db.execute(
            "SELECT id, start_ms, end_ms FROM conversations WHERE status = 'done' "
            "AND start_ms >= ? AND start_ms < ?" + ("" if redo else " AND notes IS NULL") + " ORDER BY start_ms",
            (lo_ms, hi_ms)).fetchall()
        done = 0
        for n, (cid, start_ms, end_ms) in enumerate(rows, 1):
            text, words, line_ids = self.conversation_text(start_ms, end_ms)
            found: dict = {"notes": [], "todos": []}
            if words >= MIN_DIGEST_WORDS:
                try:
                    found = summarizer.notes(text)
                except DigestDeclined:
                    pass
            for item in [*found["notes"], *found["todos"]]:
                # The model cites a line number; keep the line's own id.
                cited = item["line"]
                item["line"] = line_ids[cited - 1] if cited and 1 <= cited <= len(line_ids) else None
            self.db.execute("UPDATE conversations SET notes = ? WHERE id = ?", (json.dumps(found["notes"]), cid))
            if not self.db.execute("SELECT 1 FROM reminders WHERE conversation_id = ?", (cid,)).fetchone():
                for todo in found["todos"]:
                    self.db.execute(
                        "INSERT INTO reminders (conversation_id, text, created_ms, utterance_id) VALUES (?, ?, ?, ?)",
                        (cid, todo["text"], start_ms, todo["line"]))
            self.db.commit()
            done += 1
            if on_progress:
                on_progress(n, len(rows))
        return done

    def link_reminders_to_lines(self) -> int:
        """Find the line each unlinked reminder came from, by its wording."""
        linked = 0
        rows = self.db.execute(
            "SELECT r.id, r.text, c.start_ms, c.end_ms FROM reminders r "
            "JOIN conversations c ON c.id = r.conversation_id WHERE r.utterance_id IS NULL").fetchall()
        for reminder_id, text, start_ms, end_ms in rows:
            lines = self.db.execute(
                "SELECT id, text FROM utterances WHERE start_ms >= ? AND start_ms <= ? "
                "ORDER BY start_ms, id", (start_ms, end_ms)).fetchall()
            line_id = best_matching_line(text, lines)
            if line_id is not None:
                self.db.execute("UPDATE reminders SET utterance_id = ? WHERE id = ?", (line_id, reminder_id))
                linked += 1
        self.db.commit()
        return linked

    def digest_pending(
        self,
        summarizer: Summarizer,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Title, summarise and extract reminders for new conversations.

        Returns how many were summarised. A DigestError from the engine stops the
        run and leaves the rest pending for next time.
        """
        self.build_conversations()
        pending = self.db.execute(
            "SELECT id, start_ms, end_ms FROM conversations WHERE status = 'pending' ORDER BY start_ms"
        ).fetchall()
        done = 0
        for n, (cid, start_ms, end_ms) in enumerate(pending, 1):
            text, words, line_ids = self.conversation_text(start_ms, end_ms)
            if words < MIN_DIGEST_WORDS:
                self.db.execute("UPDATE conversations SET status = 'short' WHERE id = ?", (cid,))
            else:
                try:
                    result = summarizer.summarize(text)
                except DigestDeclined:
                    self.db.execute("UPDATE conversations SET status = 'skipped' WHERE id = ?", (cid,))
                else:
                    self.db.execute(
                        "UPDATE conversations SET status = 'done', title = ?, summary = ?, engine = ? "
                        "WHERE id = ?",
                        (result["title"], json.dumps(result["summary"]), summarizer.name, cid))
                    for reminder in result["reminders"]:
                        # The model cites a line number; fall back to matching the wording.
                        if isinstance(reminder, str):
                            reminder = {"text": reminder, "line": None}
                        cited = reminder.get("line")
                        line_id = line_ids[cited - 1] if cited and 1 <= cited <= len(line_ids) else None
                        self.db.execute(
                            "INSERT INTO reminders (conversation_id, text, created_ms, utterance_id) "
                            "VALUES (?, ?, ?, ?)", (cid, reminder["text"], start_ms, line_id))
                    self.link_reminders_to_lines()
                    done += 1
            self.db.commit()
            self._rewrite_days("start_ms >= ? AND start_ms <= ?", (start_ms, end_ms))
            if on_progress:
                on_progress(n, len(pending))
        if hasattr(summarizer, "recap"):
            self.recap_pending(summarizer)  # days that have just been completed
        return done

    # --- the day's recap ---------------------------------------------------------------

    def day_material(self, day: str) -> tuple[str, str, int]:
        """What a recap of `day` (local, YYYY-MM-DD) is written from.

        Returns the text handed to the model, a signature that changes when the
        day's conversations do, and how many conversations there are.
        """
        start = datetime.strptime(day, "%Y-%m-%d")
        lo, hi = int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)
        rows = self.db.execute(
            "SELECT id, start_ms, end_ms, title, summary FROM conversations "
            "WHERE status = 'done' AND start_ms >= ? AND start_ms < ? ORDER BY start_ms", (lo, hi)).fetchall()
        parts = []
        for _cid, start_ms, end_ms, title, summary in rows:
            at = datetime.fromtimestamp(start_ms / 1000).strftime("%H:%M")
            minutes = max(1, round((end_ms - start_ms) / 60000))
            points = json.loads(summary) if summary else []
            parts.append("\n".join([f"{at}, {minutes} min: {title}", *(f"- {point}" for point in points)]))
        signature = hashlib.sha256(json.dumps([(r[0], r[3], r[4]) for r in rows]).encode()).hexdigest()[:16]
        return "\n\n".join(parts), signature, len(rows)

    def day_recap(self, day: str) -> dict | None:
        """The saved recap of a day, marked stale if the day has changed since."""
        row = self.db.execute(
            "SELECT headline, points, signature, made_ms FROM day_recaps WHERE day = ?", (day,)).fetchone()
        if row is None:
            return None
        return {"headline": row[0], "points": json.loads(row[1]), "made": row[3],
                "stale": row[2] != self.day_material(day)[1]}

    def recap_day(self, day: str, summarizer: Summarizer) -> dict | None:
        """Write (or rewrite) the recap of one day. None if the day has too little in it."""
        text, signature, count = self.day_material(day)
        if count < MIN_RECAP_CONVERSATIONS:
            return None
        date = datetime.strptime(day, "%Y-%m-%d")
        result = summarizer.recap(f"{date:%A, %B} {date.day}, {date.year}\n\n{text}")
        self.db.execute(
            "INSERT OR REPLACE INTO day_recaps (day, headline, points, engine, signature, made_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (day, result["headline"], json.dumps(result["points"]), summarizer.name, signature,
             int(time.time() * 1000)))
        self.db.commit()
        return self.day_recap(day)

    def days_to_recap(self, everything: bool = False, today: str | None = None) -> list[str]:
        """Finished days whose recap is missing or out of date, oldest first.

        By default only days with something recorded from the pendant: sending
        the whole imported history to a model is a choice, made with `everything`.
        """
        today = today or datetime.now().strftime("%Y-%m-%d")
        ours = "" if everything else "AND engine IS NOT 'limitless' "
        days = [d for (d,) in self.db.execute(
            "SELECT DISTINCT strftime('%Y-%m-%d', start_ms / 1000, 'unixepoch', 'localtime') FROM conversations "
            f"WHERE status = 'done' {ours}ORDER BY 1") if d < today]
        out = []
        for day in days:
            _text, signature, count = self.day_material(day)
            saved = self.db.execute("SELECT signature FROM day_recaps WHERE day = ?", (day,)).fetchone()
            if count >= MIN_RECAP_CONVERSATIONS and (saved is None or saved[0] != signature):
                out.append(day)
        return out

    def recap_pending(self, summarizer: Summarizer, everything: bool = False,
                      on_progress: Callable[[int, int], None] | None = None, today: str | None = None) -> int:
        """Recap the days that need it. Returns how many were written."""
        days = self.days_to_recap(everything, today)
        done = 0
        for n, day in enumerate(days, 1):
            try:
                done += self.recap_day(day, summarizer) is not None
            except DigestDeclined:
                pass  # left without a recap; the conversations still have theirs
            if on_progress:
                on_progress(n, len(days))
        return done

    def forget_digests(self) -> None:
        """Drop every conversation, summary and reminder so they can be rebuilt."""
        # Imported conversations carry their own titles; only ours are rebuilt.
        ours = "SELECT id FROM conversations WHERE engine IS NOT 'limitless'"
        self.db.execute(f"DELETE FROM reminders WHERE conversation_id IN ({ours})")
        self.db.execute(f"DELETE FROM sections WHERE conversation_id IN ({ours})")
        self.db.execute("DELETE FROM conversations WHERE engine IS NOT 'limitless'")
        self.db.commit()
        self._rewrite_days("1 = 1", ())

    def conversations(self, lo_ms: int, hi_ms: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, start_ms, end_ms, title, summary, notes FROM conversations "
            "WHERE status = 'done' AND start_ms >= ? AND start_ms < ? ORDER BY start_ms",
            (lo_ms, hi_ms),
        ).fetchall()
        out = []
        for cid, start_ms, end_ms, title, summary, notes in rows:
            (line_count,) = self.db.execute(
                "SELECT COUNT(*) FROM utterances WHERE start_ms >= ? AND start_ms <= ?",
                (start_ms, end_ms)).fetchone()
            out.append({
                "id": cid, "start": start_ms, "end": end_ms, "title": title,
                "summary": json.loads(summary or "[]"),
                "notes": json.loads(notes) if notes is not None else None,  # None: not written yet
                "lines": line_count,
                "sections": [
                    {"start": at, "level": level, "title": heading}
                    for at, level, heading in self.db.execute(
                        "SELECT start_ms, level, title FROM sections WHERE conversation_id = ? "
                        "ORDER BY start_ms, id", (cid,))
                ],
                "reminders": [
                    {"id": rid, "text": text, "status": status, "line": line_id}
                    for rid, text, status, line_id in self.db.execute(
                        "SELECT id, text, status, utterance_id FROM reminders "
                        "WHERE conversation_id = ? ORDER BY id", (cid,))
                ],
            })
        return out

    def reminders(self, include_done: bool = False) -> list[dict]:
        """Reminders still worth showing: suggested and accepted, newest first.

        With include_done, finished ones come too, so they can be shown ticked off
        and brought back. Dismissed ones never do.
        """
        statuses = "('suggested', 'accepted', 'done')" if include_done else "('suggested', 'accepted')"
        rows = self.db.execute(
            "SELECT r.id, r.text, r.status, r.created_ms, c.title, c.id, c.start_ms, "
            "u.id, u.text, u.start_ms FROM reminders r "
            "JOIN conversations c ON c.id = r.conversation_id "
            "LEFT JOIN utterances u ON u.id = r.utterance_id "
            f"WHERE r.status IN {statuses} ORDER BY c.start_ms DESC, r.id"
        ).fetchall()
        out = []
        for rid, text, status, when, title, cid, c_start, line_id, line_text, line_start in rows:
            anchor = line_start if line_id else c_start
            out.append({
                "id": rid, "text": text, "status": status, "when": when, "from": title,
                # Where to go: the day, the conversation, and the line when it is known.
                "day": datetime.fromtimestamp(anchor / 1000).strftime("%Y-%m-%d"),
                "conversation": cid,
                "start": c_start,
                "line": {"id": line_id, "text": line_text, "start": line_start} if line_id else None,
            })
        return out

    def set_reminder_status(self, reminder_id: int, status: str) -> None:
        if status not in REMINDER_STATUSES:
            raise ValueError(f"status must be one of {', '.join(REMINDER_STATUSES)}")
        cur = self.db.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id))
        if cur.rowcount == 0:
            raise ValueError(f"there is no reminder {reminder_id}")
        self.db.commit()

    # --- Ask threads ------------------------------------------------------------------

    def save_exchange(self, chat_id: int | None, question: str, answer: str, lines: dict) -> int:
        """Store a question and its answer, starting a new thread when chat_id is None."""
        now = int(time.time() * 1000)
        if chat_id is None or self.db.execute(
                "SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is None:
            title = " ".join(question.split())
            title = title if len(title) <= 80 else title[:79].rstrip() + "…"
            chat_id = self.db.execute(
                "INSERT INTO chats (title, created_ms, updated_ms) VALUES (?, ?, ?)",
                (title, now, now)).lastrowid
        self.db.executemany(
            "INSERT INTO chat_messages (chat_id, role, content, lines, created_ms) VALUES (?, ?, ?, ?, ?)",
            [(chat_id, "user", question, None, now), (chat_id, "assistant", answer, json.dumps(lines), now)])
        self.db.execute("UPDATE chats SET updated_ms = ? WHERE id = ?", (now, chat_id))
        self.db.commit()
        return chat_id

    def chats(self) -> list[dict]:
        """Saved threads, most recently used first."""
        return [{"id": cid, "title": title, "updated": updated, "questions": count}
                for cid, title, updated, count in self.db.execute(
                    "SELECT c.id, c.title, c.updated_ms, "
                    "(SELECT COUNT(*) FROM chat_messages m WHERE m.chat_id = c.id AND m.role = 'user') "
                    "FROM chats c ORDER BY c.updated_ms DESC, c.id DESC")]

    def chat_messages(self, chat_id: int) -> list[dict]:
        return [{"role": role, "content": content, "lines": json.loads(lines) if lines else {}}
                for role, content, lines in self.db.execute(
                    "SELECT role, content, lines FROM chat_messages WHERE chat_id = ? ORDER BY id",
                    (chat_id,))]

    def delete_chat(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        self.db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        self.db.commit()

    # --- re-timing imported lines -------------------------------------------------------

    def pending_retime(self) -> list[tuple[str, int, int]]:
        """(path, start_ms, duration_ms) of imported recordings whose lines are not re-timed."""
        return self.db.execute(
            "SELECT s.path, s.start_ms, s.duration_ms FROM segments s "
            # IN, not EXISTS: lines have no index by recording, and EXISTS read every
            # line once per recording (20 s on 75 hours of imports; this takes 0.02 s).
            "WHERE s.retimed IS NULL AND s.path IN "
            "(SELECT segment FROM utterances WHERE source IS NOT NULL) "
            "ORDER BY s.start_ms DESC").fetchall()

    def retime_recording(self, path: str, words: list[tuple[float, str]]) -> tuple[int, int]:
        """Move the imported lines of one recording to where their words are heard.

        Lines too short to find on their own move with the nearest line that was
        found. Each line's original timestamp is kept. Returns (lines found, lines).
        """
        import statistics

        (file_start,) = self.db.execute("SELECT start_ms FROM segments WHERE path = ?", (path,)).fetchone()
        rows = self.db.execute(
            "SELECT id, COALESCE(orig_start_ms, start_ms), end_ms - start_ms, text, source FROM utterances "
            "WHERE segment = ? AND source IS NOT NULL ORDER BY COALESCE(orig_start_ms, start_ms), id",
            (path,)).fetchall()
        claimed = [(orig - file_start) / 1000 for _id, orig, _dur, _text, _source in rows]
        found = locate_lines([(at, row[3]) for at, row in zip(claimed, rows)], words)
        shifts = [None if at is None else at - was for at, was in zip(found, claimed)]
        known = [(was, shift) for was, shift in zip(claimed, shifts) if shift is not None]
        typical = statistics.median(shift for _was, shift in known) if known else 0.0
        sources = set()
        for (uid, orig, duration, _text, source), was, shift in zip(rows, claimed, shifts):
            if shift is None:
                # Borrow the correction of the nearest located line, if one is close by.
                near = min(known, key=lambda item: abs(item[0] - was), default=None)
                shift = near[1] if near and abs(near[0] - was) <= 30 else typical
            start = max(file_start, orig + round(shift * 1000))
            self.db.execute(
                "UPDATE utterances SET start_ms = ?, end_ms = ?, orig_start_ms = ? WHERE id = ?",
                (start, start + duration, orig, uid))
            sources.add(source)
        # Conversations and their headings are placed by line times, so move them too.
        for source in sources:
            first, last, first_orig = self.db.execute(
                "SELECT MIN(start_ms), MAX(end_ms), MIN(COALESCE(orig_start_ms, start_ms)) "
                "FROM utterances WHERE source = ?", (source,)).fetchone()
            row = self.db.execute(
                "SELECT id FROM conversations WHERE engine = 'limitless' AND "
                "(source = ? OR (source IS NULL AND start_ms = ?))", (source, first_orig)).fetchone()
            if row is None:
                continue
            self.db.execute("UPDATE conversations SET start_ms = ?, end_ms = ?, source = ? WHERE id = ?",
                            (first, last, source, row[0]))
            for sid, at in self.db.execute(
                    "SELECT id, COALESCE(orig_start_ms, start_ms) FROM sections WHERE conversation_id = ?",
                    (row[0],)).fetchall():
                moved = self.db.execute(
                    "SELECT start_ms FROM utterances WHERE source = ? AND COALESCE(orig_start_ms, start_ms) >= ? "
                    "ORDER BY COALESCE(orig_start_ms, start_ms), id LIMIT 1", (source, at)).fetchone()
                if moved:
                    self.db.execute("UPDATE sections SET start_ms = ?, orig_start_ms = ? WHERE id = ?",
                                    (moved[0], at, sid))
        self.db.execute("UPDATE segments SET retimed = 1 WHERE path = ?", (path,))
        self.db.commit()
        return len(known), len(rows)

    def undo_retime(self) -> int:
        """Put every re-timed line, heading and conversation back at its imported time."""
        (count,) = self.db.execute(
            "SELECT COUNT(*) FROM utterances WHERE orig_start_ms IS NOT NULL").fetchone()
        sources = [row[0] for row in self.db.execute(
            "SELECT DISTINCT source FROM utterances WHERE orig_start_ms IS NOT NULL")]
        self.db.execute(
            "UPDATE utterances SET end_ms = orig_start_ms + (end_ms - start_ms), start_ms = orig_start_ms, "
            "orig_start_ms = NULL WHERE orig_start_ms IS NOT NULL")
        self.db.execute(
            "UPDATE sections SET start_ms = orig_start_ms, orig_start_ms = NULL WHERE orig_start_ms IS NOT NULL")
        for source in sources:
            first, last = self.db.execute(
                "SELECT MIN(start_ms), MAX(end_ms) FROM utterances WHERE source = ?", (source,)).fetchone()
            self.db.execute("UPDATE conversations SET start_ms = ?, end_ms = ? WHERE source = ?",
                            (first, last, source))
        self.db.execute("UPDATE segments SET retimed = NULL")
        self.db.commit()
        if count:
            self.rewrite_all_transcripts()
        return count
