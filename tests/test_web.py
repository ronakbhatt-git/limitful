import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from limitful.protocol import FlashPage
from limitful.store import Store
from limitful.transcribe import Utterance
from limitful.web import make_server

from wire import flash_page, opus_frame

T0 = int(datetime(2026, 11, 13, 9, 7, 0).timestamp() * 1000)


class Words:
    name = "fake"

    def transcribe(self, audio_path):
        return [Utterance(0, 900, "buy 100% oat_milk"), Utterance(1200, 1900, "call Joseph")]


@pytest.fixture
def site(tmp_path):
    store = Store(tmp_path)
    frames = [opus_frame(i) for i in range(50)]
    for index, ts in [(0, T0), (1, T0 + 1000), (2, T0 + 3_600_000)]:
        store.add_page(FlashPage(index=index, session=1, seq=0, data=flash_page(ts, frames)))
    store.commit()
    store.export_audio()
    store.transcribe_pending(Words())
    store.db.execute("INSERT INTO speakers (id, name, voice_sum, lines) VALUES (1, 'Speaker 1', x'00000000', 2)")
    store.db.execute("INSERT INTO speakers (id, name, voice_sum, lines) VALUES (2, 'Joseph', x'00000000', 1)")
    store.db.execute("UPDATE utterances SET speaker_id = 1 WHERE text LIKE 'buy%'")
    store.db.execute("INSERT INTO conversations (id, start_ms, end_ms, status, title, summary) "
                     "VALUES (1, ?, ?, 'done', 'Shopping and a call', '[\"Oat milk is needed.\"]')",
                     (T0, T0 + 2000))
    store.db.execute("INSERT INTO reminders (id, conversation_id, text, created_ms) VALUES (1, 1, 'Call Joseph', ?)", (T0,))
    store.db.commit()
    assert store.link_reminders_to_lines() == 1  # "Call Joseph" is found in the transcript
    store.close()
    (tmp_path / "secret.txt").write_text("private")

    server = make_server(tmp_path, 0, link_speakers=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def fetch(path, headers=None, data=None):
        req = urllib.request.Request(base + path, headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, dict(res.headers), res.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    yield fetch
    server.shutdown()
    server.server_close()


def test_page_and_days(site):
    status, headers, body = site("/")
    assert status == 200 and b"Lifelog" in body and "text/html" in headers["Content-Type"]
    assert json.loads(site("/api/days")[2]) == [{"day": "2026-11-13", "lines": 4, "talks": 1, "headline": None}]


def test_day_lists_lines_with_audio_offsets(site):
    lines = json.loads(site("/api/day/2026-11-13")[2])
    assert [u["text"] for u in lines] == ["buy 100% oat_milk", "call Joseph"] * 2
    assert (lines[0]["start"], lines[1]["offset"]) == (T0, 1200)
    assert lines[2]["start"] == T0 + 3_600_000 and lines[2]["offset"] == 0
    assert lines[0]["audio"] != lines[2]["audio"]
    assert json.loads(site("/api/day/2026-11-14")[2]) == []
    assert site("/api/day/yesterday")[0] == 400


def test_search_is_literal_and_case_insensitive(site):
    hits = json.loads(site("/api/search?q=JOSEPH")[2])
    assert len(hits) == 2 and hits[0]["day"] == "2026-11-13"
    assert hits[0]["start"] > hits[1]["start"]  # newest first
    assert len(json.loads(site("/api/search?q=100%25")[2])) == 2
    assert json.loads(site("/api/search?q=oat%20milk")[2]) == []  # "_" is not a wildcard
    assert json.loads(site("/api/search?q=%25")[2]) != [] and json.loads(site("/api/search?q=")[2]) == []


def test_search_can_be_kept_to_one_named_person(site):
    assert len(json.loads(site("/api/search?q=JOSEPH")[2])) == 2
    site("/api/speakers/1", JSON, b'{"name": "Andrei"}')
    mine = json.loads(site("/api/search?q=&by=andrei")[2])  # no words: everything that person said
    assert mine and {hit["speaker"]["name"] for hit in mine} == {"Andrei"}
    both = json.loads(site("/api/search?q=buy&by=Andrei")[2])
    assert both and all("buy" in hit["text"].lower() and hit["speaker"]["name"] == "Andrei" for hit in both)
    assert len(both) <= len(mine)
    assert json.loads(site("/api/search?q=buy&by=Nobody")[2]) == []
    assert json.loads(site("/api/search?q=&by=")[2]) == []
    # A name is matched whole: "%" in it is not a wildcard.
    assert json.loads(site("/api/search?q=&by=%25")[2]) == []


def test_people_are_listed_with_where_each_was_heard(site):
    assert json.loads(site("/api/persons")[2]) == []  # nobody named yet: "Speaker 1" is not a name
    site("/api/speakers/1", JSON, b'{"name": "Andrei"}')
    people = json.loads(site("/api/persons")[2])
    (andrei,) = people  # Joseph is named but has said nothing, so there is nothing to show for him
    assert andrei["lines"] >= 1 and andrei["ms"] > 0 and andrei["days"] == 1 and andrei["first"] <= andrei["last"]
    assert andrei["conversations"] == 1
    one = json.loads(site(f"/api/persons/{andrei['id']}")[2])
    assert one["name"] == "Andrei"
    (talk,) = one["conversations"]
    assert (talk["title"], talk["day"]) == ("Shopping and a call", "2026-11-13") and talk["ms"] > 0 and talk["lines"] >= 1
    assert site("/api/persons/999999")[0] == 400


def test_audio_supports_ranges(site):
    path = json.loads(site("/api/day/2026-11-13")[2])[0]["audio"]
    status, headers, body = site(path)
    assert status == 200 and body[:4] == b"OggS" and headers["Accept-Ranges"] == "bytes"
    status, headers, part = site(path, {"Range": "bytes=4-9"})
    assert status == 206 and part == body[4:10]
    assert headers["Content-Range"] == f"bytes 4-9/{len(body)}"
    assert site(path, {"Range": "bytes=-5"})[2] == body[-5:]
    assert site(path, {"Range": f"bytes={len(body)}-"})[0] == 416


def test_only_audio_files_are_served(site):
    assert site("/audio/../secret.txt")[0] == 404
    assert site("/audio/%2e%2e/secret.txt")[0] == 404
    assert site("/audio/../pendant.db")[0] == 404
    assert site("/secret.txt")[0] == 404


def test_other_hosts_are_refused(site):
    assert site("/api/days", {"Host": "evil.example"})[0] == 403
    assert site("/api/days", {"Host": "localhost:9"})[0] == 200


JSON = {"Content-Type": "application/json"}


def test_speakers_are_shown_and_can_be_renamed(site):
    lines = json.loads(site("/api/day/2026-11-13")[2])
    assert lines[0]["speaker"] == {"id": 1, "name": "Speaker 1", "line": lines[0]["id"]}
    assert lines[1]["speaker"] is None
    status, _h, body = site("/api/speakers/1", JSON, b'{"name": "Andrei"}')
    assert (status, json.loads(body)) == (200, {"id": 1})
    assert json.loads(site("/api/day/2026-11-13")[2])[0]["speaker"]["name"] == "Andrei"
    # Using a name that exists merges into that speaker.
    assert json.loads(site("/api/speakers/1", JSON, b'{"name": "joseph"}')[2]) == {"id": 2}
    first = json.loads(site("/api/day/2026-11-13")[2])[0]
    assert (first["speaker"]["id"], first["speaker"]["name"]) == (2, "Joseph")
    # Naming one line only: the other line is untouched, and a new person appears.
    second = json.loads(site("/api/day/2026-11-13")[2])[1]["id"]
    assert site(f"/api/lines/{second}/speaker", JSON, b'{"name": "Karen"}')[0] == 200
    after = json.loads(site("/api/day/2026-11-13")[2])
    assert after[1]["speaker"]["name"] == "Karen" and after[0]["speaker"]["name"] == "Joseph"
    assert site("/api/lines/999999/speaker", JSON, b'{"name": "X"}')[0] == 400


def test_rename_rejects_bad_requests(site):
    assert site("/api/speakers/1", JSON, b'{"name": "  "}')[0] == 400
    assert site("/api/speakers/1", JSON, b'{"name": 5}')[0] == 400
    assert site("/api/speakers/1", JSON, b'[1]')[0] == 400
    assert site("/api/speakers/99", JSON, b'{"name": "X"}')[0] == 400
    assert site("/api/speakers/1", {"Content-Type": "text/plain"}, b'{"name": "X"}')[0] == 415
    assert site("/api/speakers/1", dict(JSON, Host="evil.example"), b'{"name": "X"}')[0] == 403
    assert site("/api/other", JSON, b'{}')[0] == 404
    assert json.loads(site("/api/day/2026-11-13")[2])[0]["speaker"]["name"] == "Speaker 1"


def test_a_conversation_can_be_renamed(site, tmp_path):
    (talk,) = json.loads(site("/api/conversations/2026-11-13")[2])
    status, _h, body = site(f"/api/conversations/{talk['id']}", JSON, b'{"title": "  Groceries,   then Joseph "}')
    assert (status, json.loads(body)) == (200, {"id": talk["id"], "title": "Groceries, then Joseph"})
    (after,) = json.loads(site("/api/conversations/2026-11-13")[2])
    assert (after["title"], after["summary"]) == ("Groceries, then Joseph", talk["summary"])
    assert site(f"/api/conversations/{talk['id']}", JSON, b'{"title": "   "}')[0] == 400
    assert site(f"/api/conversations/{talk['id']}", JSON, b'{"title": 7}')[0] == 400
    assert site("/api/conversations/999999", JSON, b'{"title": "X"}')[0] == 400
    assert site(f"/api/conversations/{talk['id']}", dict(JSON, Host="evil.example"), b'{"title": "X"}')[0] == 403
    assert json.loads(site("/api/conversations/2026-11-13")[2])[0]["title"] == "Groceries, then Joseph"


def test_conversations_and_reminders(site):
    (talk,) = json.loads(site("/api/conversations/2026-11-13")[2])
    assert (talk["title"], talk["summary"], talk["start"]) == ("Shopping and a call", ["Oat milk is needed."], T0)
    line = json.loads(site("/api/day/2026-11-13")[2])[1]
    assert talk["reminders"] == [{"id": 1, "text": "Call Joseph", "status": "suggested", "line": line["id"]}]
    assert json.loads(site("/api/conversations/2026-11-14")[2]) == []
    assert site("/api/conversations/soon")[0] == 400

    (reminder,) = json.loads(site("/api/reminders")[2])
    assert (reminder["text"], reminder["from"], reminder["status"]) == ("Call Joseph", "Shopping and a call", "suggested")
    assert (reminder["day"], reminder["conversation"], reminder["start"]) == ("2026-11-13", 1, T0)
    assert reminder["line"] == {"id": line["id"], "text": "call Joseph", "start": line["start"]}
    status, _h, body = site("/api/reminders/1", JSON, b'{"status": "accepted"}')
    assert (status, json.loads(body)) == (200, {"id": 1, "status": "accepted"})
    assert json.loads(site("/api/reminders")[2])[0]["status"] == "accepted"
    # Finished reminders leave the working list but can still be listed, to tick back.
    assert site("/api/reminders/1", JSON, b'{"status": "done"}')[0] == 200
    assert json.loads(site("/api/reminders")[2]) == []
    assert [r["status"] for r in json.loads(site("/api/reminders?done=1")[2])] == ["done"]
    assert site("/api/reminders/1", JSON, b'{"status": "dismissed"}')[0] == 200
    assert json.loads(site("/api/reminders?done=1")[2]) == []


def test_reminder_updates_reject_bad_requests(site):
    assert site("/api/reminders/1", JSON, b'{"status": "later"}')[0] == 400
    assert site("/api/reminders/1", JSON, b'{}')[0] == 400
    assert site("/api/reminders/42", JSON, b'{"status": "done"}')[0] == 400
    assert site("/api/reminders/1", {"Content-Type": "text/plain"}, b'{"status": "done"}')[0] == 415
    assert json.loads(site("/api/reminders")[2])[0]["status"] == "suggested"


ZIP = {"Content-Type": "application/zip"}


def test_import_an_export_through_the_page(site, tmp_path):
    from test_importer import make_zip

    data = make_zip(tmp_path / "export.zip").read_bytes()
    status, _h, body = site("/api/import", ZIP, data)
    assert status == 200
    got = json.loads(body)
    assert (got["conversations"], got["lines"], got["recordings"], got["reminders"]) == (1, 3, 2, 1)
    assert {d["day"] for d in json.loads(site("/api/days")[2])} >= {"2026-11-08", "2026-11-13"}
    assert not list((tmp_path / "imports").glob("*.zip"))  # the upload is not kept
    assert json.loads(site("/api/status")[2])["running"] is False

    again = json.loads(site("/api/import", ZIP, data)[2])
    assert (again["conversations"], again["skipped"]) == (0, 1)


def test_import_rejects_bad_uploads(site):
    assert site("/api/import", ZIP, b"this is not a zip")[0] == 400
    assert site("/api/import", ZIP, b"")[0] == 400
    assert site("/api/import", JSON, b"{}")[0] == 415
    assert site("/api/import", {"Content-Type": "text/plain"}, b"x")[0] == 415
    assert site("/api/import", dict(ZIP, Host="evil.example"), b"x")[0] == 403
    assert site("/api/speakers/1", ZIP, b"x")[0] == 415
    assert json.loads(site("/api/status")[2])["running"] is False


def test_server_upgrades_an_old_database_and_serves_an_empty_folder(tmp_path):
    import sqlite3

    old = tmp_path / "old"
    old.mkdir()
    db = sqlite3.connect(old / "pendant.db")
    # The layout of the first release: no speaker, source or conversation columns.
    db.executescript("""
        CREATE TABLE segments (path TEXT PRIMARY KEY, start_ms INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
                               first_page INTEGER NOT NULL, last_page INTEGER NOT NULL);
        CREATE TABLE utterances (id INTEGER PRIMARY KEY, segment TEXT NOT NULL, start_ms INTEGER NOT NULL,
                                 end_ms INTEGER NOT NULL, text TEXT NOT NULL);
    """)
    db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES ('', ?, ?, 'hello')", (T0, T0 + 900))
    db.commit()
    db.close()

    for folder, expected in ((old, ["hello"]), (tmp_path / "brand-new", [])):
        server = make_server(folder, 0, link_speakers=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/api/day/2026-11-13") as res:
            assert [u["text"] for u in json.loads(res.read())] == expected
        with urllib.request.urlopen(base + "/api/conversations/2026-11-13") as res:
            assert json.loads(res.read()) == []
        server.shutdown()
        server.server_close()


def test_settings_save_a_key_without_ever_returning_it(site, monkeypatch, tmp_path):
    import os
    import stat

    from limitful import config, digest

    checked = []
    monkeypatch.setattr(digest, "check_claude", lambda key=None, model=None: checked.append(key))
    monkeypatch.setattr(digest, "check_ollama", lambda: checked.append("ollama"))
    assert json.loads(site("/api/settings")[2])["provider"] is None

    status, _h, body = site("/api/settings", JSON, b'{"provider": "claude", "api_key": " sk-ant-api03-SECRETSECRET1234 "}')
    assert status == 200 and b"SECRETSECRET" not in body
    got = json.loads(body)
    assert (got["provider"], got["has_key"], got["key_hint"]) == ("claude", True, "ends in 1234")
    assert checked == ["sk-ant-api03-SECRETSECRET1234"]
    assert b"SECRETSECRET" not in site("/api/settings")[2]
    assert config.api_key() == "sk-ant-api03-SECRETSECRET1234"
    assert sys.platform == "win32" or stat.S_IMODE(os.stat(config.config_path()).st_mode) == 0o600  # no POSIX modes on Windows
    assert not list(tmp_path.glob("**/pendant.db-key*")) and "SECRET" not in (tmp_path / "pendant.db").read_bytes().decode("latin1")

    # Changing the provider alone keeps the key; an empty key removes it.
    assert json.loads(site("/api/settings", JSON, b'{"provider": "ollama"}')[2])["has_key"] is True
    assert checked[-1] == "ollama"
    after = json.loads(site("/api/settings", JSON, b'{"provider": "", "api_key": ""}')[2])
    assert (after["provider"], after["has_key"]) == (None, False)


def test_settings_choose_the_claude_model(site, monkeypatch):
    from limitful import config, digest

    checked = []
    monkeypatch.setattr(digest, "check_claude", lambda key=None, model=None: checked.append(model))
    got = json.loads(site("/api/settings")[2])
    assert got["claude_model"] == "claude-opus-5-5" and got["claude_models"][0] == "claude-haiku-5-5"
    got = json.loads(site("/api/settings", JSON, b'{"provider": "claude", "claude_model": "claude-haiku-5-5"}')[2])
    assert got["claude_model"] == "claude-haiku-5-5" and checked == ["claude-haiku-5-5"]
    assert config.claude_model() == "claude-haiku-5-5"
    status, _h, body = site("/api/settings", JSON, b'{"provider": "claude", "claude_model": "gpt-5"}')
    assert status == 400 and config.claude_model() == "claude-haiku-5-5"


def test_settings_name_the_notes_vault_once_the_export_has_a_repo(site, tmp_path):
    from limitful import brain

    assert json.loads(site("/api/settings")[2])["notes_vault"] is None
    (tmp_path / "lifelog").mkdir()
    brain.save_settings(tmp_path / "lifelog", None, None)
    assert json.loads(site("/api/settings")[2])["notes_vault"] == str((tmp_path / "lifelog").resolve())


def test_calendars_are_added_read_and_removed_from_settings_without_showing_the_address(site, monkeypatch):
    from limitful import calendars

    secret = "https://calendar.google.com/calendar/ical/me/private-abcdef0123456789/basic.ics"
    ics = (b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nBEGIN:VEVENT\nUID:a@x\n"
           b"DTSTART:20261120T140000Z\nDTEND:20261120T150000Z\nSUMMARY:Board\nEND:VEVENT\nEND:VCALENDAR\n")
    monkeypatch.setattr(calendars, "_window", lambda store: (calendars.date(2026, 11, 1), calendars.date(2026, 12, 1)))
    monkeypatch.setattr(calendars, "_fetch", lambda url: ics)
    assert json.loads(site("/api/calendars")[2]) == []

    status, _h, body = site("/api/calendars", JSON, json.dumps({"action": "add", "name": "Personal", "url": secret}).encode())
    assert status == 200 and b"private-abcdef" not in body
    [cal] = json.loads(body)
    assert (cal["name"], cal["events"], cal["error"]) == ("Personal", 1, None) and cal["read_ms"]
    assert b"private-abcdef" not in site("/api/calendars")[2]

    def offline(url):
        raise calendars.CalendarError("could not read calendar.google.com: timed out")
    monkeypatch.setattr(calendars, "_fetch", offline)
    [cal] = json.loads(site("/api/calendars", JSON, b'{"action": "read"}')[2])
    assert cal["events"] == 1 and "timed out" in cal["error"]  # the old events stay

    status, _h, body = site("/api/calendars", JSON, b'{"action": "add", "name": "Bad", "url": "http://x"}')
    assert status == 400 and b"https" in body
    assert json.loads(site("/api/calendars", JSON, b'{"action": "remove", "name": "Personal"}')[2]) == []


def test_settings_reject_a_key_anthropic_refuses(site, monkeypatch):
    from limitful import config, digest

    def refuse(key=None, model=None):
        raise digest.DigestError("Anthropic did not accept that key")

    monkeypatch.setattr(digest, "check_claude", refuse)
    status, _h, body = site("/api/settings", JSON, b'{"provider": "claude", "api_key": "sk-ant-wrong-000000"}')
    assert status == 400 and b"Nothing was saved" in body
    assert config.load() == {}

    def absent():
        raise digest.DigestError("could not reach Ollama at http://localhost:11434. Start it with `ollama serve`")

    monkeypatch.setattr(digest, "check_ollama", absent)
    status, _h, body = site("/api/settings", JSON, b'{"provider": "ollama"}')
    assert status == 400 and b"ollama serve" in body and b"Nothing was saved" in body
    assert config.load() == {}
    assert site("/api/settings", JSON, b'{"provider": "gpt"}')[0] == 400
    assert site("/api/settings", {"Content-Type": "text/plain"}, b'{}')[0] == 415
    assert site("/api/settings", dict(JSON, Host="evil.example"), b'{}')[0] == 403


def test_summarise_from_the_page(site, monkeypatch):
    import time

    from limitful import config, digest

    assert site("/api/digest", JSON, b"{}")[0] == 400  # nothing chosen yet

    class Canned:
        name = "canned"

        def summarize(self, transcript):
            return {"title": "A new title", "summary": ["Point."], "reminders": []}

    monkeypatch.setattr(digest, "make_summarizer", lambda provider, model=None, api_key=None: Canned())
    monkeypatch.setattr("limitful.store.MIN_DIGEST_WORDS", 1)
    config.update(provider="ollama")
    assert site("/api/digest", JSON, b"{}")[0] == 200
    for _ in range(100):
        job = json.loads(site("/api/status")[2])
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["error"] is None
    titles = [t["title"] for t in json.loads(site("/api/conversations/2026-11-13")[2])]
    assert "A new title" in titles and "Shopping and a call" in titles


def test_pendant_status_and_light(site, monkeypatch):
    from limitful import client, config

    reads = {"n": 0}

    async def read_state(timeout=8.0):
        reads["n"] += 1
        if reads["n"] == 2:
            raise client.PendantError("the pendant is not in range")
        return {"battery": 80, "pages": 100, "free_pages": 9000, "total_pages": 9100,
                "minutes_waiting": 2.4, "hours_free": 3.6}

    sent = []

    async def set_brightness(percent, timeout=8.0):
        sent.append(percent)

    monkeypatch.setattr(client, "read_state", read_state)
    monkeypatch.setattr(client, "set_brightness", set_brightness)

    before = json.loads(site("/api/pendant")[2])
    assert (before["checked"], before["brightness"]) == (None, None)  # reading the page touches no radio
    assert reads["n"] == 0

    got = json.loads(site("/api/pendant", JSON, b'{"action": "check"}')[2])
    assert (got["battery"], got["minutes_waiting"], got["error"]) == (80, 2.4, None) and got["checked"]
    failed = json.loads(site("/api/pendant", JSON, b'{"action": "check"}')[2])
    assert failed["error"] == "the pendant is not in range" and "battery" not in failed

    after = json.loads(site("/api/pendant", JSON, b'{"action": "brightness", "value": 35}')[2])
    assert sent == [35] and after["brightness"] == 35 and config.load()["led_brightness"] == 35
    for bad in (b'{"action": "brightness", "value": 101}', b'{"action": "brightness", "value": "bright"}',
                b'{"action": "brightness"}', b'{"action": "explode"}', b'[]'):
        assert site("/api/pendant", JSON, bad)[0] == 400
    assert sent == [35]
    assert site("/api/pendant", {"Content-Type": "text/plain"}, b'{"action": "check"}')[0] == 415
    assert site("/api/pendant", dict(JSON, Host="evil.example"), b'{"action": "check"}')[0] == 403


def test_sync_from_the_page_reports_when_the_pendant_is_away(site, monkeypatch):
    import time

    from limitful import client

    async def nobody(timeout=5.0):
        return []

    monkeypatch.setattr(client, "scan", nobody)
    assert site("/api/pendant", JSON, b'{"action": "sync"}')[0] == 200
    for _ in range(100):
        job = json.loads(site("/api/status")[2])
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["error"].startswith("Sync stopped: the pendant is not in range")


def test_pause_pair_and_unpair_from_the_page(site, monkeypatch):
    import time

    from limitful import client

    before = json.loads(site("/api/pendant")[2])
    assert before["paused_since"] is None
    paused = json.loads(site("/api/pendant", JSON, b'{"action": "pause"}')[2])
    assert paused["paused_since"] and json.loads(site("/api/pendant")[2])["paused_since"] == paused["paused_since"]
    assert json.loads(site("/api/pendant", JSON, b'{"action": "pause"}')[2])["paused_since"] == paused["paused_since"]
    assert json.loads(site("/api/pendant", JSON, b'{"action": "resume"}')[2])["paused_since"] is None

    calls = []

    async def unpair(timeout=8.0):
        calls.append("unpair")

    async def pair(timeout=180.0, log=print):
        calls.append(("pair", timeout))
        return None if len(calls) == 2 else object()

    monkeypatch.setattr(client, "unpair", unpair)
    monkeypatch.setattr(client, "pair", pair)
    assert site("/api/pendant", JSON, b'{"action": "unpair"}')[0] == 200 and calls == ["unpair"]

    def finished():
        for _ in range(100):
            job = json.loads(site("/api/status")[2])
            if not job["running"]:
                return job
            time.sleep(0.05)

    assert site("/api/pendant", JSON, b'{"action": "pair"}')[0] == 200
    assert finished()["error"].startswith("Pairing stopped: the pendant never accepted")
    assert site("/api/pendant", JSON, b'{"action": "pair"}')[0] == 200
    job = finished()
    assert (job["error"], job["note"]) == (None, "Paired. The pendant accepted this computer.")

    async def gone(timeout=8.0):
        raise client.PendantError("the pendant is not in range")

    monkeypatch.setattr(client, "unpair", gone)
    status, _h, body = site("/api/pendant", JSON, b'{"action": "unpair"}')
    assert status == 400 and b"Could not unpair: the pendant is not in range" in body


def test_transcription_settings_keep_the_deepgram_key_private(site, monkeypatch):
    import os
    import stat

    from limitful import config, digest, hosted

    checked = []
    monkeypatch.setattr(hosted, "check_deepgram", lambda key, base_url=None: checked.append(key))
    monkeypatch.setattr(digest, "check_claude", lambda key=None, model=None: pytest.fail("summaries were not touched"))
    start = json.loads(site("/api/settings")[2])
    assert (start["transcriber"], start["has_deepgram_key"], start["deepgram_key_hint"]) == ("local", False, None)

    # Deepgram cannot be chosen without a key, and nothing is sent to check one that is missing.
    status, _h, body = site("/api/settings", JSON, b'{"transcriber": "deepgram"}')
    assert status == 400 and b"needs an API key" in body and checked == [] and config.load() == {}

    status, _h, body = site("/api/settings", JSON, b'{"transcriber": "deepgram", "deepgram_api_key": " dg-SECRETSECRET9876 "}')
    assert status == 200 and b"SECRETSECRET" not in body
    got = json.loads(body)
    assert (got["transcriber"], got["has_deepgram_key"], got["deepgram_key_hint"]) == ("deepgram", True, "ends in 9876")
    assert got["provider"] is None  # the summaries choice is untouched
    assert checked == ["dg-SECRETSECRET9876"] and config.transcriber() == "deepgram"
    assert b"SECRETSECRET" not in site("/api/settings")[2]
    assert sys.platform == "win32" or stat.S_IMODE(os.stat(config.config_path()).st_mode) == 0o600  # no POSIX modes on Windows

    # Going back to this computer keeps the key; removing the key is explicit.
    assert json.loads(site("/api/settings", JSON, b'{"transcriber": "local"}')[2])["has_deepgram_key"] is True
    after = json.loads(site("/api/settings", JSON, b'{"transcriber": "local", "deepgram_api_key": ""}')[2])
    assert (after["transcriber"], after["has_deepgram_key"]) == ("local", False)
    assert site("/api/settings", JSON, b'{"transcriber": "deepgram", "deepgram_api_key": ""}')[0] == 400
    assert site("/api/settings", JSON, b'{"transcriber": "elsewhere"}')[0] == 400
    assert site("/api/settings", JSON, b'{"deepgram_api_key": 7}')[0] == 400


def test_transcription_settings_reject_a_key_deepgram_refuses(site, monkeypatch):
    from limitful import config, hosted

    def refuse(key, base_url=None):
        raise hosted.HostedError("Deepgram did not accept that key")

    monkeypatch.setattr(hosted, "check_deepgram", refuse)
    status, _h, body = site("/api/settings", JSON, b'{"transcriber": "deepgram", "deepgram_api_key": "dg-wrong-0000000"}')
    assert status == 400 and b"Nothing was saved" in body and config.load() == {}


def test_hosted_lines_show_numbered_speakers_without_the_import_lead(site, tmp_path):
    store = Store(tmp_path)
    store.db.execute("UPDATE utterances SET speaker_id = NULL, speaker_group = 'deepgram:1', source_speaker = '2' "
                     "WHERE text LIKE 'call%'")
    store.db.commit()
    store.close()
    day = datetime.fromtimestamp(T0 / 1000).strftime("%Y-%m-%d")
    reply = json.loads(site(f"/api/day/{day}")[2])
    found = [line for line in json.dumps(reply).split('"text"') if "call Joseph" in line]
    assert found and '"Speaker 2"' in json.dumps(reply)
    # Its audio offset is its own start (1.2 s in), not moved earlier as for imported lines.
    assert '"offset": 1200' in json.dumps(reply)


def test_an_impossible_date_is_a_bad_request_not_a_failure(site):
    status, _, body = site("/api/day/2026-13-45")
    assert status == 400 and b"cannot be answered" in body


def test_forget_over_usb(site, monkeypatch):
    from limitful import usb

    monkeypatch.setattr(usb, "forget", lambda: True)
    assert json.loads(site("/api/pendant", JSON, b'{"action": "forget"}')[2])["forgotten"] is True

    monkeypatch.setattr(usb, "forget", lambda: False)
    status, _, body = site("/api/pendant", JSON, b'{"action": "forget"}')
    assert status == 400 and b"No pendant is plugged in" in body

    def silent():
        raise usb.UsbError("the pendant did not confirm that it has forgotten its old computer")
    monkeypatch.setattr(usb, "forget", silent)
    status, _, body = site("/api/pendant", JSON, b'{"action": "forget"}')
    assert status == 400 and b"did not confirm" in body


def test_usb_readings_reach_the_page_and_correct_the_light(site, monkeypatch):
    from limitful import config, usb

    monkeypatch.setattr(usb, "read", lambda: None)
    assert json.loads(site("/api/pendant", JSON, b'{"action": "usb"}')[2])["usb"] is None

    monkeypatch.setattr(usb, "read", lambda: {"firmware": "1.1.20", "brightness": 40, "celsius": 31})
    got = json.loads(site("/api/pendant", JSON, b'{"action": "usb"}')[2])
    assert got["usb"]["firmware"] == "1.1.20" and got["brightness"] == 40
    assert config.load()["led_brightness"] == 40

    def broken():
        raise usb.UsbError("could not open /dev/x: Resource busy")
    monkeypatch.setattr(usb, "read", broken)
    assert json.loads(site("/api/pendant", JSON, b'{"action": "usb"}')[2])["usb"] == {
        "error": "could not open /dev/x: Resource busy"}


def test_day_recap_is_offered_written_and_served(site, tmp_path, monkeypatch):
    from limitful import config, digest
    from limitful.store import Store

    day = "2026-11-12"
    base = int(datetime(2026, 11, 12, 9, 0).timestamp() * 1000)
    store = Store(tmp_path)
    for n in range(2):
        store.db.execute(
            "INSERT INTO conversations (start_ms, end_ms, status, title, summary) VALUES (?, ?, 'done', ?, '[\"A point.\"]')",
            (base + n * 3600000, base + n * 3600000 + 600000, f"Talk {n + 1}"))
    store.db.commit()
    store.close()

    assert json.loads(site(f"/api/recap/{day}")[2]) == {"recap": None, "can_write": False}  # nothing chosen to write it
    assert site(f"/api/recap/{day}", JSON, b"{}")[0] == 400
    assert site("/api/recap/yesterday")[0] == 400

    class Engine:
        name = "fake"

        def recap(self, text):
            assert "Thursday, November 12, 2026" in text and "09:00, 10 min: Talk 1\n- A point." in text
            return {"headline": "Two talks", "points": ["One.", "Two."]}

    config.update(provider="ollama")
    monkeypatch.setattr(digest, "make_summarizer", lambda *a: Engine())
    assert json.loads(site(f"/api/recap/{day}")[2])["can_write"] is True
    written = json.loads(site(f"/api/recap/{day}", JSON, b"{}")[2])["recap"]
    assert (written["headline"], written["points"], written["stale"]) == ("Two talks", ["One.", "Two."], False)
    assert json.loads(site(f"/api/recap/{day}")[2])["recap"]["headline"] == "Two talks"
    assert site("/api/recap/2026-11-01", JSON, b"{}")[0] == 400  # an empty day


def test_first_run_state_demo_load_and_remove(site, tmp_path, monkeypatch):
    import sqlite3

    from limitful import speech_model
    from limitful.web import Library

    monkeypatch.setattr(speech_model, "ready", lambda: False)
    # The fixture's folder already has lines in it: not a first run, and no demo on top.
    assert json.loads(site("/api/welcome")[2]) == {"empty": False, "demo": False, "model_ready": False, "provider": None}
    assert site("/api/demo", JSON, b'{"action": "load"}')[0] == 400
    assert site("/api/demo", JSON, b'{"action": "explode"}')[0] == 400

    fresh = tmp_path / "fresh"
    Store(fresh).close()
    library = Library(fresh)
    assert library.welcome()["empty"] is True
    loaded = library.demo("load")
    assert (loaded["empty"], loaded["demo"]) == (False, True) and len(library.days()) == 3

    # A real line among the made-up ones survives their removal.
    db = sqlite3.connect(fresh / "pendant.db")
    db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES ('', 1, 2, 'a real line')")
    db.commit()
    db.close()
    gone = library.demo("remove")
    assert (gone["empty"], gone["demo"]) == (False, False)
    db = sqlite3.connect(fresh / "pendant.db")
    assert db.execute("SELECT text FROM utterances").fetchall() == [("a real line",)]
    for table in ("conversations", "sections", "reminders", "day_recaps", "speakers", "chats", "chat_messages"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,), table
    db.close()


def test_pendant_failures_come_with_plain_advice(site, monkeypatch):
    from limitful import client
    from limitful.web import pendant_advice

    assert "Privacy & Security" in pendant_advice("Bluetooth is not authorized for an unknown reason. Check macOS privacy settings.")
    assert "Switch it on" in pendant_advice("Bluetooth device is turned off")
    assert "Bluetooth on your phone" in pendant_advice(
        "Sync stopped: the pendant is not in range, is switched off, or is connected to another device")
    assert "not been paired" in pendant_advice("the pendant refused the connection because this computer is not paired with it")
    assert "old pairing" in pendant_advice("the pendant kept dropping the connection. Check it is nearby")
    assert pendant_advice("") is None and pendant_advice(None) is None and pendant_advice("disk full") is None

    async def away(*args, **kwargs):
        raise client.PendantError("the pendant is not in range, is switched off, or is connected to another device")
    monkeypatch.setattr(client, "read_state", away)
    checked = json.loads(site("/api/pendant", JSON, b'{"action": "check"}')[2])
    assert "Bluetooth on your phone" in checked["advice"]
    assert json.loads(site("/api/status")[2])["advice"] is None


def test_speech_model_download_is_reported_while_it_loads(tmp_path, monkeypatch):
    import time as clock

    from limitful import config, speech_model, transcribe
    from limitful.web import Library

    library = Library(tmp_path)
    seen = []

    def slow_load(engine):
        clock.sleep(1.3)                      # long enough for one progress reading
        seen.append(dict(library.job))
        return "the transcriber"

    monkeypatch.setattr(config, "transcriber", lambda: "local")
    monkeypatch.setattr(speech_model, "ready", lambda: False)
    monkeypatch.setattr(speech_model, "downloaded_bytes", lambda: 300 * 1024 * 1024)
    monkeypatch.setattr(transcribe, "make_transcriber", slow_load)
    assert library._transcriber() == "the transcriber"
    assert seen[0]["what"].startswith("Downloading the speech model") and (seen[0]["done"], seen[0]["total"]) == (300, 1546)
    assert (library.job["what"], library.job["total"]) == ("Transcribing", 0)

    # Already on this computer: nothing is announced.
    monkeypatch.setattr(speech_model, "ready", lambda: True)
    library.job.update(what="Syncing the pendant")
    library._transcriber()
    assert library.job["what"] == "Syncing the pendant"


def test_pairing_clears_an_earlier_not_paired_failure(site, monkeypatch):
    import time

    from limitful import client

    async def refused(timeout=8.0):
        raise client.PendantError(client._NOT_PAIRED)

    async def pair(timeout=180.0, log=print):
        return object()

    monkeypatch.setattr(client, "read_state", refused)
    monkeypatch.setattr(client, "pair", pair)
    assert "not paired" in json.loads(site("/api/pendant", JSON, b'{"action": "check"}')[2])["error"]
    assert site("/api/pendant", JSON, b'{"action": "pair"}')[0] == 200
    for _ in range(100):
        job = json.loads(site("/api/status")[2])
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["note"].startswith("Paired")
    after = json.loads(site("/api/pendant")[2])
    assert (after["error"], after["checked"], after["advice"]) == (None, None, None)


def test_days_carry_what_the_calendar_shows(site, tmp_path):
    store = Store(tmp_path)
    store.db.execute("INSERT INTO day_recaps (day, headline, points, engine, signature, made_ms) "
                     "VALUES ('2026-11-13', 'Shopping, then a call', '[]', 'x', 's', 1)")
    store.db.commit()
    store.close()
    (only,) = json.loads(site("/api/days")[2])
    assert (only["talks"], only["headline"]) == (1, "Shopping, then a call")


def test_search_hits_come_with_their_surroundings(site, tmp_path):
    store = Store(tmp_path)
    base = T0 + 7_200_000
    for n, text in enumerate(["We could go on Friday.", "Friday works, the ferry leaves at nine.", "Then nine it is."]):
        store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES ('', ?, ?, ?)",
                         (base + n * 4000, base + n * 4000 + 3000, text))
    store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES ('', ?, ?, 'A ferry, hours later.')",
                     (base + 3_600_000, base + 3_601_000))
    store.db.execute("INSERT INTO conversations (start_ms, end_ms, status, title, summary) VALUES (?, ?, 'done', 'Trip plans', '[]')",
                     (base, base + 12000))
    store.db.commit()
    store.close()
    later, hit = json.loads(site("/api/search?q=ferry")[2])   # newest first
    assert (hit["before"], hit["after"], hit["talk"]) == ("We could go on Friday.", "Then nine it is.", "Trip plans")
    assert (later["before"], later["after"], later["talk"]) == (None, None, None)  # nothing near it, no conversation


def _spoken_file(folder, name="20261114 093012-1A2B3C4D.m4a", seconds=3):
    """A short AAC file of a tone, as a phone's recorder would write it."""
    import av
    import numpy as np

    path = folder / name
    with av.open(str(path), "w", format="ipod") as out:
        stream = out.add_stream("aac", rate=44100)
        stream.layout = "mono"
        tone = (np.sin(np.arange(44100 * seconds) * 2 * np.pi * 220 / 44100) * 0.3).astype(np.float32)
        frame = av.AudioFrame.from_ndarray(tone.reshape(1, -1), format="fltp", layout="mono")
        frame.sample_rate = 44100
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return path


AUDIO = {"Content-Type": "application/octet-stream"}


def test_add_a_recording_through_the_page(site, tmp_path, monkeypatch):
    from limitful import web

    data = _spoken_file(tmp_path).read_bytes()
    named = dict(AUDIO, **{"X-Name": "20261114%20093012-1A2B3C4D.m4a"})
    status, _h, body = site("/api/recordings", named, data)
    assert status == 200, body
    got = json.loads(body)
    assert got["added"] and got["day"] == "2026-11-14" and got["dated_by"] == "name"
    assert 2800 <= got["duration_ms"] <= 3200
    kept = list((tmp_path / "audio" / "2026-11-14").glob("added_09-30-12_*.ogg"))
    assert len(kept) == 1 and kept[0].read_bytes()[:4] == b"OggS"
    assert not list((tmp_path / "imports").iterdir())  # the upload is not kept
    assert site(f"/audio/2026-11-14/{kept[0].name}")[0] == 200

    # The same file again adds nothing.
    assert json.loads(site("/api/recordings", named, data)[2])["added"] is False

    # Transcribing needs no pendant.
    monkeypatch.setattr(web.Library, "_transcriber", lambda self: Words())
    assert json.loads(site("/api/transcribe", JSON, b"{}")[2]) == {"waiting": 1}
    for _ in range(200):
        job = json.loads(site("/api/status")[2])
        if not job["running"]:
            break
        time.sleep(0.02)
    assert job["error"] is None and job["note"] == "Transcribed 1 recording: 2 lines of speech."
    store = Store(tmp_path)
    try:
        rows = store.db.execute(
            "SELECT text, start_ms FROM utterances WHERE segment LIKE '%added_%' ORDER BY start_ms").fetchall()
    finally:
        store.close()
    assert [r[0] for r in rows] == ["buy 100% oat_milk", "call Joseph"]
    assert datetime.fromtimestamp(rows[0][1] / 1000).strftime("%Y-%m-%d %H:%M:%S") == "2026-11-14 09:30:12"
    assert json.loads(site("/api/transcribe", JSON, b"{}")[2]) == {"waiting": 0}


def test_add_a_recording_rejects_bad_uploads(site):
    named = dict(AUDIO, **{"X-Name": "memo.m4a"})
    assert site("/api/recordings", named, b"this is not audio")[0] == 400
    assert site("/api/recordings", named, b"")[0] == 400
    assert site("/api/recordings", dict(AUDIO, **{"X-Name": "notes.txt"}), b"x")[0] == 400
    assert site("/api/recordings", dict(AUDIO, **{"X-Name": "..%2F..%2Fescape.m4a"}), b"x")[0] == 400
    assert site("/api/recordings", JSON, b"{}")[0] == 415
    assert site("/api/recordings", {"Content-Type": "audio/mp4"}, b"x")[0] == 415
    assert site("/api/recordings", dict(named, Host="evil.example"), b"x")[0] == 403
    assert site("/api/import", AUDIO, b"x")[0] == 415


def test_voice_memos_through_the_page(site, tmp_path, monkeypatch):
    from limitful import web
    from test_voicememos import MEMOS, library

    folder = library(tmp_path, MEMOS)
    monkeypatch.setenv("LIMITFUL_VOICE_MEMOS", str(folder))
    monkeypatch.setattr(web.Library, "_transcriber", lambda self: Words())

    got = json.loads(site("/api/recordings")[2])
    assert got["recordings"] == [] and got["voice_memos"]["state"] == "ok"
    assert (got["voice_memos"]["memos"], got["voice_memos"]["enabled"]) == (2, False)

    def finished():
        for _ in range(300):
            job = json.loads(site("/api/status")[2])
            if not job["running"]:
                return job
            time.sleep(0.02)
        raise AssertionError("still running")

    # Off: a check adds nothing.
    assert site("/api/voicememos/sync", JSON, b"{}")[0] == 200
    assert finished()["note"] == "No new Voice Memos."

    # On: only memos recorded from now on, so still nothing.
    on = json.loads(site("/api/voicememos", JSON, b'{"enabled": true}')[2])["voice_memos"]
    assert on["enabled"] and on["since"] > 0
    site("/api/voicememos/sync", JSON, b"{}")
    assert finished()["note"] == "No new Voice Memos."

    # Older ones too: both arrive and are transcribed.
    assert json.loads(site("/api/voicememos", JSON, b'{"enabled": true, "older": true}')[2])["voice_memos"]["since"] == 0
    site("/api/voicememos/sync", JSON, b"{}")
    job = finished()
    assert job["error"] is None and job["note"] == "Added 2 Voice Memos: 4 lines of speech."
    listed = json.loads(site("/api/recordings")[2])["recordings"]
    assert [(r["name"], r["lines"], r["transcribed"]) for r in listed] == [
        ("Dr. Ruiz, follow-up", 2, True), ("Idea for the newsletter", 2, True)]

    # Give one the right time, then remove it.
    target = int(datetime(2026, 11, 13, 7, 0, 0).timestamp() * 1000)
    body = json.dumps({"path": listed[0]["path"], "start_ms": target}).encode()
    moved = json.loads(site("/api/recordings/move", JSON, body)[2])["recordings"]
    assert sorted(r["start_ms"] for r in moved)[1] == target
    path = next(r["path"] for r in moved if r["start_ms"] == target)
    left = json.loads(site("/api/recordings/remove", JSON, json.dumps({"path": path}).encode())[2])["recordings"]
    assert [r["name"] for r in left] == ["Idea for the newsletter"]

    assert site("/api/recordings/remove", JSON, b'{"path": "audio/2026-11-08/nope.ogg"}')[0] == 400
    assert site("/api/recordings/move", JSON, json.dumps({"path": left[0]["path"], "start_ms": "soon"}).encode())[0] == 400
    assert site("/api/voicememos", dict(JSON, Host="evil.example"), b'{"enabled": true}')[0] == 403


def test_a_suggested_name_can_be_turned_down(site):
    assert json.loads(site("/api/names/suggested")[2]) == []
    assert site("/api/speakers/1/not", JSON, b'{"name": "Joseph"}')[0] == 200
    assert site("/api/speakers/1/not", JSON, b'{"name": ""}')[0] == 400
    assert site("/api/speakers/99/not", JSON, b'{"name": "Joseph"}')[0] == 400
    assert site("/api/speakers/1", JSON, b'{"name": "Andrei"}')[0] == 200  # the plain rename still works
