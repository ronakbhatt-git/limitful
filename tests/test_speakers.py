"""Speaker grouping, with a stand-in for the voice model."""

from datetime import datetime

import numpy as np
import pytest

from limitful.protocol import FlashPage
from limitful.store import Store
from limitful.transcribe import Utterance

from wire import flash_page, opus_frame

T0 = int(datetime(2026, 11, 13, 9, 0, 0).timestamp() * 1000)
WHO = [0, 1, 0, 2, 1, 0, 2, 1, 0, 1, 2, 0]  # who says each 2-second line
RATE = 16000


class Words:
    name = "fake"

    def __init__(self, count):
        self.count = count

    def transcribe(self, audio_path):
        said = [Utterance(i * 2000, i * 2000 + 1900, f"line {i}") for i in range(self.count)]
        return said + [Utterance(self.count * 2000, self.count * 2000 + 500, "mm")]


def fake_audio(path):
    """Samples whose value says who is speaking at that moment."""
    audio = np.zeros(40 * RATE, dtype=np.float32)
    for i, who in enumerate(WHO):
        audio[i * 2 * RATE : (i + 1) * 2 * RATE] = who
    return audio


class Voices:
    """Every voice shares a large common part (the microphone) plus its own."""

    def embed(self, clip):
        who = int(clip[0])
        rng = np.random.default_rng(len(clip) + who)
        voice = np.full(16, 5.0)
        voice[who] += 1.5
        return (voice + rng.normal(0, 0.05, 16)).astype(np.float32)


def make_store(tmp_path, lines=len(WHO)):
    store = Store(tmp_path)
    for index in range(30):
        store.add_page(FlashPage(index=index, session=1, seq=0,
                                 data=flash_page(T0 + index * 1000, [opus_frame(i) for i in range(50)])))
    store.commit()
    store.export_audio()
    store.transcribe_pending(Words(lines))
    return store


def groups(store):
    rows = store.db.execute("SELECT text, speaker_id FROM utterances ORDER BY start_ms").fetchall()
    return [speaker for _text, speaker in rows]


def test_lines_are_grouped_by_voice(tmp_path):
    store = make_store(tmp_path)
    assert store.label_speakers(Voices(), fake_audio) == 12
    got = groups(store)
    assert got[-1] is None  # the half-second "mm" is too short to judge
    first_seen = {}
    for who, speaker in zip(WHO, got):
        assert first_seen.setdefault(who, speaker) == speaker
    assert len(set(first_seen.values())) == 3
    assert [(name, lines) for _id, name, lines in store.speakers()] == [
        ("Speaker 1", 5), ("Speaker 2", 4), ("Speaker 3", 3)]
    text = (tmp_path / "transcripts" / "2026-11-13.md").read_text()
    assert "- **09:00:00** Speaker 1: line 0" in text
    assert "- **09:00:24** mm" in text
    assert store.label_speakers(Voices(), fake_audio) == 0  # nothing new
    store.close()


def test_too_few_lines_are_left_unlabelled(tmp_path):
    store = make_store(tmp_path, lines=9)
    assert store.label_speakers(Voices(), fake_audio) == 0
    assert set(groups(store)) == {None} and store.speakers() == []
    store.close()


def test_rename_and_merge(tmp_path):
    store = make_store(tmp_path)
    store.label_speakers(Voices(), fake_audio)
    assert store.rename_speaker(1, "  Andrei  ") == 1
    transcript = tmp_path / "transcripts" / "2026-11-13.md"
    assert "Andrei: line 0" in transcript.read_text()
    # Giving speaker 3 the same name (any capitalisation) merges it into speaker 1.
    assert store.rename_speaker(3, "andrei") == 1
    assert [(name, lines) for _id, name, lines in store.speakers()] == [("Andrei", 8), ("Speaker 2", 4)]
    assert "Speaker 3" not in transcript.read_text()
    # Only speakers somebody named are offered as suggestions.
    assert [(name, lines) for name, lines, _last in store.named_speakers()] == [("Andrei", 8)]
    with pytest.raises(ValueError):
        store.rename_speaker(1, "   ")
    with pytest.raises(ValueError):
        store.rename_speaker(99, "Nobody")
    store.close()


def test_reset_regroups_from_stored_fingerprints(tmp_path):
    store = make_store(tmp_path)
    store.label_speakers(Voices(), fake_audio)
    store.rename_speaker(1, "Andrei")
    assert store.reset_speakers() == 12
    assert sorted(lines for _id, _name, lines in store.speakers()) == [3, 4, 5]
    assert all(name.startswith("Speaker ") for _id, name, _lines in store.speakers())
    store.close()


# --- imported conversations: per-conversation labels linked to people ------------

def imported_store(tmp_path):
    """Two imported conversations. In each, labels are different people; voice 0
    is label "1" in the first conversation and label "2" in the second."""
    store = make_store(tmp_path)  # 30 s of audio, lines 2 s apart, WHO says who speaks
    store.db.execute("DELETE FROM utterances")
    seg = store.db.execute("SELECT path, start_ms FROM segments").fetchone()
    plan = {"conv-a": {0: "1", 1: "2", 2: "3"}, "conv-b": {0: "2", 1: "1", 2: "3"}}
    for i, who in enumerate(WHO):
        for source, labels in plan.items():
            store.db.execute(
                "INSERT INTO utterances (segment, start_ms, end_ms, text, source, source_speaker) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (seg[0], seg[1] + i * 2000, seg[1] + i * 2000 + 1900, f"{source} {i}", source, labels[who]))
    # A short line: no fingerprint of its own, but it shares a label.
    store.db.execute(
        "INSERT INTO utterances (segment, start_ms, end_ms, text, source, source_speaker) "
        "VALUES (?, ?, ?, 'mm', 'conv-a', '1')", (seg[0], seg[1] + 100, seg[1] + 400))
    store.db.execute("UPDATE utterances SET speaker_group = source")  # as the importer does
    store.db.commit()
    return store


def people(store, source):
    return dict(store.db.execute(
        "SELECT DISTINCT source_speaker, speaker_id FROM utterances WHERE source = ?", (source,)))


def test_imported_speakers_are_linked_across_conversations(tmp_path):
    store = imported_store(tmp_path)
    assert store.label_speakers(Voices(), fake_audio) == 0  # nothing recorded from the pendant
    # ...and it must not fingerprint every imported line: that is the slow path.
    assert store.db.execute("SELECT COUNT(*) FROM utterances WHERE embedding IS NOT NULL").fetchone() == (0,)
    linked, conflicts = store.link_imported_speakers(Voices(), fake_audio)
    assert (linked, conflicts) == (6, 0)
    a, b = people(store, "conv-a"), people(store, "conv-b")
    assert len(set(a.values())) == 3 and None not in a.values()
    # The same voices carry different labels in the two conversations.
    assert (a["1"], a["2"], a["3"]) == (b["2"], b["1"], b["3"])
    # The short line follows its label.
    assert store.db.execute("SELECT speaker_id FROM utterances WHERE text = 'mm'").fetchone() == (a["1"],)
    assert len(store.speakers()) == 3
    assert store.link_imported_speakers(Voices(), fake_audio) == (0, 0)  # nothing left to do
    store.close()


def test_two_labels_in_one_conversation_never_share_a_person(tmp_path):
    store = imported_store(tmp_path)
    # In conv-b the import split voice 0 across two labels, "2" and "4".
    store.db.execute("UPDATE utterances SET source_speaker = '4' WHERE source = 'conv-b' "
                     "AND text IN ('conv-b 5', 'conv-b 8', 'conv-b 11')")
    store.db.commit()
    linked, conflicts = store.link_imported_speakers(Voices(), fake_audio)
    assert (linked, conflicts) == (7, 1)
    b = people(store, "conv-b")
    assert len(b) == 4 and len(set(b.values())) == 4  # kept apart, though they sound alike
    assert people(store, "conv-a")["1"] in (b["2"], b["4"])
    store.close()


def test_naming_a_speaker_in_one_conversation_only(tmp_path):
    store = imported_store(tmp_path)
    line = store.db.execute(
        "SELECT id FROM utterances WHERE source = 'conv-a' AND source_speaker = '2' LIMIT 1").fetchone()[0]
    # Works before any voices are linked, and covers the whole label in that conversation.
    joseph = store.assign_line_speaker(line, "Joseph")
    assert people(store, "conv-a") == {"1": None, "2": joseph, "3": None}
    assert people(store, "conv-b") == {"1": None, "2": None, "3": None}

    store.link_imported_speakers(Voices(), fake_audio)
    assert people(store, "conv-b")["1"] == joseph  # the same voice elsewhere now carries the name
    assert {name: lines for _id, name, lines in store.speakers()}["Joseph"] == 8

    # Reassigning to another name leaves no empty speaker behind.
    other = store.assign_line_speaker(line, "Karen")
    assert other != joseph and people(store, "conv-a")["2"] == other
    assert people(store, "conv-b")["1"] == joseph
    with pytest.raises(ValueError):
        store.assign_line_speaker(99999, "Nobody")
    with pytest.raises(ValueError):
        store.assign_line_speaker(line, "  ")
    store.close()


def test_unlinking_keeps_named_speakers(tmp_path):
    store = imported_store(tmp_path)
    store.link_imported_speakers(Voices(), fake_audio)
    a = people(store, "conv-a")
    store.rename_speaker(a["1"], "Andrei")
    assert store.unlink_imported_speakers() == 4  # two unnamed people, in two conversations each
    after_a, after_b = people(store, "conv-a"), people(store, "conv-b")
    assert after_a == {"1": a["1"], "2": None, "3": None}
    assert after_b["2"] == a["1"] and after_b["1"] is None
    assert [(name, lines) for _id, name, lines in store.speakers()] == [("Andrei", 11)]
    assert store.unlink_imported_speakers() == 0
    # A reset does not quietly link imported speakers again.
    assert store.reset_speakers() == 0
    assert set(people(store, "conv-a").values()) == {None}
    store.close()


def test_linking_can_be_limited_to_a_date_window(tmp_path):
    store = imported_store(tmp_path)
    start = store.db.execute("SELECT MIN(start_ms) FROM utterances WHERE source = 'conv-a'").fetchone()[0]
    day = 24 * 3600 * 1000
    # conv-b happened the next day.
    store.db.execute("UPDATE utterances SET start_ms = start_ms + ?, end_ms = end_ms + ? WHERE source = 'conv-b'",
                     (day, day))
    store.db.commit()
    linked, _conflicts = store.link_imported_speakers(Voices(), fake_audio, until_ms=start + day // 2)
    assert linked == 3
    assert None not in people(store, "conv-a").values()
    assert people(store, "conv-b") == {"1": None, "2": None, "3": None}
    # Nothing in conv-b was listened to, so it costs nothing until asked for.
    assert store.db.execute("SELECT COUNT(*) FROM utterances WHERE source = 'conv-b' "
                            "AND embedding IS NOT NULL").fetchone() == (0,)
    assert store.link_imported_speakers(Voices(), fake_audio, since_ms=start + 2 * day) == (0, 0)
    store.close()


def test_unnamed_voices_lists_grouped_voices_with_somewhere_to_listen(tmp_path):
    store = imported_store(tmp_path)
    assert store.unnamed_voices() == []  # nothing grouped yet: every label is one conversation
    start = store.db.execute("SELECT MIN(start_ms), MAX(end_ms) FROM utterances").fetchone()
    store.db.execute("INSERT INTO conversations (id, start_ms, end_ms, status, title) VALUES (7, ?, ?, 'done', 'Talk')",
                     start)
    store.db.commit()
    store.link_imported_speakers(Voices(), fake_audio)
    voices = store.unnamed_voices()
    assert len(voices) == 3
    assert [v["ms"] for v in voices] == sorted((v["ms"] for v in voices), reverse=True)
    assert all(v["label"].startswith("Speaker ") and v["examples"][0]["id"] == 7 for v in voices)
    assert all(v["conversations"] == 2 for v in voices)  # conv-a and conv-b
    # A named voice is no longer waiting for a name.
    store.rename_speaker(voices[0]["id"], "Andrei")
    assert [v["id"] for v in store.unnamed_voices()] == [v["id"] for v in voices[1:]]
    store.close()


def test_grouping_from_the_lifelog_runs_in_the_background_and_can_be_undone(tmp_path, monkeypatch):
    import json
    import threading
    import time
    import urllib.error
    import urllib.request

    from limitful import speakers
    from limitful.web import make_server

    imported_store(tmp_path).close()
    monkeypatch.setattr(speakers, "TitanetEmbedder", Voices)
    monkeypatch.setattr(speakers, "load_audio", fake_audio)
    server = make_server(tmp_path, 0, link_speakers=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path, body=None):
        req = urllib.request.Request(
            base + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()

    def finished():
        deadline = time.monotonic() + 5
        while call("/api/status")[1]["running"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        return call("/api/status")[1]

    day = datetime.fromtimestamp(T0 / 1000).strftime("%Y-%m-%d")
    try:
        assert call("/api/voices") == (200, [])
        assert call("/api/voices", {"action": "explode"})[0] == 400
        assert call("/api/voices", {"action": "group", "since": "December"})[0] == 400
        assert call("/api/voices", {"action": "group", "since": day, "until": "2000-01-01"})[0] == 400
        # A window that holds none of them listens to nothing.
        assert call("/api/voices", {"action": "group", "since": "2000-01-01", "until": "2000-01-31"})[0] == 200
        assert finished()["note"] == "No new voices to group in those dates."
        assert call("/api/voices", {"action": "group", "since": day, "until": day}) == (200, {"started": True})
        job = finished()
        assert job["error"] is None and job["note"].startswith("Grouped 6 speakers")
        voices = call("/api/voices")[1]
        assert len(voices) == 3
        # Naming one from the list keeps it through an undo; the rest go back to their labels.
        assert call(f"/api/speakers/{voices[0]['id']}", {"name": "Andrei"})[0] == 200
        assert call("/api/voices", {"action": "ungroup"}) == (200, {"ungrouped": 4})
        assert call("/api/voices") == (200, [])
        assert [p["name"] for p in call("/api/persons")[1]] == ["Andrei"]
    finally:
        server.shutdown()
        server.server_close()
