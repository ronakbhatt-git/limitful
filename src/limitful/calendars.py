"""Calendars: read events from private ICS links so the Lifelog knows what was on the schedule.

Each calendar is a read-only ICS address, such as Google Calendar's "secret address in iCal
format" or a calendar Outlook publishes. Nothing is ever written back. The address is as good
as a password for that calendar, so it lives only in config.json on this computer and is
never printed in full.

Events are kept in the `events` table: title, time, location and who was invited, by name
(or address when the invitation gives no name). Descriptions are left out, since they often
hold meeting passcodes. Repeating events are expanded over the days the Lifelog covers, plus
the next two weeks. Every read replaces that calendar's events, so a cancelled meeting goes.

The export puts a day's events on its page and its timeline, and triage sees the events a
conversation overlaps, whose invitees are clues to who is speaking.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import config
from .store import Store

READ_EVERY_S = 6 * 3600  # how often the background sync reads the calendars again
AHEAD_DAYS = 14
MAX_BYTES = 50_000_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    calendar  TEXT    NOT NULL,   -- the name it was added under
    uid       TEXT    NOT NULL,
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    all_day   INTEGER NOT NULL DEFAULT 0,
    title     TEXT    NOT NULL,
    location  TEXT,
    attendees TEXT    NOT NULL DEFAULT '[]',  -- JSON list of names, or addresses without one
    PRIMARY KEY (calendar, uid, start_ms)
);
CREATE INDEX IF NOT EXISTS events_start ON events (start_ms);
"""


class CalendarError(Exception):
    pass


# The last read of each calendar while this program runs: name -> (time.time(), events or why it failed).
last_read: dict[str, tuple[float, int | str]] = {}


@dataclass
class Calendar:
    name: str
    url: str


def calendars() -> list[Calendar]:
    saved = config.load().get("calendars") or []
    return [Calendar(c["name"], c["url"]) for c in saved if isinstance(c, dict) and c.get("name") and c.get("url")]


def hint(url: str) -> str:
    """Enough of an address to tell calendars apart, never the secret part."""
    host = url.split("://", 1)[-1].split("/", 1)[0]
    return f"{host}/…{url[-6:]}" if len(url) > 30 else host


def add(name: str, url: str) -> None:
    name, url = " ".join(name.split()), url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url.removeprefix("webcal://")
    if not name:
        raise CalendarError("give the calendar a name, such as Personal or Telra")
    if not url.startswith("https://"):
        raise CalendarError("the address should start with https:// (or webcal://)")
    with config._updating:
        settings = config.load()
        kept = [c for c in settings.get("calendars") or [] if c.get("name") != name]
        settings["calendars"] = [*kept, {"name": name, "url": url}]
        config.save(settings)


def remove(store: Store, name: str) -> bool:
    with config._updating:
        settings = config.load()
        before = settings.get("calendars") or []
        settings["calendars"] = [c for c in before if c.get("name") != name]
        config.save(settings)
    _ensure(store)
    store.db.execute("DELETE FROM events WHERE calendar = ?", (name,))
    store.db.commit()
    return len(settings["calendars"]) < len(before)


def _ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Limitful calendar reader"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as err:
        if err.code in (401, 403, 404, 410):
            raise CalendarError(f"{hint(url)} turned the address away ({err.code}). It may have been reset or "
                                "mistyped: copy it again from the calendar's settings") from None
        raise CalendarError(f"{hint(url)} answered with an error ({err.code}); it is tried again later") from None
    except (urllib.error.URLError, OSError) as err:
        reason = getattr(err, "reason", err)
        raise CalendarError(f"could not reach {hint(url)} ({reason}). Check the internet connection; "
                            "it is tried again later") from None
    if len(data) > MAX_BYTES:
        raise CalendarError(f"{hint(url)} is larger than {MAX_BYTES // 1_000_000} MB")
    return data


def _ms(value) -> tuple[int, bool]:
    """Milliseconds for an event time, and whether it is a whole day. Floating times are local."""
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000), False
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day).timestamp() * 1000), True
    raise ValueError(f"not a time: {value!r}")


def _who(attendee) -> str | None:
    name = str(attendee.params.get("CN") or "").strip().strip('"')
    address = str(attendee).removeprefix("mailto:").removeprefix("MAILTO:").strip()
    return name or address or None


def parse(name: str, data: bytes, since: date, until: date) -> list[tuple]:
    """Rows for the events table: every event, repeats expanded, that starts between the two days."""
    import icalendar
    import recurring_ical_events

    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as err:
        raise CalendarError(f"{name} is not a calendar file: {err}") from None
    rows = {}
    for event in recurring_ical_events.of(cal).between(since, until):
        if str(event.get("STATUS") or "").upper() == "CANCELLED" or event.get("DTSTART") is None:
            continue
        start, all_day = _ms(event.decoded("DTSTART"))
        if event.get("DTEND") is not None:
            end, _ = _ms(event.decoded("DTEND"))
        elif event.get("DURATION") is not None:
            end = start + int(event.decoded("DURATION").total_seconds() * 1000)
        else:
            end = start + (86_400_000 if all_day else 0)
        attendees = event.get("ATTENDEE") or []
        if not isinstance(attendees, list):
            attendees = [attendees]
        people = [who for who in (_who(a) for a in attendees) if who]
        organizer = _who(event["ORGANIZER"]) if event.get("ORGANIZER") is not None else None
        if organizer and organizer not in people:
            people.insert(0, organizer)
        title = " ".join(str(event.get("SUMMARY") or "").split())[:200] or "Busy"
        location = " ".join(str(event.get("LOCATION") or "").split())[:200] or None
        uid = str(event.get("UID") or f"{title}-{start}")
        rows[(uid, start)] = (name, uid, start, max(end, start), int(all_day), title, location,
                              json.dumps(list(dict.fromkeys(people)), ensure_ascii=False))
    return list(rows.values())


def _window(store: Store) -> tuple[date, date]:
    first = store.db.execute("SELECT MIN(start_ms) FROM utterances").fetchone()[0]
    since = datetime.fromtimestamp(first / 1000).date() if first else date.today()
    return since - timedelta(days=1), date.today() + timedelta(days=AHEAD_DAYS)


def read(store: Store, only: str | None = None) -> dict[str, int | str]:
    """Read every calendar (or one) again. Returns how many events each has, or why it failed."""
    _ensure(store)
    since, until = _window(store)
    out: dict[str, int | str] = {}
    for cal in calendars():
        if only and cal.name != only:
            continue
        try:
            rows = parse(cal.name, _fetch(cal.url), since, until)
        except CalendarError as err:
            out[cal.name] = str(err)
            last_read[cal.name] = (time.time(), out[cal.name])
            continue
        with store.db:
            store.db.execute("DELETE FROM events WHERE calendar = ?", (cal.name,))
            store.db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        out[cal.name] = len(rows)
        last_read[cal.name] = (time.time(), len(rows))
    return out


def overview(store: Store) -> list[dict]:
    """What the Settings page shows: each calendar by name, never its address."""
    _ensure(store)
    out = []
    for cal in calendars():
        when, result = last_read.get(cal.name, (None, None))
        count = store.db.execute("SELECT COUNT(*) FROM events WHERE calendar = ?", (cal.name,)).fetchone()[0]
        out.append({"name": cal.name, "hint": hint(cal.url), "events": count,
                    "read_ms": int(when * 1000) if when else None,
                    "error": result if isinstance(result, str) else None})
    return out


def between(store: Store, start_ms: int, end_ms: int, *, all_day: bool = True) -> list[dict]:
    """Events that overlap this stretch of time, earliest first."""
    _ensure(store)
    rows = store.db.execute(
        "SELECT calendar, start_ms, end_ms, all_day, title, location, attendees FROM events "
        "WHERE start_ms < ? AND end_ms > ? " + ("" if all_day else "AND all_day = 0 ") +
        "ORDER BY all_day DESC, start_ms, title", (end_ms, start_ms)).fetchall()
    return [{"calendar": c, "start_ms": s, "end_ms": e, "all_day": bool(a), "title": t, "location": loc,
             "attendees": json.loads(people or "[]")} for c, s, e, a, t, loc, people in rows]


def due(last: float | None) -> bool:
    return bool(calendars()) and (last is None or time.monotonic() - last >= READ_EVERY_S)
