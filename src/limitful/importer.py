"""Import a data export from the Limitless service.

The export is a zip with:

    lifelogs/YYYY/MM/DD/<local time>_<Title>.md   one conversation each
    audio/pendant/YYYY/MM/DD/<date>_<HH>h<MM>m<SS>s_to_<HH>h<MM>m<SS>s.ogg
    reminders/<id>.json
    chats/, pendants/, user/                       (not imported yet)

A lifelog is Markdown: a `# Title`, `##`/`###` section headings, and one line
per utterance: `> [speaker](#startMs=..&endMs=..): text`. The speaker is a
number that only means something within that conversation.

Limitless's transcripts are kept as they are; nothing is re-transcribed.
"""

from __future__ import annotations

import bisect
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .store import Store

_UTTERANCE = re.compile(r"^> \[([^\]]*)\]\(#startMs=(\d+)&endMs=(\d+)\): ?(.*)$")
_AUDIO = re.compile(
    r"(\d{4}-\d\d-\d\d)_(\d\d)h(\d\d)m(\d\d)s_to_(\d\d)h(\d\d)m(\d\d)s\.ogg$")
_EPOCH = datetime(1970, 1, 1)
_REMINDER_STATUS = {
    "suggested": "suggested", "accepted": "accepted", "completed": "done",
    "rejected": "dismissed", "dismissed": "dismissed",
}


@dataclass
class Lifelog:
    key: str
    title: str
    sections: list[str]
    lines: list[tuple[str, int, int, str]]  # speaker label, start_ms, end_ms, text
    headings: list[tuple[int, int, str]] = field(default_factory=list)  # start_ms, level, title


@dataclass
class ImportResult:
    conversations: int = 0
    lines: int = 0
    lines_with_audio: int = 0
    audio_files: int = 0
    audio_hours: float = 0.0
    reminders: int = 0
    skipped_existing: int = 0
    utc_offset_minutes: int | None = None
    not_imported: list[str] = field(default_factory=list)


def parse_lifelog(key: str, text: str) -> Lifelog | None:
    title = ""
    sections: list[str] = []
    lines: list[tuple[str, int, int, str]] = []
    headings: list[tuple[int, int, str]] = []
    waiting: list[tuple[int, str]] = []  # headings seen since the last line
    for raw in text.splitlines():
        match = _UTTERANCE.match(raw)
        if match:
            start, end = int(match.group(2)), int(match.group(3))
            said = match.group(4).strip()
            if said:
                lines.append((match.group(1).strip(), start, max(end, start + 1), said))
                # A heading belongs to the first line that follows it.
                headings += [(start, level, heading) for level, heading in waiting]
                waiting = []
        elif raw.startswith("# ") and not title:
            title = raw[2:].strip()
        elif raw.startswith("## "):
            sections.append(raw[3:].strip())
            waiting.append((2, raw[3:].strip()))
        elif raw.startswith("### "):
            waiting.append((3, raw[4:].strip()))
    if not lines:
        return None
    lines.sort(key=lambda item: item[1])
    return Lifelog(key, title or "Untitled conversation", sections, lines, headings)


def _audio_times(name: str) -> tuple[int, int] | None:
    """(start, end) of an audio file in ms, read as if its name were in UTC."""
    match = _AUDIO.search(name)
    if not match:
        return None
    day = datetime.strptime(match.group(1), "%Y-%m-%d")
    start = day + timedelta(hours=int(match.group(2)), minutes=int(match.group(3)),
                            seconds=int(match.group(4)))
    end = day + timedelta(hours=int(match.group(5)), minutes=int(match.group(6)),
                          seconds=int(match.group(7)))
    if end <= start:
        end += timedelta(days=1)  # the recording ran past midnight
    return (int((start - _EPOCH).total_seconds() * 1000),
            int((end - _EPOCH).total_seconds() * 1000))


def best_utc_offset(audio: list[tuple[int, int]], utterance_starts: list[int]) -> int:
    """The time zone (minutes east of UTC) the audio file names were written in.

    The export does not say, and its settings file can be wrong, so pick the
    offset under which the most transcript lines fall inside a recording.
    """
    if not audio or not utterance_starts:
        return 0
    starts = sorted(utterance_starts)

    def covered(offset: int) -> int:
        total = 0
        for a, b in audio:
            lo = bisect.bisect_left(starts, a - offset * 60000)
            hi = bisect.bisect_right(starts, b - offset * 60000 + 1000)
            total += hi - lo
        return total

    return max(range(-12 * 60, 14 * 60 + 1, 15), key=lambda o: (covered(o), -abs(o)))


def import_limitless(
    store: Store, zip_path: Path, log: Callable[[str], None] = print
) -> ImportResult:
    result = ImportResult()
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()

        lifelogs = []
        for name in sorted(n for n in names if n.startswith("lifelogs/") and n.endswith(".md")):
            parsed = parse_lifelog(f"limitless:{name}", archive.read(name).decode("utf-8", "replace"))
            if parsed:
                lifelogs.append(parsed)

        audio = [(n, _audio_times(n)) for n in names if n.startswith("audio/") and n.endswith(".ogg")]
        audio = [(n, t) for n, t in audio if t]
        offset = best_utc_offset([t for _n, t in audio],
                                 [line[1] for log_ in lifelogs for line in log_.lines])
        result.utc_offset_minutes = offset if audio else None

        # --- audio: copy each recording into the data folder -----------------------
        log(f"Copying {len(audio)} recordings...")
        for name, (start, end) in audio:
            start, end = start - offset * 60000, end - offset * 60000
            local = datetime.fromtimestamp(start / 1000)
            rel = Path("audio") / local.strftime("%Y-%m-%d") / f"limitless_{Path(name).name}"
            target = store.data_dir / rel
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                # Written under a name we chose, so a hostile zip cannot escape the folder.
                target.write_bytes(archive.read(name))
            cur = store.db.execute(
                "INSERT OR IGNORE INTO segments (path, start_ms, duration_ms, first_page, last_page) "
                "VALUES (?, ?, ?, -1, -1)",
                (rel.as_posix(), start, end - start))
            if cur.rowcount:
                result.audio_files += 1
                result.audio_hours += (end - start) / 3_600_000
        store.db.commit()

        segments = store.db.execute(
            "SELECT start_ms, start_ms + duration_ms, path FROM segments ORDER BY start_ms").fetchall()
        segment_starts = [row[0] for row in segments]

        def recording_at(ms: int) -> str:
            i = bisect.bisect_right(segment_starts, ms) - 1
            # Look back a few files: a long recording can start before a short one.
            for j in range(i, max(i - 4, -1), -1):
                if segments[j][0] <= ms < segments[j][1] + 1000:
                    return segments[j][2]
            return ""

        # --- conversations and their lines ------------------------------------------
        for lifelog in lifelogs:
            start = lifelog.lines[0][1]
            end = max(line[2] for line in lifelog.lines)
            if store.db.execute("SELECT 1 FROM imports WHERE key = ?", (lifelog.key,)).fetchone():
                result.skipped_existing += 1
                # Imports made before section headings were kept: add them now.
                row = store.db.execute(
                    "SELECT id FROM conversations WHERE engine = 'limitless' AND start_ms = ? "
                    "AND id NOT IN (SELECT conversation_id FROM sections)", (start,)).fetchone()
                if row:
                    store.db.executemany(
                        "INSERT INTO sections (conversation_id, start_ms, level, title) VALUES (?, ?, ?, ?)",
                        [(row[0], at, level, heading) for at, level, heading in lifelog.headings])
                continue
            cur = store.db.execute(
                "INSERT INTO conversations (start_ms, end_ms, status, title, summary, engine) "
                "VALUES (?, ?, 'done', ?, ?, 'limitless')",
                (start, end, lifelog.title, json.dumps(lifelog.sections)))
            store.db.executemany(
                "INSERT INTO sections (conversation_id, start_ms, level, title) VALUES (?, ?, ?, ?)",
                [(cur.lastrowid, at, level, heading) for at, level, heading in lifelog.headings])
            rows = []
            for speaker, line_start, line_end, text in lifelog.lines:
                segment = recording_at(line_start)
                result.lines_with_audio += bool(segment)
                rows.append((segment, line_start, line_end, text, lifelog.key, lifelog.key, speaker))
            store.db.executemany(
                "INSERT INTO utterances (segment, start_ms, end_ms, text, source, speaker_group, source_speaker) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            store.db.execute("INSERT INTO imports VALUES (?)", (lifelog.key,))
            result.conversations += 1
            result.lines += len(rows)
        # Imported audio is already transcribed; do not run Whisper over it.
        store.db.execute(
            "INSERT OR IGNORE INTO transcriptions "
            "SELECT path, 'limitless', 0 FROM segments WHERE path LIKE 'audio/%/limitless_%'")
        store.db.commit()

        # --- reminders ----------------------------------------------------------------
        for name in sorted(n for n in names if n.startswith("reminders/") and n.endswith(".json")):
            key = f"limitless:{name}"
            if store.db.execute("SELECT 1 FROM imports WHERE key = ?", (key,)).fetchone():
                continue
            try:
                item = json.loads(archive.read(name))
                text = " ".join(str(item["title"]).split())
                when = int((item.get("source") or {}).get("startTimeMs") or item["createdTimeMs"])
            except (ValueError, KeyError, TypeError):
                continue
            row = store.db.execute(
                "SELECT id FROM conversations ORDER BY "
                "CASE WHEN ? BETWEEN start_ms AND end_ms THEN 0 ELSE 1 END, ABS(start_ms - ?) LIMIT 1",
                (when, when)).fetchone()
            if not text or row is None:
                continue
            store.db.execute(
                "INSERT INTO reminders (conversation_id, text, status, created_ms) VALUES (?, ?, ?, ?)",
                (row[0], text, _REMINDER_STATUS.get(item.get("status"), "suggested"),
                 int(item.get("createdTimeMs") or when)))
            store.db.execute("INSERT INTO imports VALUES (?)", (key,))
            result.reminders += 1
        store.db.commit()

        for kind in ("chats", "pendants", "user"):
            count = sum(1 for n in names if n.startswith(kind + "/") and not n.endswith("/"))
            if count:
                result.not_imported.append(f"{count} file(s) in {kind}/")

    store.link_reminders_to_lines()
    store.rewrite_all_transcripts()
    return result
