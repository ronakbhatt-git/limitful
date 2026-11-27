"""The second-brain export: which days and conversations go out, encrypted audio, git."""

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from limitful import brain
from limitful.store import Store

DAY = "2026-11-20"
T0 = int(datetime(2026, 11, 20, 9, 0).timestamp() * 1000)
LATER = int(datetime(2026, 11, 23, 9, 0).timestamp() * 1000)  # well past the 24-hour delay


def _talk(store, cid, start, *, tier, sensitive=0, status="done", text="we agreed to ship on Friday"):
    seg = f"audio/{DAY}/rec_{cid}.ogg"
    (store.data_dir / seg).parent.mkdir(parents=True, exist_ok=True)
    (store.data_dir / seg).write_bytes(b"OggS fake audio " + bytes([cid]))
    store.db.execute("INSERT INTO segments (path, start_ms, duration_ms, first_page, last_page) "
                     "VALUES (?, ?, 60000, -1, -1)", (seg, start))
    store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES (?, ?, ?, ?)",
                     (seg, start + 1000, start + 3000, text))
    store.db.execute("INSERT INTO conversations (id, start_ms, end_ms, status, title, summary, tier, tags, sensitive) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (cid, start, start + 3000, status, f"Talk {cid}", json.dumps(["point"]), tier,
                      json.dumps(["work"]), sensitive))
    store.db.commit()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    yield s
    s.close()


def _settings(tmp_path, **kw):
    public, private = brain.new_key()
    return brain.Settings(repo=tmp_path / "repo", audio=tmp_path / "drive", recipient=public, **kw), private


def test_tiers_decide_what_goes_out(store, tmp_path):
    _talk(store, 1, T0, tier="keep")
    _talk(store, 2, T0 + 600_000, tier="text", text="the plumber comes Tuesday")
    _talk(store, 3, T0 + 1_200_000, tier="hold", text="TV in the background")
    _talk(store, 4, T0 + 1_800_000, tier="keep", sensitive=1, text="a hard talk about money")
    settings, private = _settings(tmp_path)

    result = brain.export(store, settings, now_ms=LATER, git=False)

    md = (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")
    assert "ship on Friday" in md and "plumber" in md
    assert "TV in the background" not in md and "hard talk" not in md
    lines = [json.loads(line) for line in
             (settings.repo / "data/2026/2026-11" / f"{DAY}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["conversation"] for line in lines] == [1, 2]
    assert lines[0]["audio"]["file"] == f"2026/2026-11/{DAY}/rec_1.ogg.age"
    assert lines[1]["audio"] is None  # text tier: transcript only
    assert result.audio_added == 1
    assert sorted(p.name for p in (settings.audio).rglob("*.age")) == ["rec_1.ogg.age"]
    for name in ("index.md", "reminders.md", "people.md", "AGENTS.md", "README.md"):
        assert (settings.repo / name).exists()

    # The audio is encrypted, and the private key opens it.
    encrypted = next(settings.audio.rglob("*.age")).read_bytes()
    assert b"fake audio" not in encrypted
    assert brain.restore(settings.audio, private, tmp_path / "out") == 1
    assert (tmp_path / "out" / "2026/2026-11" / DAY / "rec_1.ogg").read_bytes().startswith(b"OggS fake audio")


def test_sensitive_goes_out_only_when_asked(store, tmp_path):
    _talk(store, 4, T0, tier="keep", sensitive=1, text="a hard talk about money")
    settings, _ = _settings(tmp_path, include_sensitive=True)
    brain.export(store, settings, now_ms=LATER, git=False)
    assert "hard talk" in (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")


def test_recent_days_and_untriaged_days_wait(store, tmp_path):
    _talk(store, 1, T0, tier="keep")
    settings, _ = _settings(tmp_path)
    too_soon = int(datetime(2026, 11, 21, 12, 0).timestamp() * 1000)
    assert brain.ready_days(store, 24, too_soon) == ([], [])

    _talk(store, 2, T0 + 600_000, tier=None, status="pending")
    assert brain.ready_days(store, 24, LATER) == ([], [DAY])
    result = brain.export(store, settings, now_ms=LATER, git=False)
    assert result.days == [] and result.waiting_days == [DAY]
    assert not (settings.repo / "days").exists()


def test_forgotten_and_retiered_conversations_are_removed(store, tmp_path):
    _talk(store, 1, T0, tier="keep")
    settings, _ = _settings(tmp_path)
    brain.export(store, settings, now_ms=LATER, git=False)
    assert list(settings.audio.rglob("*.age"))

    store.db.execute("UPDATE conversations SET tier = 'hold' WHERE id = 1")
    store.db.commit()
    result = brain.export(store, settings, now_ms=LATER, git=False)
    assert result.audio_removed == 1 and not list(settings.audio.rglob("*.age"))
    assert not (settings.repo / "days/2026/2026-11" / f"{DAY}.md").exists()
    assert not (settings.repo / "data/2026/2026-11" / f"{DAY}.jsonl").exists()


def test_audio_needs_a_key(tmp_path):
    with pytest.raises(brain.BrainError):
        brain.save_settings(tmp_path / "repo", tmp_path / "drive", None)
    with pytest.raises(brain.BrainError):
        brain.save_settings(tmp_path / "repo", tmp_path / "drive", "not-a-key")
    public, _ = brain.new_key()
    saved = brain.save_settings(tmp_path / "repo", tmp_path / "drive", public, delay_hours=12)
    assert saved.recipient == public and saved.delay_hours == 12 and saved.audio == (tmp_path / "drive").resolve()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_commits_to_the_repo(store, tmp_path):
    _talk(store, 1, T0, tier="text")
    settings, _ = _settings(tmp_path)
    settings.repo.mkdir()
    subprocess.run(["git", "init", "-q", str(settings.repo)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(settings.repo), "config", key, value], check=True)

    first = brain.export(store, settings, now_ms=LATER)
    assert first.committed and not first.pushed  # no remote
    again = brain.export(store, settings, now_ms=LATER)
    assert not again.committed  # nothing changed
    log = subprocess.run(["git", "-C", str(settings.repo), "log", "--oneline"], capture_output=True, text=True)
    assert len(log.stdout.splitlines()) == 1


def test_not_a_repo_is_an_error(store, tmp_path):
    _talk(store, 1, T0, tier="text")
    settings, _ = _settings(tmp_path)
    with pytest.raises(brain.BrainError, match="not a git repository"):
        brain.export(store, settings, now_ms=LATER)


def test_cli_keygen_and_setup(tmp_path, capsys):
    from limitful.cli import main

    assert main(["brain", "keygen"]) == 0
    out = capsys.readouterr().out
    public = next(word for word in out.split() if word.startswith("age1"))
    assert "AGE-SECRET-KEY-" in out
    assert main(["brain", "setup", "--repo", str(tmp_path / "repo"), "--audio", str(tmp_path / "drive"),
                 "--recipient", public]) == 0
    assert brain.settings().recipient == public
    # Running setup again without the key keeps the saved one.
    assert main(["brain", "setup", "--repo", str(tmp_path / "repo2")]) == 0
    assert brain.settings().recipient == public and brain.settings().audio == (tmp_path / "drive").resolve()


def test_a_day_waiting_again_keeps_what_it_had(store, tmp_path):
    _talk(store, 1, T0, tier="keep")
    settings, _ = _settings(tmp_path)
    brain.export(store, settings, now_ms=LATER, git=False)
    _talk(store, 2, T0 + 600_000, tier=None, status="pending")
    result = brain.export(store, settings, now_ms=LATER, git=False)
    assert result.waiting_days == [DAY] and result.audio_removed == 0
    assert (settings.repo / "days/2026/2026-11" / f"{DAY}.md").exists()
