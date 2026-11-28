"""Naming voices from clues in what was said."""

import json
from datetime import datetime

import pytest

from limitful import naming, triage
from limitful.store import Store

T0 = int(datetime(2026, 11, 20, 9, 0).timestamp() * 1000)
HOUR = 3_600_000


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    for sid in (1, 2, 3):
        s.db.execute("INSERT INTO speakers (id, name, voice_sum, lines) VALUES (?, ?, x'', 0)", (sid, f"Speaker {sid}"))
    s.db.commit()
    yield s
    s.close()


def talk(store, cid, lines):
    """A waiting conversation; lines are (speaker id, text). Returns nothing: line n is lines[n-1]."""
    start = T0 + cid * HOUR
    for n, (sid, text) in enumerate(lines):
        store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text, speaker_id) VALUES ('x', ?, ?, ?, ?)",
                         (start + n * 3000, start + n * 3000 + 2500, text, sid))
    store.db.execute("INSERT INTO conversations (id, start_ms, end_ms, status) VALUES (?, ?, ?, 'pending')",
                     (cid, start, start + len(lines) * 3000))
    store.db.commit()


def save(store, cid, names):
    return triage.save(store, cid, title="A talk", summary=[], notes=[], reminders=[], tier="text",
                       tags=["small-talk"], sensitive=False, names=names)


def name_of(store, sid):
    return store.db.execute("SELECT name, named_by FROM speakers WHERE id = ?", (sid,)).fetchone()


def test_three_agreeing_conversations_name_a_voice(store):
    for cid in (1, 2):
        talk(store, cid, [(1, "hi, I'm Joseph"), (2, "nice to meet you")])
        assert save(store, cid, [{"line": 1, "name": "Joseph", "clue": "introduction"}])["voices_named"] == []
    assert name_of(store, 1) == ("Speaker 1", None)
    [guess] = naming.suggestions(store)
    assert (guess["speaker"], guess["name"], guess["introductions"], guess["label"]) == (1, "Joseph", 2, "Speaker 1")

    # The third clue is an address: "thanks, Joseph" from voice 2, answered by voice 1.
    talk(store, 3, [(2, "thanks, Joseph"), (2, "really"), (1, "any time")])
    saved = save(store, 3, [{"line": 1, "name": "joseph.", "clue": "addressed"}])
    assert saved["voices_named"] == ["Joseph"] and saved["name_clues"] == 1
    assert name_of(store, 1) == ("Joseph", "clues")
    assert naming.suggestions(store) == []
    assert [p["from_clues"] for p in store.people()] == [True]


def test_disagreement_or_a_third_person_mention_keeps_it_a_suggestion(store):
    for cid in (1, 2, 3):
        talk(store, cid, [(1, "I'm Joseph"), (2, "hello")])
        save(store, cid, [{"line": 1, "name": "Joseph", "clue": "introduction"}])
    # Before the third save could apply it, another conversation says voice 1 talks about Joseph.
    store.db.execute("UPDATE speakers SET name = 'Speaker 1', named_by = NULL WHERE id = 1")
    talk(store, 4, [(1, "Joseph is running late"), (2, "ok")])
    save(store, 4, [{"line": 1, "name": "Joseph", "clue": "mentioned"}])
    assert name_of(store, 1)[0] == "Speaker 1"
    [guess] = naming.suggestions(store)
    assert guess["against"] == 1 and len(guess["conversations"]) == 3


def test_a_mention_does_not_count_against_someone_named_in_the_same_talk(store):
    talk(store, 1, [(1, "I'm Priya"), (2, "Priya said she'd do it"), (1, "I did")])
    save(store, 1, [{"line": 1, "name": "Priya", "clue": "introduction"},
                    {"line": 2, "name": "Priya", "clue": "mentioned"}])
    votes = naming.tally(store)
    assert votes[1]["priya"].against == set() and votes[2]["priya"].against == {1}


def test_names_the_owner_typed_are_never_changed_and_not_them_sticks(store):
    store.rename_speaker(1, "Dad")
    for cid in (1, 2, 3):
        talk(store, cid, [(1, "I'm Joseph"), (2, "I'm Sam")])
        save(store, cid, [{"line": 1, "name": "Joseph", "clue": "introduction"},
                          {"line": 2, "name": "Sam", "clue": "introduction"}])
    assert name_of(store, 1) == ("Dad", None)
    assert name_of(store, 2) == ("Sam", "clues")
    store.rename_speaker(2, "Samuel")  # the owner corrects it: now theirs
    assert name_of(store, 2) == ("Samuel", None)

    talk(store, 4, [(3, "I'm Alex"), (1, "hey")])
    save(store, 4, [{"line": 1, "name": "Alex", "clue": "introduction"}])
    assert [g["name"] for g in naming.suggestions(store)] == ["Alex"]
    naming.reject(store, 3, "Alex")
    assert naming.suggestions(store) == []


def test_bad_clues_are_dropped(store):
    talk(store, 1, [(1, "hello"), (2, "hi")])
    saved = save(store, 1, [{"line": 9, "name": "Joseph", "clue": "introduction"},
                            {"line": 1, "name": "Speaker 2", "clue": "introduction"},
                            {"line": 1, "name": "Joseph", "clue": "guess"},
                            {"line": 1, "name": "", "clue": "introduction"}, "nonsense"])
    assert saved["name_clues"] == 0


def test_forgetting_a_conversation_forgets_its_clues(store):
    from limitful.forget import forget

    talk(store, 1, [(1, "I'm Joseph"), (2, "hi")])
    save(store, 1, [{"line": 1, "name": "Joseph", "clue": "introduction"}])
    forget(store, T0 + HOUR, T0 + HOUR + 6000)
    assert store.db.execute("SELECT COUNT(*) FROM name_votes").fetchone()[0] == 0


def test_the_web_app_suggests_and_takes_a_no(tmp_path, store):
    from limitful.web import Library

    talk(store, 1, [(1, "I'm Joseph"), (2, "hi")])
    save(store, 1, [{"line": 1, "name": "Joseph", "clue": "introduction"}])
    library = Library(tmp_path)
    [guess] = library.name_suggestions()
    assert json.loads(json.dumps(guess))["example"]["id"] == 1
    library.reject_name(1, "Joseph")
    assert library.name_suggestions() == []
