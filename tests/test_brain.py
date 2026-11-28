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


def test_every_transcript_goes_out_and_tiers_decide_the_audio(store, tmp_path):
    _talk(store, 1, T0, tier="keep")
    _talk(store, 2, T0 + 600_000, tier="text", text="the plumber comes Tuesday")
    _talk(store, 3, T0 + 1_200_000, tier="hold", text="TV in the background")
    _talk(store, 4, T0 + 1_800_000, tier="keep", sensitive=1, text="a hard talk about money")
    settings, private = _settings(tmp_path)

    result = brain.export(store, settings, now_ms=LATER, git=False)

    md = (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")
    for words in ("ship on Friday", "plumber", "TV in the background", "hard talk"):
        assert words in md
    assert "keep · sensitive" in md
    lines = [json.loads(line) for line in
             (settings.repo / "data/2026/2026-11" / f"{DAY}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["conversation"] for line in lines] == [1, 2, 3, 4]
    assert lines[0]["audio"]["file"] == f"2026/2026-11/{DAY}/rec_1.ogg.age"
    assert lines[1]["audio"] is None and lines[2]["audio"] is None  # text and hold: no audio
    assert lines[3]["sensitive"] is True and lines[3]["tier"] == "keep" and lines[3]["audio"]
    assert result.audio_added == 2
    assert sorted(p.name for p in (settings.audio).rglob("*.age")) == ["rec_1.ogg.age", "rec_4.ogg.age"]
    for name in ("index.md", "reminders.md", "people.md", "AGENTS.md", "README.md"):
        assert (settings.repo / name).exists()

    # The audio is encrypted, and the private key opens it.
    encrypted = next(settings.audio.rglob("*.age")).read_bytes()
    assert b"fake audio" not in encrypted
    assert brain.restore(settings.audio, private, tmp_path / "out") == 2
    assert (tmp_path / "out" / "2026/2026-11" / DAY / "rec_1.ogg").read_bytes().startswith(b"OggS fake audio")


def test_recent_days_wait_and_untriaged_ones_go_out_without_audio(store, tmp_path):
    _talk(store, 1, T0, tier=None, status="pending")
    settings, _ = _settings(tmp_path)
    too_soon = int(datetime(2026, 11, 21, 12, 0).timestamp() * 1000)
    assert brain.ready_days(store, 24, too_soon) == ([], [])
    assert brain.ready_days(store, 24, LATER) == ([DAY], [DAY])

    result = brain.export(store, settings, now_ms=LATER, git=False)
    assert result.days == [DAY] and result.waiting_days == [DAY] and result.audio_added == 0
    assert "not triaged yet" in (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")

    from limitful import triage
    triage.save(store, 1, title="Shipping", summary=[], notes=[], reminders=[], tier="keep",
                tags=["work"], sensitive=False)
    result = brain.export(store, settings, now_ms=LATER, git=False)
    assert result.waiting_days == [] and result.audio_added == 1


def test_forgotten_and_retiered_conversations_are_removed(store, tmp_path):
    _talk(store, 1, T0, tier="keep")
    settings, _ = _settings(tmp_path)
    brain.export(store, settings, now_ms=LATER, git=False)
    assert list(settings.audio.rglob("*.age"))

    store.db.execute("UPDATE conversations SET tier = 'hold' WHERE id = 1")
    store.db.commit()
    result = brain.export(store, settings, now_ms=LATER, git=False)
    assert result.audio_removed == 1 and not list(settings.audio.rglob("*.age"))
    assert (settings.repo / "days/2026/2026-11" / f"{DAY}.md").exists()  # the words stay

    # Forgotten: the words and the conversation are gone from the Lifelog.
    store.db.execute("DELETE FROM utterances")
    store.db.execute("DELETE FROM conversations")
    store.db.commit()
    brain.export(store, settings, now_ms=LATER, git=False)
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


def test_imported_conversations_are_triaged_for_their_audio(store, tmp_path):
    """Imported from Limitless: finished and titled, but never tiered."""
    from limitful import triage

    _talk(store, 9, T0, tier=None, text="a hard talk about money")
    settings, _ = _settings(tmp_path)
    result = brain.export(store, settings, now_ms=LATER, git=False)
    assert result.days == [DAY] and result.waiting_days == [DAY] and result.audio_added == 0

    assert [c["id"] for c in triage.pending(store)] == [9] and triage.waiting(store) == 1
    triage.save(store, 9, title="Money", summary=[], notes=[], reminders=[], tier="keep",
                tags=["money"], sensitive=True)
    assert triage.waiting(store) == 0
    result = brain.export(store, settings, now_ms=LATER, git=False)
    assert result.days == [DAY] and result.waiting_days == [] and result.audio_added == 1
    with pytest.raises(ValueError, match="already been triaged"):
        triage.save(store, 9, title="Again", summary=[], notes=[], reminders=[], tier="keep",
                    tags=[], sensitive=False)


def test_excluded_conversations_stay_out_of_the_repo(store, tmp_path, capsys):
    from limitful.cli import main

    _talk(store, 1, T0, tier="keep")
    _talk(store, 2, T0 + 600_000, tier="keep", sensitive=1, text="my card number is four one one one")
    store.db.execute("INSERT INTO reminders (conversation_id, text, status, created_ms) VALUES (2, 'cancel the card', 'accepted', 0)")
    store.db.commit()
    assert main(["brain", "setup", "--repo", str(tmp_path / "repo")]) == 0
    assert main(["brain", "exclude", "2"]) == 0
    assert "Kept out of the repo: 2" in capsys.readouterr().out
    # Running setup again keeps the exclusions.
    assert main(["brain", "setup", "--repo", str(tmp_path / "repo")]) == 0
    settings = brain.settings()
    assert settings.exclude == {2}

    result = brain.export(store, settings, now_ms=LATER, git=False)

    assert result.conversations == 1
    everything = "".join(p.read_text(encoding="utf-8") for p in settings.repo.rglob("*") if p.is_file())
    assert "ship on Friday" in everything
    assert "card number" not in everything and "cancel the card" not in everything and "Talk 2" not in everything

    assert main(["brain", "include", "2"]) == 0
    assert brain.export(store, brain.settings(), now_ms=LATER, git=False).conversations == 2


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _clone(remote, where):
    subprocess.run(["git", "clone", "-q", str(remote), str(where)], check=True, capture_output=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        _git(where, "config", key, value)
    return where


def test_each_conversation_links_to_its_audio_in_limitful(store, tmp_path):
    _talk(store, 7, T0, tier="text")
    settings, _ = _settings(tmp_path)

    brain.export(store, settings, now_ms=LATER, git=False)
    md = (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")
    assert f"[Listen in Limitful](http://localhost:4747/#{DAY}/c7/play)" in md

    settings.listen_url = None  # links turned off
    brain.export(store, settings, now_ms=LATER, git=False)
    assert "Listen in Limitful" not in (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")


def test_notes_app_files_stay_out_of_git_and_the_owners_ignores_are_kept(store, tmp_path):
    _talk(store, 1, T0, tier="text")
    settings, _ = _settings(tmp_path)
    settings.repo.mkdir()
    (settings.repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")

    brain.export(store, settings, now_ms=LATER, git=False)
    brain.export(store, settings, now_ms=LATER, git=False)
    ignored = (settings.repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ignored == ["scratch/", *brain.IGNORED]  # added once, the owner's line first


def test_notes_written_elsewhere_are_brought_in_before_pushing(store, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    settings, _ = _settings(tmp_path)
    _clone(remote, settings.repo)
    _git(settings.repo, "checkout", "-q", "-b", "main")
    _talk(store, 1, T0, tier="text")
    assert brain.export(store, settings, now_ms=LATER).pushed

    # A note written on another device reaches GitHub first.
    phone = _clone(remote, tmp_path / "phone")
    (phone / "notes").mkdir()
    (phone / "notes" / "idea.md").write_text("call the plumber back\n", encoding="utf-8")
    _git(phone, "add", "-A")
    _git(phone, "commit", "-q", "-m", "note from the phone")
    _git(phone, "push", "-q", "origin", "main")

    # Nothing new here: the note still arrives.
    assert not brain.export(store, settings, now_ms=LATER).committed
    assert (settings.repo / "notes" / "idea.md").exists()

    # Both sides change: the PC's commit goes on top and the push is not refused.
    (phone / "notes" / "second.md").write_text("another\n", encoding="utf-8")
    _git(phone, "add", "-A")
    _git(phone, "commit", "-q", "-m", "second note")
    _git(phone, "push", "-q", "origin", "main")
    _talk(store, 2, T0 + 600_000, tier="text", text="the plumber comes Tuesday")
    result = brain.export(store, settings, now_ms=LATER)
    assert result.pushed
    assert (settings.repo / "notes" / "second.md").exists()
    assert _git(remote, "log", "--format=%s", "main").splitlines()[1] == "second note"


def test_a_clash_with_github_is_left_for_the_owner(store, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    settings, _ = _settings(tmp_path)
    _clone(remote, settings.repo)
    _git(settings.repo, "checkout", "-q", "-b", "main")
    _talk(store, 1, T0, tier="text")
    brain.export(store, settings, now_ms=LATER)

    other = _clone(remote, tmp_path / "other")
    day = other / "days/2026/2026-11" / f"{DAY}.md"
    day.write_text("edited by hand on GitHub\n", encoding="utf-8")
    _git(other, "commit", "-q", "-am", "hand edit")
    _git(other, "push", "-q", "origin", "main")
    store.db.execute("UPDATE conversations SET title = 'Renamed' WHERE id = 1")
    store.db.commit()

    with pytest.raises(brain.BrainError, match="could not be combined"):
        brain.export(store, settings, now_ms=LATER)
    assert not (settings.repo / ".git" / "rebase-merge").exists()  # left clean, not mid-rebase
    assert "Renamed" in (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")


def test_each_day_starts_with_a_timeline_of_its_conversations(store, tmp_path):
    _talk(store, 1, T0, tier="text")
    store.db.execute("UPDATE conversations SET title = 'Budget: round #2; final' WHERE id = 1")
    _talk(store, 2, T0 + 600_000, tier="keep", sensitive=1)
    store.db.commit()
    settings, _ = _settings(tmp_path)

    brain.export(store, settings, now_ms=LATER, git=False)
    md = (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")
    chart = md.split("```mermaid\n", 1)[1].split("```", 1)[0].splitlines()
    assert chart[:4] == ["gantt", "  dateFormat HH:mm", "  axisFormat %H:%M", "  todayMarker off"]
    assert "  Budget - round 2, final :c1, 09:00, 09:01" in chart  # at least a minute; no ':' '#' ';' in names
    assert "  Talk 2 :crit, c2, 09:10, 09:11" in chart  # sensitive shows red
    assert f'  click c2 href "http://localhost:4747/#{DAY}/c2/play"' in chart
    assert md.index("```mermaid") < md.index("## 09:00")  # above the conversations


def test_day_properties_list_only_named_people(store, tmp_path):
    _talk(store, 1, T0, tier="text")
    store.db.execute("INSERT INTO speakers (id, name, voice_sum, lines) VALUES (1, 'Dana Example', x'00000000', 1)")
    store.db.execute("UPDATE utterances SET speaker_id = 1")
    store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text, source_speaker) "
                     "SELECT segment, start_ms + 1500, end_ms, 'hello', '4' FROM utterances LIMIT 1")
    store.db.commit()
    settings, _ = _settings(tmp_path)

    brain.export(store, settings, now_ms=LATER, git=False)
    md = (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")
    assert 'speakers: ["Dana Example"]' in md
    assert "Speaker 4: hello" in md  # still labelled in the transcript itself


def test_git_runs_without_a_console_window(monkeypatch, tmp_path):
    """The tray app has no console, so on Windows each git call would flash a window up."""
    seen = {}

    def run(args, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(brain.subprocess, "run", run)
    monkeypatch.setattr(brain.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    brain._git(tmp_path, "status")
    assert seen["creationflags"] == 0x08000000


def test_transcripts_fold_under_their_summary_and_the_day_links_to_my_notes(store, tmp_path):
    _talk(store, 7, T0, tier="text")
    settings, _ = _settings(tmp_path)
    page = settings.repo / "days/2026/2026-11" / f"{DAY}.md"

    brain.export(store, settings, now_ms=LATER, git=False)
    md = page.read_text(encoding="utf-8")
    assert "My notes" not in md  # no daily note for that day yet
    fold = md.split("> [!quote]- Transcript · ", 1)[1].splitlines()
    assert fold[0] in ("1 line",) or fold[0].endswith(" lines")
    assert all(line.startswith("> - **") for line in fold[1:] if line)
    assert "we agreed to ship on Friday" in "\n".join(fold)



def test_my_notes_sit_between_the_conversations_by_time(store, tmp_path):
    _talk(store, 7, T0, tier="text")                       # 09:00
    _talk(store, 8, T0 + 3 * 3_600_000, tier="text")       # 12:00
    settings, _ = _settings(tmp_path)
    (settings.repo / "notes/daily").mkdir(parents=True)
    (settings.repo / "notes/daily" / f"{DAY}.md").write_text(
        "---\ntype: daily\n---\nPendant: [x](../../days/2026/2026-11/2026-11-20.md)\n\n## Notes\n\n"
        "Call the bank.\n\n### 10:40\nBoard wants the [budget](../attachments/budget.pdf) by Friday.\n\n"
        "### 08:15 Before work\n- gym\n\n### 18:30\nDinner with family, see [site](https://example.com).\n"
        "\n## Pendant\n\n![2026-11-20](../../days/2026/2026-11/2026-11-20.md)\n", encoding="utf-8")

    brain.export(store, settings, now_ms=LATER, git=False)
    md = (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")
    order = [md.index(s) for s in ("> [!note] My notes", "Call the bank", "## 08:15 Before work", "## 09:00 Talk 7",
                                   "## 10:40 My note", "## 12:00 Talk 8", "## 18:30 My note")]
    assert order == sorted(order)
    assert f"> [!note] My note · [edit](../../../notes/daily/{DAY}.md)" in md
    assert "> Board wants the [budget](../../../notes/attachments/budget.pdf) by Friday." in md
    assert "[site](https://example.com)" in md
    assert "Pendant" not in md and "## Notes" not in md  # the template's own lines stay out
    assert f"[Edit in Limitful](http://localhost:4747/#{DAY}/c7)" in md
    chart = md.split("```mermaid\n", 1)[1].split("```", 1)[0]
    assert "  section My notes\n  Before work :milestone, n1, 08:15, 0m\n  Note :milestone, n2, 10:40, 0m\n" in chart
