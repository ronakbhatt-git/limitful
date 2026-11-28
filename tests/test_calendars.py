"""Calendars: private ICS addresses read into the Lifelog, onto day pages and into triage."""

import json
from datetime import datetime

import pytest

from limitful import brain, calendars, triage
from limitful.store import Store

DAY = "2026-11-20"
T0 = int(datetime(2026, 11, 20, 9, 0).timestamp() * 1000)
LATER = int(datetime(2026, 11, 23, 9, 0).timestamp() * 1000)
SECRET = "https://calendar.google.com/calendar/ical/me%40example.com/private-0123456789abcdef/basic.ics"

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:board@x
DTSTART;TZID=America/New_York:20261120T130000
DTEND;TZID=America/New_York:20261120T140000
SUMMARY:Board meeting: budget
LOCATION:Room 4
DESCRIPTION:Zoom passcode 99999
ORGANIZER;CN=Dana Example:mailto:dana@example.com
ATTENDEE;CN=Sam Example:mailto:sam@example.com
ATTENDEE:mailto:pat@example.com
END:VEVENT
BEGIN:VEVENT
UID:standup@x
DTSTART:20261116T090000
DURATION:PT15M
RRULE:FREQ=DAILY;COUNT=10
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:off@x
DTSTART;VALUE=DATE:20261120
DTEND;VALUE=DATE:20261121
SUMMARY:Teacher workday
END:VEVENT
BEGIN:VEVENT
UID:gone@x
DTSTART:20261120T160000Z
DTEND:20261120T170000Z
SUMMARY:Cancelled call
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    yield s
    s.close()


def _talk(store, cid, start, end, title="Talk"):
    seg = f"audio/{DAY}/rec_{cid}.ogg"
    store.db.execute("INSERT INTO segments (path, start_ms, duration_ms, first_page, last_page) "
                     "VALUES (?, ?, 60000, -1, -1)", (seg, start))
    store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES (?, ?, ?, ?)",
                     (seg, start + 1000, end, "so the budget for next year " * 5))
    store.db.execute("INSERT INTO conversations (id, start_ms, end_ms, status, title, summary, tier, tags, sensitive) "
                     "VALUES (?, ?, ?, 'done', ?, '[]', 'text', '[]', 0)", (cid, start, end, title))
    store.db.commit()


def _added(monkeypatch, store, data=ICS):
    calendars.add("Work", SECRET)
    monkeypatch.setattr(calendars, "_fetch", lambda url: data)
    return calendars.read(store)


def test_events_are_read_with_repeats_and_without_descriptions_or_cancellations(store, monkeypatch):
    _talk(store, 1, T0, T0 + 60_000)
    # Board, the workday, and the standups from the day before the first recording on (19th to 25th).
    assert _added(monkeypatch, store) == {"Work": 1 + 1 + 7}

    start = int(datetime.fromisoformat("2026-11-20T18:00:00+00:00").timestamp() * 1000)
    board = [e for e in calendars.between(store, start, start + 1) if e["title"].startswith("Board")]
    assert board[0]["attendees"] == ["Dana Example", "Sam Example", "pat@example.com"]
    assert board[0]["location"] == "Room 4"
    raw = json.dumps(store.db.execute("SELECT * FROM events").fetchall())
    assert "passcode" not in raw and "Cancelled call" not in raw

    standup = int(datetime(2026, 11, 20, 9, 0).timestamp() * 1000)  # floating time: local
    found = calendars.between(store, standup, standup + 60_000, all_day=False)
    assert [(e["title"], e["end_ms"] - e["start_ms"]) for e in found] == [("Standup", 15 * 60_000)]
    assert any(e["all_day"] and e["title"] == "Teacher workday" for e in calendars.between(store, T0, T0 + 1))


def test_reading_again_replaces_the_calendar_and_a_failure_keeps_what_was_there(store, monkeypatch):
    _talk(store, 1, T0, T0 + 60_000)
    _added(monkeypatch, store)
    smaller = ICS.split(b"BEGIN:VEVENT\nUID:standup")[0] + b"END:VCALENDAR\n"
    assert calendars.read(store) == {"Work": 9}
    monkeypatch.setattr(calendars, "_fetch", lambda url: smaller)
    assert calendars.read(store) == {"Work": 1}

    def offline(url):
        raise calendars.CalendarError("could not read calendar.google.com")
    monkeypatch.setattr(calendars, "_fetch", offline)
    assert "could not read" in calendars.read(store)["Work"]
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    assert calendars.remove(store, "Work")
    assert calendars.calendars() == [] and store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_the_secret_address_is_never_shown_and_must_be_https(monkeypatch):
    assert "private-0123456789" not in calendars.hint(SECRET)
    with pytest.raises(calendars.CalendarError, match="https"):
        calendars.add("Work", "http://example.com/cal.ics")
    calendars.add("Phone", "webcal://example.com/basic.ics")
    assert calendars.calendars()[0].url == "https://example.com/basic.ics"


def test_day_pages_show_the_days_events_and_which_meeting_a_conversation_was_in(store, monkeypatch, tmp_path):
    standup = int(datetime(2026, 11, 20, 9, 0).timestamp() * 1000)
    _talk(store, 1, standup + 60_000, standup + 10 * 60_000, title="Morning check-in")
    _added(monkeypatch, store)
    settings = brain.Settings(repo=tmp_path / "repo")

    brain.export(store, settings, now_ms=LATER, git=False)
    md = (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")
    assert "### Calendar" in md
    assert "- all day **Teacher workday** _(Work)_" in md
    assert "**Board meeting: budget** · Room 4 · with Dana Example, Sam Example, pat@example.com _(Work)_" in md
    assert "_conversation 1 · text · during Standup_" in md
    chart = md.split("```mermaid\n", 1)[1].split("```", 1)[0]
    assert "  section Calendar\n  Standup :done, e1, 09:00, 09:15\n" in chart
    assert "Board meeting - budget :done, e2," in chart
    assert "  section Conversations\n  Morning check-in :c1, 09:01, 09:10" in chart
    assert "Teacher workday" not in chart  # all-day events are listed, not drawn


def test_triage_sees_the_meeting_a_conversation_overlaps(store, monkeypatch):
    standup = int(datetime(2026, 11, 20, 9, 0).timestamp() * 1000)
    _talk(store, 1, standup + 60_000, standup + 10 * 60_000)
    store.db.execute("UPDATE conversations SET status = 'pending', tier = NULL WHERE id = 1")
    store.db.commit()
    _added(monkeypatch, store)

    [item] = triage.pending(store)
    assert [e["title"] for e in item["calendar"]] == ["Standup"]  # not the all-day workday
    assert "an invitation is not proof someone spoke" in triage.GUIDE
