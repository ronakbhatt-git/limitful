"""Behaviour that differs on Windows, checked on any system."""

from pathlib import Path

from limitful import config
from limitful.recordings import added_recordings
from limitful.store import Store


def test_backslash_paths_from_windows_are_rewritten(tmp_path):
    store = Store(tmp_path)
    old = r"audio\2026-11-27\added_14-30-05_abcd1234.ogg"
    store.db.execute("INSERT INTO segments (path, start_ms, duration_ms, first_page, last_page) "
                     "VALUES (?, 1764250205000, 60000, -1, -1)", (old,))
    store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES (?, 1, 2, 'hi')", (old,))
    store.db.execute("INSERT INTO transcriptions VALUES (?, 'whisper', 0)", (old,))
    store.db.execute("INSERT INTO added (path, name, source, key) VALUES (?, 'memo', 'file', 'k')", (old,))
    store.db.commit()
    store.close()

    store = Store(tmp_path)
    new = "audio/2026-11-27/added_14-30-05_abcd1234.ogg"
    assert store.db.execute("SELECT path FROM segments").fetchone()[0] == new
    assert store.db.execute("SELECT segment FROM utterances").fetchone()[0] == new
    assert store.db.execute("SELECT segment FROM transcriptions").fetchone()[0] == new
    assert store.db.execute("SELECT path FROM added").fetchone()[0] == new
    # The queries that look for added recordings find it again.
    assert [r["path"] for r in added_recordings(store)] == [new]
    store.close()


def test_transcripts_are_written_as_utf8(tmp_path):
    store = Store(tmp_path)
    store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) "
                     "VALUES ('', 1764250205000, 1764250206000, 'Café → 日本')")
    store.db.commit()
    from datetime import datetime

    day = datetime.fromtimestamp(1764250205).strftime("%Y-%m-%d")
    path = store.write_transcript(day)
    assert "Café → 日本" in path.read_bytes().decode("utf-8")
    assert b"\r\n" not in path.read_bytes()
    store.close()


def test_settings_round_trip_as_utf8(tmp_path):
    config.update(api_key="ključ-→")
    assert config.api_key() == "ključ-→"
    assert Path(config.config_path()).read_bytes().decode("utf-8")


def test_whisper_falls_back_to_the_cpu_when_gpu_libraries_are_missing(monkeypatch, tmp_path):
    import faster_whisper

    from limitful.transcribe import WhisperTranscriber

    devices = []

    class FakeSegment:
        start, end, text, avg_logprob = 0.0, 1.0, " hello ", 0.0

    class FakeModel:
        def __init__(self, model, device, compute_type):
            self.device = device
            devices.append(device)

        def transcribe(self, *args, **kwargs):
            def segments():
                if self.device != "cpu":
                    raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                yield FakeSegment()
            return segments(), None

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    lines = WhisperTranscriber("tiny", device="auto").transcribe(tmp_path / "x.ogg")
    assert devices == ["auto", "cpu"]
    assert [line.text for line in lines] == ["hello"]
