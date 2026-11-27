"""Hosted transcription with speaker labels, against a stand-in for Deepgram.

Nothing here talks to the real service: the stand-in replies in the shape
Deepgram's reference describes. That the real service agrees is unverified.
"""

import json
import sys
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from limitful import config, hosted, scoring
from limitful.hosted import SpokenLine
from limitful.protocol import FlashPage
from limitful.store import CONVERSATION_GAP_MS, Store
from limitful.transcribe import make_transcriber

from wire import flash_page, opus_frame

T0 = int(datetime(2026, 11, 13, 9, 0, 0).timestamp() * 1000)
MINUTE = 60_000

REPLY = {
    "metadata": {"diarize_info": {"model": "v2"}},
    "results": {"utterances": [
        {"start": 4.2, "end": 6.0, "transcript": " and  you? ", "speaker": 1,
         "words": [{"word": "and", "start": 4.2, "end": 4.5, "speaker": 1}]},
        {"start": 0.4, "end": 3.9, "transcript": "Hello there.", "speaker": 0, "words": []},
        # No speaker on the line itself: the one heard on most of its words is used.
        {"start": 6.5, "end": 8.0, "transcript": "Fine thanks.",
         "words": [{"word": "fine", "speaker": 0}, {"word": "thanks", "speaker": 2}, {"word": "x", "speaker": 2}]},
        {"start": 9.0, "end": 9.5, "transcript": "Mm."},                # nobody identified
        {"start": 10.0, "end": 10.5, "transcript": "   "},              # empty: dropped
        {"start": "soon", "end": 11.0, "transcript": "Broken."},        # no usable time: dropped
    ]},
}


@pytest.fixture
def deepgram():
    """A stand-in Deepgram server that records each request and replies as configured."""
    state = {"reply": REPLY, "status": 200, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            state["requests"].append({
                "method": self.command, "path": self.path, "headers": dict(self.headers),
                "body": self.rfile.read(length)})
            body = state["reply"] if isinstance(state["reply"], bytes) else json.dumps(state["reply"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET = _answer

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()


# --- the request and the reply ----------------------------------------------------

def test_request_carries_the_audio_the_key_and_asks_for_speakers(deepgram):
    service = hosted.DeepgramTranscriber("dg-secret", language="en", base_url=deepgram["url"])
    assert service.name == "deepgram:nova-3"
    lines = service.transcribe_session(b"OggS-audio")
    request = deepgram["requests"][0]
    url = urllib.parse.urlsplit(request["path"])
    assert (request["method"], url.path) == ("POST", "/v1/listen")
    assert dict(urllib.parse.parse_qsl(url.query)) == {
        "model": "nova-3", "diarize_model": "latest", "utterances": "true",
        "punctuate": "true", "smart_format": "true", "mip_opt_out": "true", "language": "en"}
    assert request["headers"]["Authorization"] == "Token dg-secret"
    assert request["headers"]["Content-Type"] == "audio/ogg"
    assert request["body"] == b"OggS-audio"
    assert lines == [
        SpokenLine(0.4, 3.9, "Hello there.", 0),
        SpokenLine(4.2, 6.0, "and you?", 1),
        SpokenLine(6.5, 8.0, "Fine thanks.", 2),
        SpokenLine(9.0, 9.5, "Mm.", None),
    ]


def test_language_is_left_to_the_service_unless_given(deepgram):
    hosted.DeepgramTranscriber("k", base_url=deepgram["url"]).transcribe_session(b"x", "audio/wav")
    request = deepgram["requests"][0]
    assert "language" not in request["path"]
    assert request["headers"]["Content-Type"] == "audio/wav"


def test_failures_are_explained(deepgram):
    service = hosted.DeepgramTranscriber("k", base_url=deepgram["url"])
    for status, said in [(401, "did not accept that key"), (403, "did not accept that key"),
                         (402, "no credit left"), (429, "rate-limiting"), (500, "Deepgram error 500")]:
        deepgram["status"] = status
        with pytest.raises(hosted.HostedError, match=said):
            service.transcribe_session(b"x")
    deepgram["status"], deepgram["reply"] = 200, b"<html>"
    with pytest.raises(hosted.HostedError, match="not valid JSON"):
        service.transcribe_session(b"x")
    deepgram["reply"] = {"results": {"channels": []}}
    with pytest.raises(hosted.HostedError, match="no utterances"):
        service.transcribe_session(b"x")
    deepgram["reply"] = []
    with pytest.raises(hosted.HostedError, match="expected format"):
        service.transcribe_session(b"x")
    with pytest.raises(hosted.HostedError, match="could not reach"):
        hosted.DeepgramTranscriber("k", base_url="http://127.0.0.1:9").transcribe_session(b"x")
    with pytest.raises(hosted.HostedError, match="add a Deepgram API key"):
        hosted.DeepgramTranscriber("")


def test_checking_a_key_sends_no_audio(deepgram):
    deepgram["reply"] = {"projects": []}
    hosted.check_deepgram("dg-secret", deepgram["url"])
    request = deepgram["requests"][0]
    assert (request["method"], request["path"], request["body"]) == ("GET", "/v1/projects", b"")
    assert request["headers"]["Authorization"] == "Token dg-secret"
    deepgram["status"] = 401
    with pytest.raises(hosted.HostedError, match="did not accept"):
        hosted.check_deepgram("wrong", deepgram["url"])


# --- choosing the engine ----------------------------------------------------------

def test_local_is_the_default_and_the_choice_is_saved_privately(monkeypatch):
    import os
    import stat

    assert config.transcriber() == "local" and config.deepgram_api_key() is None
    with pytest.raises(hosted.HostedError, match="add a Deepgram API key"):
        make_transcriber("deepgram")
    config.update(transcriber="deepgram", deepgram_api_key="  dg-secret  ")
    assert (config.transcriber(), config.deepgram_api_key()) == ("deepgram", "dg-secret")
    assert sys.platform == "win32" or stat.S_IMODE(os.stat(config.config_path()).st_mode) == 0o600  # no POSIX modes on Windows
    assert make_transcriber("deepgram", language="en").name == "deepgram:nova-3"
    monkeypatch.setenv("LIMITFUL_TRANSCRIBER", "local")  # the environment wins
    assert config.transcriber() == "local"
    monkeypatch.delenv("LIMITFUL_TRANSCRIBER")
    config.update(deepgram_api_key="")
    assert config.deepgram_api_key() is None
    with pytest.raises(ValueError):
        config.update(transcriber="somewhere")
    with pytest.raises(ValueError):
        make_transcriber("somewhere")


# --- sessions: grouping recordings, joining them, and mapping times back -----------

class Service:
    """Replies with lines placed by a function of the pieces it was sent."""

    name = "deepgram:test"

    def __init__(self, lines):
        self.lines = lines
        self.sent = []

    def transcribe_session(self, audio, content_type="audio/ogg"):
        self.sent.append(audio)
        return self.lines.pop(0)


def recorded(tmp_path):
    """Three recordings: two a minute apart (one conversation), one twenty minutes later."""
    store = Store(tmp_path)
    frames = [opus_frame(i) for i in range(50)]
    stamps = [T0, T0 + 1000, T0 + 2000, T0 + MINUTE, T0 + MINUTE + 1000, T0 + 20 * MINUTE]
    for index, stamp in enumerate(stamps):
        store.add_page(FlashPage(index=index, session=1, seq=0, data=flash_page(stamp, frames)))
    store.commit()
    assert len(store.export_audio()) == 3
    return store


def test_recordings_are_grouped_into_conversations_and_the_newest_waits(tmp_path):
    store = recorded(tmp_path)
    later = T0 + 3 * 60 * MINUTE
    sessions = store.pending_sessions(later)
    assert [[start - T0 for _path, start, _length in session] for session in sessions] == [
        [0, MINUTE], [20 * MINUTE]]
    # Two minutes after the last recording it may still be going on.
    soon = T0 + 22 * MINUTE
    assert len(store.pending_sessions(soon)) == 1
    assert len(store.pending_sessions(T0 + 20 * MINUTE + CONVERSATION_GAP_MS + 5000)) == 2
    store.close()


def test_a_session_is_sent_as_one_piece_and_lines_land_in_the_right_recording(tmp_path):
    store = recorded(tmp_path)
    first, second = store.pending_sessions(T0 + 3 * 60 * MINUTE)
    audio, pieces = store.session_audio(first)
    assert audio.startswith(b"OggS")
    (path_a, wall_a, at_a, length_a), (path_b, wall_b, at_b, length_b) = pieces
    assert (wall_a, wall_b, at_a) == (T0, T0 + MINUTE, 0.0)
    assert at_b == pytest.approx(length_a) and length_a == pytest.approx(1.5 * length_b)
    durations = dict(store.db.execute("SELECT path, duration_ms FROM segments"))
    assert length_a * 1000 == pytest.approx(durations[path_a], abs=1)

    service = Service([
        [SpokenLine(0.5, 1.0, "hello", 0),
         SpokenLine(length_a + 0.25, length_a + 0.75, "hi back", 1),   # a quarter second into the second recording
         SpokenLine(length_a + 0.9, length_a + 1.2, "how are you", 0),
         SpokenLine(length_a + 1.3, length_a + 1.4, "mm", None)],
        [SpokenLine(0.1, 0.6, "later on", 0)],
    ])
    progress = []
    added, files = store.transcribe_sessions(service, lambda n, of: progress.append((n, of)), T0 + 3 * 60 * MINUTE)
    assert added == 5 and progress == [(1, 2), (2, 2)] and len(files) == 1
    assert service.sent[0] == audio and len(service.sent) == 2
    rows = store.db.execute(
        "SELECT text, segment, start_ms - ?, end_ms - start_ms, source_speaker, speaker_group, source "
        "FROM utterances ORDER BY start_ms", (T0,)).fetchall()
    group = f"deepgram:{T0}"
    assert rows == [
        ("hello", path_a, 500, 500, "1", group, None),
        ("hi back", path_b, MINUTE + 250, 500, "2", group, None),       # clock time of the second recording
        ("how are you", path_b, MINUTE + 900, 300, "1", group, None),
        ("mm", path_b, MINUTE + 1300, 100, "", group, None),
        ("later on", second[0][0], 20 * MINUTE + 100, 500, "1", f"deepgram:{T0 + 20 * MINUTE}", None),
    ]
    assert store.pending_sessions(T0 + 3 * 60 * MINUTE) == [] and store.pending_transcription() == []
    assert set(store.db.execute("SELECT engine FROM transcriptions")) == {("deepgram:test",)}
    text = files[0].read_text()
    assert "- **09:00:00** Speaker 1: hello" in text and "- **09:01:00** Speaker 2: hi back" in text
    assert "- **09:01:01** mm" in text

    # Naming a numbered speaker covers that conversation only, as with imported ones.
    line = store.db.execute("SELECT id FROM utterances WHERE text = 'hello'").fetchone()[0]
    andrei = store.assign_line_speaker(line, "Andrei")
    named = dict(store.db.execute("SELECT text, speaker_id FROM utterances"))
    assert named == {"hello": andrei, "how are you": andrei, "hi back": None, "mm": None, "later on": None}
    store.close()


def test_transcribe_pending_uses_sessions_for_a_hosted_service_and_keeps_the_rest_on_failure(tmp_path):
    store = recorded(tmp_path)

    class Failing(Service):
        def transcribe_session(self, audio, content_type="audio/ogg"):
            if not self.lines:
                raise hosted.HostedError("could not reach Deepgram")
            return super().transcribe_session(audio, content_type)

    # The last recording was hours ago by the real clock, so both sessions are due.
    with pytest.raises(hosted.HostedError):
        store.transcribe_pending(Failing([[SpokenLine(0.5, 1.0, "hello", 0)]]))
    assert store.db.execute("SELECT text FROM utterances").fetchall() == [("hello",)]
    assert len(store.pending_sessions()) == 1  # only the one that failed is left
    # A redo sends pendant recordings again, and leaves imported ones with Limitless's transcript.
    store.db.execute("INSERT INTO transcriptions VALUES ('audio/x/limitless_1.ogg', 'limitless', 0)")
    store.forget_transcriptions()
    assert len(store.pending_sessions()) == 2
    assert store.db.execute("SELECT segment FROM transcriptions").fetchall() == [("audio/x/limitless_1.ogg",)]
    store.close()


def test_voice_labelling_leaves_numbered_speakers_alone(tmp_path):
    store = recorded(tmp_path)
    store.transcribe_sessions(Service([[SpokenLine(0.2, 2.9, "a long enough line", 0)], []]), None, T0 + 3 * 60 * MINUTE)

    class Never:
        def embed(self, clip):
            raise AssertionError("a line with a service's speaker number was fingerprinted")

    assert store.label_speakers(Never(), lambda path: np.zeros(16000 * 5, dtype=np.float32)) == 0
    store.close()


# --- scoring a service against Limitless's labels ----------------------------------

def test_best_pairing_ignores_what_the_speakers_are_called():
    truth = ["1", "1", "2", "2", "3"]
    assert scoring.best_pairing_score(truth, ["b", "b", "a", "a", "c"], [1, 1, 1, 1, 1]) == 1.0
    # One guessed speaker cannot stand for two real ones.
    assert scoring.best_pairing_score(truth, ["a"] * 5, [1, 1, 1, 1, 1]) == pytest.approx(0.4)
    # Weighted by speaking time; a line nobody was heard on counts against.
    assert scoring.best_pairing_score(["1", "2"], ["a", None], [3.0, 1.0]) == pytest.approx(0.75)
    assert scoring.best_pairing_score([], [], []) == 0.0
    many = [str(i) for i in range(9)]
    assert scoring.best_pairing_score(many, list(range(9)), [1.0] * 9) == 1.0
    assert scoring.majority_share(["1", "1", "2"], [1.0, 2.0, 1.0]) == pytest.approx(0.75)


def test_speaker_of_a_span_is_whoever_overlaps_it_most():
    turns = [(0.0, 2.0, 0), (2.0, 5.0, 1), (9.0, 10.0, None)]
    assert scoring.speakers_by_overlap([(0.0, 1.0), (1.5, 4.0), (6.0, 7.0), (9.0, 10.0)], turns) == [0, 1, None, None]


def scored_store(tmp_path):
    """One imported conversation: 60 half-second lines, two speakers taking turns."""
    store = Store(tmp_path)
    for index in range(40):
        store.add_page(FlashPage(index=index, session=1, seq=0,
                                 data=flash_page(T0 + index * 1000, [opus_frame(i) for i in range(50)])))
    store.commit()
    path, start = [(s.path.relative_to(tmp_path).as_posix(), s.start_ms) for s in store.export_audio()][0]
    for i in range(60):
        # Limitless's times run 0.9 s late.
        at = start + 900 + i * 500
        store.db.execute(
            "INSERT INTO utterances (segment, start_ms, end_ms, text, source, speaker_group, source_speaker) "
            "VALUES (?, ?, ?, ?, 'conv', 'conv', ?)", (path, at, at + 400, f"line {i}", str(i % 2 + 1)))
    store.db.execute("INSERT INTO conversations (start_ms, end_ms, status, title, engine) "
                     "VALUES (?, ?, 'done', 'A chat', 'limitless')", (start, start + 40_000))
    store.db.commit()
    store.close()


def test_scoring_sends_the_conversation_once_and_compares_speakers(tmp_path):
    scored_store(tmp_path)
    sent = []

    class Right:
        name = "deepgram:test"

        def transcribe_session(self, audio, content_type="audio/ogg"):
            sent.append((audio[:4], content_type))
            return [SpokenLine(i * 0.5, i * 0.5 + 0.4, "x", 7 if i % 2 else 3) for i in range(60)]

    class OneVoice(Right):
        def transcribe_session(self, audio, content_type="audio/ogg"):
            return [SpokenLine(0.0, 40.0, "x", 0)]

    silence = lambda path: np.zeros(16000 * 40, dtype=np.float32)  # noqa: E731
    assert scoring.score_service(tmp_path, Right(), silence, 5) == [("A chat", 60, 2, 2, 1.0, 0.5)]
    assert sent == [(b"RIFF", "audio/wav")]
    [(_title, _lines, _speakers, found, score, baseline)] = scoring.score_service(tmp_path, OneVoice(), silence, 5)
    assert (found, score, baseline) == (1, 0.5, 0.5)
    assert scoring.score_service(tmp_path, Right(), silence, 0) == []
