"""Conversations, summaries and reminders, with stand-ins for the language model."""

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from limitful import digest
from limitful.store import Store

T0 = int(datetime(2026, 11, 13, 9, 0, 0).timestamp() * 1000)
MIN = 60_000
LONG = ("we should finish the quarterly report by Thursday and keep it under ten pages "
        "with the detailed tables moved into an appendix for the client to read later")


class Canned:
    name = "canned"

    def __init__(self, fail_on=None):
        self.seen = []
        self.fail_on = fail_on

    def summarize(self, transcript):
        self.seen.append(transcript)
        if self.fail_on and self.fail_on in transcript:
            raise self.fail_on_error
        return {"title": f"Talk {len(self.seen)}", "summary": ["One.", "Two."],
                "reminders": ["Send the template"] if "report" in transcript else []}


def say(store, minute, text, speaker=None):
    start = T0 + minute * MIN
    store.db.execute(
        "INSERT INTO utterances (segment, start_ms, end_ms, text, speaker_id) VALUES ('x', ?, ?, ?, ?)",
        (start, start + 5000, text, speaker))
    store.db.commit()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    yield s
    s.close()


def test_conversations_split_at_long_silences_and_wait_for_the_last(store):
    say(store, 0, "a"); say(store, 2, "b"); say(store, 10, "c"); say(store, 30, "d")
    # Three minutes after the last line, it may still be going on.
    assert store.build_conversations(now_ms=T0 + 33 * MIN) == 2
    assert store.build_conversations(now_ms=T0 + 33 * MIN) == 0
    assert store.build_conversations(now_ms=T0 + 40 * MIN) == 1
    spans = store.db.execute("SELECT start_ms, end_ms FROM conversations ORDER BY start_ms").fetchall()
    assert [(a - T0) // MIN for a, _b in spans] == [0, 10, 30]
    assert spans[0][1] == T0 + 2 * MIN + 5000


def test_digest_writes_titles_summaries_and_reminders(store, tmp_path):
    store.db.execute("INSERT INTO speakers (id, name, voice_sum, lines) VALUES (1, 'Karen', x'00', 1)")
    say(store, 0, LONG, speaker=1)
    say(store, 1, "sounds good to me")
    say(store, 20, "okay")  # too little to summarise
    canned = Canned()
    assert store.digest_pending(canned) == 1
    assert canned.seen == [f"1. [09:00] Karen: {LONG}\n2. [09:01] Unknown: sounds good to me"]
    assert store.db.execute("SELECT status, title FROM conversations ORDER BY start_ms").fetchall() == [
        ("done", "Talk 1"), ("short", None)]

    (talk,) = store.conversations(T0, T0 + 24 * 60 * MIN)
    assert (talk["title"], talk["summary"]) == ("Talk 1", ["One.", "Two."])
    assert [(r["text"], r["status"]) for r in talk["reminders"]] == [("Send the template", "suggested")]

    text = (tmp_path / "transcripts" / "2026-11-13.md").read_text()
    assert "## 09:00 Talk 1\n\n> One.\n> Two.\n\n- **09:00:00** Karen: we should finish" in text
    assert "- **09:20:00** okay" in text
    assert store.digest_pending(canned) == 0 and len(canned.seen) == 1  # nothing left to do


def test_reminder_states(store):
    say(store, 0, LONG)
    store.digest_pending(Canned())
    (reminder,) = store.reminders()
    assert (reminder["status"], reminder["from"]) == ("suggested", "Talk 1")
    store.set_reminder_status(reminder["id"], "accepted")
    assert store.reminders()[0]["status"] == "accepted"
    store.set_reminder_status(reminder["id"], "done")
    assert store.reminders() == []  # finished ones drop off the list
    with pytest.raises(ValueError):
        store.set_reminder_status(reminder["id"], "maybe")
    with pytest.raises(ValueError):
        store.set_reminder_status(999, "done")


def test_engine_failure_keeps_finished_work_and_retries_the_rest(store):
    say(store, 0, LONG)
    say(store, 30, LONG + " second")
    flaky = Canned(fail_on="second")
    flaky.fail_on_error = digest.DigestError("offline")
    with pytest.raises(digest.DigestError):
        store.digest_pending(flaky)
    assert [s for (s,) in store.db.execute("SELECT status FROM conversations ORDER BY start_ms")] == [
        "done", "pending"]
    assert store.digest_pending(Canned()) == 1  # the second run only does what is left


def test_declined_conversation_is_not_retried(store):
    say(store, 0, LONG)
    refusing = Canned(fail_on="report")
    refusing.fail_on_error = digest.DigestDeclined("no")
    assert store.digest_pending(refusing) == 0
    assert store.db.execute("SELECT status FROM conversations").fetchone() == ("skipped",)
    assert store.digest_pending(Canned()) == 0


def test_redo_discards_and_rebuilds(store):
    say(store, 0, LONG)
    store.digest_pending(Canned())
    store.forget_digests()
    assert store.reminders() == [] and store.conversations(T0, T0 + MIN) == []
    assert store.digest_pending(Canned()) == 1


def test_redo_leaves_imported_conversations_alone(store):
    store.db.execute("INSERT INTO conversations (id, start_ms, end_ms, status, title, engine) "
                     "VALUES (50, 1, 2, 'done', 'Imported', 'limitless')")
    store.db.execute("INSERT INTO reminders (conversation_id, text, created_ms) VALUES (50, 'Keep me', 1)")
    store.db.execute("INSERT INTO sections (conversation_id, start_ms, level, title) VALUES (50, 1, 2, 'S')")
    say(store, 0, LONG)
    store.digest_pending(Canned())
    store.forget_digests()
    assert store.db.execute("SELECT title FROM conversations").fetchall() == [("Imported",)]
    assert [r["text"] for r in store.reminders()] == ["Keep me"]
    assert store.db.execute("SELECT COUNT(*) FROM sections").fetchone() == (1,)


def test_clean_accepts_sloppy_replies_and_rejects_useless_ones():
    got = digest.clean({"title": "  Budget\n review ", "summary": "One point.",
                        "reminders": ["  Call  Joseph ", "", 7, "x"] + ["more"] * 20})
    assert (got["title"], got["summary"]) == ("Budget review", ["One point."])
    assert [r["text"] for r in got["reminders"]] == ["Call Joseph", "x"] + ["more"] * 8
    assert digest.clean({"title": "T"}) == {"title": "T", "summary": [], "reminders": []}
    for bad in ({"title": "   "}, {"summary": []}, ["title"], "title", {"title": 5}):
        with pytest.raises(digest.DigestError):
            digest.clean(bad)


@pytest.fixture
def ollama():
    """A stand-in Ollama server that records the request and replies as configured."""
    state = {"reply": {"message": {"content": json.dumps(
        {"title": "Local title", "summary": ["S"], "reminders": []})}}, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            body = json.dumps({"models": [{"name": "tiny:latest"}, {"name": "other:7b"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            state["path"] = self.path
            state["request"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps(state["reply"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["host"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()


def test_ollama_request_and_reply(ollama):
    engine = digest.OllamaSummarizer("tiny", ollama["host"])
    assert engine.summarize("[09:00] A: hello") == {"title": "Local title", "summary": ["S"], "reminders": []}
    request = ollama["request"]
    assert ollama["path"] == "/api/chat"
    assert (request["model"], request["stream"], request["format"]) == ("tiny", False, "json")
    assert request["messages"][0] == {"role": "system", "content": digest.SYSTEM}
    assert request["messages"][1] == {"role": "user", "content": "[09:00] A: hello"}


def test_ollama_failures_are_explained(ollama):
    engine = digest.OllamaSummarizer("tiny", ollama["host"])
    ollama["reply"] = {"message": {"content": "not json"}}
    with pytest.raises(digest.DigestError, match="not valid JSON"):
        engine.summarize("x")
    ollama["status"], ollama["reply"] = 404, {"error": "model not found"}
    with pytest.raises(digest.DigestError, match="ollama pull tiny"):
        engine.summarize("x")
    with pytest.raises(digest.DigestError, match="ollama serve"):
        digest.OllamaSummarizer("tiny", "http://127.0.0.1:9").summarize("x")


def test_checking_ollama_before_it_is_chosen(ollama):
    digest.check_ollama("tiny", ollama["host"])
    digest.check_ollama("other:7b", ollama["host"])
    with pytest.raises(digest.DigestError, match="ollama pull llama3.1"):
        digest.check_ollama("llama3.1", ollama["host"])
    with pytest.raises(digest.DigestError, match="ollama serve"):
        digest.check_ollama("tiny", "http://127.0.0.1:9")


def test_unknown_provider():
    with pytest.raises(ValueError):
        digest.make_summarizer("gpt")


def test_reminders_point_at_the_line_they_came_from(store):
    from limitful.store import best_matching_line

    say(store, 0, LONG)
    say(store, 1, "I will send everyone the template after lunch")
    say(store, 2, "okay sounds good")

    class Citing:
        name = "citing"

        def summarize(self, transcript):
            assert transcript.splitlines()[1].startswith("2. [09:01] Unknown: I will send")
            return {"title": "T", "summary": [], "reminders": [
                {"text": "Send the template", "line": 2},       # cited by the model
                {"text": "Keep the report under ten pages", "line": None},  # found by wording
                {"text": "Water the plants", "line": 99},        # a bad citation, and no match
            ]}

    store.digest_pending(Citing())
    by_text = {r["text"]: r for r in store.reminders()}
    assert by_text["Send the template"]["line"]["text"].startswith("I will send everyone")
    assert by_text["Keep the report under ten pages"]["line"]["text"] == LONG
    assert by_text["Water the plants"]["line"] is None
    first = by_text["Send the template"]
    assert first["day"] == "2026-11-13" and first["conversation"] and first["start"] == T0
    (talk,) = store.conversations(T0, T0 + MIN)
    assert [r["line"] is not None for r in talk["reminders"]] == [True, True, False]

    lines = [(1, "we talked about the weather"), (2, "they are spraying something in West Palm Beach"),
             (3, "the weather was nice")]
    assert best_matching_line("Ask West Palm Beach officials what they're spraying", lines) == 2
    assert best_matching_line("Check the weather", lines) is None  # one common word is not enough
    assert best_matching_line("Call the dentist", lines) is None
    assert best_matching_line("", lines) is None and best_matching_line("Call", []) is None


def test_clean_reads_reminders_as_objects_or_strings():
    got = digest.clean({"title": "T", "summary": [], "reminders": [
        {"text": " Send  it ", "line": 4}, "Bare string", {"text": "No line"}, {"text": "", "line": 1},
        {"text": "Bad line", "line": "three"}, {"line": 2}]})
    assert got["reminders"] == [
        {"text": "Send it", "line": 4}, {"text": "Bare string", "line": None},
        {"text": "No line", "line": None}, {"text": "Bad line", "line": None}]


LATER = "2099-01-01"  # a "today" by which every day in these tests is finished


class Recapper:
    """Recaps a day and records what it was shown."""

    name = "recapper"

    def __init__(self):
        self.days = []

    def recap(self, day):
        self.days.append(day)
        return {"headline": f"Recap {len(self.days)}", "points": ["First thread.", "Second thread."]}


def test_a_finished_day_gets_a_recap_written_from_its_conversations(tmp_path):
    store = Store(tmp_path)
    say(store, 0, LONG)
    say(store, 30, LONG + " and water the garden")
    assert store.digest_pending(Canned()) == 2
    day = datetime.fromtimestamp(T0 / 1000).strftime("%Y-%m-%d")
    engine = Recapper()
    assert store.recap_pending(engine, today=day) == 0      # the day is not over yet
    assert store.recap_pending(engine, today=LATER) == 1

    recap = store.day_recap(day)
    assert (recap["headline"], recap["points"], recap["stale"]) == ("Recap 1", ["First thread.", "Second thread."], False)
    (shown,) = engine.days
    assert "09:00, 1 min: Talk 1" in shown and "09:30, 1 min: Talk 2" in shown and "- One." in shown
    assert "quarterly report" not in shown  # titles and summaries only, never the transcript

    # Nothing changed, so nothing is written again; a retitled conversation makes it stale.
    assert store.recap_pending(engine, today=LATER) == 0 and len(engine.days) == 1
    store.db.execute("UPDATE conversations SET title = 'Renamed' WHERE id = 1")
    assert store.day_recap(day)["stale"] is True
    assert store.recap_pending(engine, today=LATER) == 1 and store.day_recap(day)["headline"] == "Recap 2"
    store.close()


def test_days_with_one_conversation_or_only_imported_are_left_alone(tmp_path):
    store = Store(tmp_path)
    say(store, 0, LONG)
    store.digest_pending(Canned())
    day = datetime.fromtimestamp(T0 / 1000).strftime("%Y-%m-%d")
    engine = Recapper()
    assert store.days_to_recap(today=LATER) == [] and store.recap_day(day, engine) is None and engine.days == []

    say(store, 30, LONG + " and water the garden")
    store.digest_pending(Canned())
    assert store.days_to_recap(today=LATER) == [day]
    store.db.execute("UPDATE conversations SET engine = 'limitless'")
    assert store.days_to_recap(today=LATER) == []                    # imported history is only sent when asked
    assert store.days_to_recap(everything=True, today=LATER) == [day]
    store.close()


def test_clean_day():
    from limitful.digest import DigestError, clean_day
    assert clean_day({"headline": "  A  day ", "points": ["one", "", 3, " two "] + ["x"] * 9}) == {
        "headline": "A day", "points": ["one", "two", "x", "x", "x"]}
    assert clean_day({"headline": "Only this", "points": "single"})["points"] == ["single"]
    for bad in ({}, {"headline": " "}, ["x"], {"headline": 3}):
        with pytest.raises(DigestError):
            clean_day(bad)


def test_notes_are_written_for_a_day_and_cite_their_lines(store):
    say(store, 0, LONG); say(store, 1, LONG)
    say(store, 600, "later")
    store.digest_pending(Canned())
    # An imported conversation can be done and still hold too little to note.
    store.db.execute("INSERT INTO conversations (start_ms, end_ms, status, title, summary) VALUES (?, ?, 'done', 'Hi', '[]')",
                     (T0 + 30 * MIN, T0 + 31 * MIN))
    say(store, 30, "hello")

    class Noter:
        name = "noter"
        seen = 0

        def notes(self, transcript):
            Noter.seen += 1
            return digest.clean_notes({"notes": [
                {"text": "  The report  stays under ten pages. ", "line": 2},
                {"text": "Tables go in an appendix.", "line": 99},
                {"text": "   ", "line": 1},
            ], "todos": [{"text": "Send the report by Thursday", "line": 1}]})

    day = int(datetime(2026, 11, 13).timestamp() * 1000)
    before = store.conversations(day, day + 24 * 60 * MIN)
    assert [talk["notes"] for talk in before] == [None, None]  # not written yet
    assert store.write_notes(Noter(), day, day + 24 * 60 * MIN) == 2 and Noter.seen == 1
    first, short = store.conversations(day, day + 24 * 60 * MIN)
    second_line = store.db.execute("SELECT id FROM utterances ORDER BY start_ms LIMIT 1 OFFSET 1").fetchone()[0]
    assert first["notes"] == [
        {"text": "The report stays under ten pages.", "line": second_line},
        {"text": "Tables go in an appendix.", "line": None},  # a line that is not there
    ]
    assert short["notes"] == []  # looked at, nothing to note
    # The first already had a reminder from its summary, so its todo is not suggested twice.
    assert [r["text"] for r in first["reminders"]] == ["Send the template"]
    assert (first["title"], first["summary"]) == (before[0]["title"], before[0]["summary"])
    # Written once: a second run leaves them alone unless asked to redo.
    assert store.write_notes(Noter(), day, day + 24 * 60 * MIN) == 0
    assert store.write_notes(Noter(), day, day + 24 * 60 * MIN, redo=True) == 2
    with pytest.raises(digest.DigestError):
        digest.clean_notes({"notes": "none"})
    # A conversation with no reminders gets its todos as suggestions, each with its line.
    store.db.execute("DELETE FROM reminders")
    store.write_notes(Noter(), day, day + 24 * 60 * MIN, redo=True)
    first = store.conversations(day, day + 24 * 60 * MIN)[0]
    first_line = store.db.execute("SELECT id FROM utterances ORDER BY start_ms LIMIT 1").fetchone()[0]
    assert [(r["text"], r["status"], r["line"]) for r in first["reminders"]] == [
        ("Send the report by Thursday", "suggested", first_line)]


def test_claude_summaries_use_the_chosen_model_and_ask_for_fallbacks_only_where_offered(monkeypatch):
    import sys
    import types

    from limitful import config
    from limitful.digest import make_summarizer

    sent = []

    class Messages:
        def create(self, **kwargs):
            sent.append(kwargs)
            block = types.SimpleNamespace(type="text", text='{"title": "Report", "summary": [], "reminders": []}')
            return types.SimpleNamespace(stop_reason="end_turn", content=[block])

    class Anthropic:
        def __init__(self, api_key=None):
            self.beta = types.SimpleNamespace(messages=Messages())

    fake = types.ModuleType("anthropic")
    fake.Anthropic = Anthropic
    for name in ("AuthenticationError", "RateLimitError", "APIStatusError", "APIConnectionError", "AnthropicError"):
        setattr(fake, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    config.update(claude_model="claude-haiku-5-5")
    haiku = make_summarizer("claude")
    assert haiku.name == "claude:claude-haiku-5-5"
    haiku.summarize("1. [09:00] Unknown: the report is due Thursday")
    assert sent[-1]["model"] == "claude-haiku-5-5" and "fallbacks" not in sent[-1] and "betas" not in sent[-1]

    make_summarizer("claude", "claude-sonnet-5-5").summarize("1. [09:00] Unknown: hello")
    assert sent[-1]["model"] == "claude-sonnet-5-5" and sent[-1]["fallbacks"] == "default"
