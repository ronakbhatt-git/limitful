"""Deleting a stretch of the Lifelog: the words, everything written about them, and the sound."""

import json
import sqlite3

import pytest

from limitful.forget import forget, silence
from limitful.protocol import FlashPage
from limitful.recordings import to_ogg
from limitful.store import Store

from test_web import JSON, T0, _spoken_file, site  # noqa: F401  (site is a fixture)
from wire import flash_page, opus_frame


def _loudness(path, from_s, to_s):
    """Mean absolute sample value in part of an audio file."""
    import av
    import numpy as np

    with av.open(str(path)) as src:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        samples = np.concatenate([piece.to_ndarray()[0] for frame in src.decode(audio=0) for piece in resampler.resample(frame)])
    return float(np.abs(samples[int(from_s * 48000) : int(to_s * 48000)].astype(np.int32)).mean()), len(samples) / 48000


def test_part_of_a_recording_can_be_silenced_without_changing_its_length(tmp_path):
    file = tmp_path / "tone.ogg"
    to_ogg(_spoken_file(tmp_path, seconds=6), file)
    before, length = _loudness(file, 2.2, 3.8)
    silence(file, 2.0, 4.0)
    inside, after = _loudness(file, 2.2, 3.8)
    assert before > 1000 and inside < 20  # the tone is gone from the middle
    assert _loudness(file, 0.2, 1.8)[0] > 1000 and _loudness(file, 4.2, 5.8)[0] > 1000  # and kept either side
    assert abs(after - length) < 0.1
    assert not list(tmp_path.glob(".silencing-*"))


def test_deleting_a_conversation_takes_everything_with_it(site, tmp_path):  # noqa: F811
    (talk,) = json.loads(site("/api/conversations/2026-11-13")[2])
    before = json.loads(site("/api/day/2026-11-13")[2])
    audio = tmp_path / before[0]["audio"].lstrip("/")
    assert audio.is_file() and talk["reminders"] and len(before) >= 3

    assert site("/api/forget", JSON, b'{"start_ms": 5}')[0] == 400
    assert site("/api/forget", JSON, json.dumps({"start_ms": talk["end"], "end_ms": talk["start"]}).encode())[0] == 400
    assert site("/api/forget", dict(JSON, Host="evil.example"), b"{}")[0] == 403
    assert json.loads(site("/api/day/2026-11-13")[2]) == before  # refusals change nothing

    status, _headers, body = site("/api/forget", JSON, json.dumps({"start_ms": talk["start"], "end_ms": talk["end"]}).encode())
    gone = json.loads(body)
    assert status == 200 and gone["conversations"] == 1 and gone["lines"] >= 2
    assert json.loads(site("/api/conversations/2026-11-13")[2]) == []
    after = json.loads(site("/api/day/2026-11-13")[2])
    assert all(not (talk["start"] <= line["start"] <= talk["end"]) for line in after)
    assert len(after) == len(before) - gone["lines"] and after  # the hour-later recording is untouched
    assert json.loads(site("/api/reminders?done=1")[2]) == []
    assert site("/api/search?q=oat")[2] == b"[]" or all(
        not (talk["start"] <= hit["start"] <= talk["end"]) for hit in json.loads(site("/api/search?q=oat")[2]))
    # The sound: this recording lay wholly inside the conversation or ran past it; either way those seconds are gone.
    assert gone["recordings_removed"] + gone["recordings_silenced"] >= 1
    if gone["recordings_removed"]:
        assert not audio.exists()
    assert "Shopping and a call" not in (tmp_path / "transcripts" / "2026-11-13.md").read_text()

    # Syncing the same pages again brings nothing back: that stretch is kept out for good.
    db = sqlite3.connect(tmp_path / "pendant.db")
    assert db.execute("SELECT COUNT(*) FROM pauses WHERE start_ms = ? AND end_ms = ?", (talk["start"], talk["end"])).fetchone()[0] == 1
    db.close()
    store = Store(tmp_path)
    try:
        pages = store.page_count()
        again = FlashPage(index=0, session=1, seq=0, data=flash_page(T0, [opus_frame(i) for i in range(50)]))
        assert store.add_page(again) is False and store.page_count() == pages
    finally:
        store.close()


def test_what_is_deleted_is_bounded(tmp_path):
    store = Store(tmp_path)
    try:
        with pytest.raises(ValueError):
            forget(store, T0, T0 + 3 * 24 * 60 * 60 * 1000)  # never more than a day at once
        assert forget(store, T0 + 1000, T0 + 2000).lines == 0  # nothing there: nothing happens
    finally:
        store.close()


def test_several_conversations_can_be_deleted_at_once(site, tmp_path):  # noqa: F811
    (talk,) = json.loads(site("/api/conversations/2026-11-13")[2])
    before = json.loads(site("/api/day/2026-11-13")[2])
    later = [line for line in before if line["start"] > talk["end"]]
    assert later  # the day also holds a recording an hour on, outside the conversation
    spans = [[talk["start"], talk["end"]], [later[0]["start"], later[-1]["start"]]]

    for bad in ({"spans": []}, {"spans": [[5]]}, {"spans": "all"}, {"spans": [[talk["start"], True]]},
                {"spans": [spans[0], [talk["end"], talk["start"]]]},  # one bad stretch refuses them all
                {"spans": [[n, n] for n in range(501)]}):
        assert site("/api/forget", JSON, json.dumps(bad).encode())[0] == 400
    assert json.loads(site("/api/day/2026-11-13")[2]) == before

    status, _headers, body = site("/api/forget", JSON, json.dumps({"spans": spans}).encode())
    gone = json.loads(body)
    assert status == 200 and gone["conversations"] == 1 and gone["lines"] == len(before)
    assert json.loads(site("/api/day/2026-11-13")[2]) == []
    assert json.loads(site("/api/conversations/2026-11-13")[2]) == []
    assert gone["recordings_removed"] + gone["recordings_silenced"] >= 2


def test_a_failure_part_way_keeps_what_was_already_deleted_in_step(tmp_path, monkeypatch):
    """If the second stretch cannot be deleted, the first is still gone from the written transcript."""
    from limitful import forget as module

    store = Store(tmp_path)
    try:
        real, seen = module._forget_span, []

        def failing(store, start_ms, end_ms, days):
            seen.append(start_ms)
            if len(seen) == 2:
                raise OSError("the file could not be rewritten")
            return real(store, start_ms, end_ms, days)

        monkeypatch.setattr(module, "_forget_span", failing)
        written = []
        monkeypatch.setattr(store, "write_transcript", written.append)
        with pytest.raises(OSError):
            module.forget_all(store, [(T0 + 5000, T0 + 6000), (T0, T0 + 1000)])
        assert seen == [T0, T0 + 5000]  # earliest first
        assert len(written) == 1  # the day the first stretch touched was written out again
    finally:
        store.close()


def test_forgetting_lines_deletes_ask_threads_that_quoted_them(tmp_path):
    from limitful.forget import forget_all

    store = Store(tmp_path)
    kept, gone = (store.db.execute(
        "INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES ('', ?, ?, ?)",
        (start, start + 1000, text)).lastrowid for start, text in ((T0 + 1_000_000, "keep"), (T0 + 5_000_000, "secret")))
    store.db.commit()
    quoting = store.save_exchange(None, "what was the secret?", "It was [L%d]." % gone, {str(gone): {"day": "x"}})
    other = store.save_exchange(None, "what did I keep?", "[L%d]" % kept, {str(kept): {"day": "x"}})
    forget_all(store, [(T0 + 4_999_000, T0 + 5_002_000)])
    assert [c["id"] for c in store.chats()] == [other]
    assert store.chat_messages(quoting) == []
    assert store.db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    store.close()
