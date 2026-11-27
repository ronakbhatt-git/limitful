"""The login item, with a stand-in for launchctl and a scratch home folder."""

import json
import plistlib
import os
import sys
import types

import pytest

from limitful import autostart
from limitful.web import Library, pendant_advice  # noqa: F401  (imports the module under test)


class Launchctl:
    def __init__(self, loaded=False, refuse=False):
        self.calls, self.loaded, self.refuse = [], loaded, refuse

    def __call__(self, *args):
        self.calls.append(args)
        code = 0
        if args[0] == "print":
            code = 0 if self.loaded else 113
        elif args[0] == "bootstrap":
            code = 5 if self.refuse else 0
            self.loaded = not self.refuse
        elif args[0] == "bootout":
            code = 0 if self.loaded else 3
            self.loaded = False
        return types.SimpleNamespace(returncode=code, stdout="", stderr="Bootstrap failed: 5: Input/output error")


@pytest.fixture(autouse=True)
def on_a_mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)  # missing on Windows


def test_enable_writes_the_job_and_starts_it(tmp_path):
    run = Launchctl()
    state = autostart.enable(tmp_path / "data", 4747, home=tmp_path, run=run)
    assert state == {"supported": True, "enabled": True, "running": True}
    with open(tmp_path / "Library" / "LaunchAgents" / "dev.limitful.app.plist", "rb") as f:
        job = plistlib.load(f)
    assert job["ProgramArguments"] == [sys.executable, "-m", "limitful", "serve", "--data-dir",
                                       str((tmp_path / "data").resolve()), "--port", "4747", "--sync-every", "120"]
    assert job["RunAtLoad"] is True and job["KeepAlive"] is True
    assert [c[0] for c in run.calls] == ["bootout", "bootstrap", "print"]


def test_enable_for_next_login_only_and_disable(tmp_path):
    run = Launchctl()
    state = autostart.enable(tmp_path / "data", 4800, start_now=False, home=tmp_path, run=run)
    assert state == {"supported": True, "enabled": True, "running": False}
    # Nothing is stopped or started: a copy already running stays up until the next login.
    assert not {"bootstrap", "bootout"} & {c[0] for c in run.calls}
    assert autostart.disable(home=tmp_path, run=run) == {"supported": True, "enabled": False, "running": False}
    assert not autostart.plist_path(home=tmp_path).exists()
    autostart.disable(home=tmp_path, run=run)  # twice is fine


def test_a_refused_start_says_so(tmp_path):
    with pytest.raises(autostart.AutostartError, match="could not be started: Bootstrap failed"):
        autostart.enable(tmp_path / "data", 4747, home=tmp_path, run=Launchctl(refuse=True))


def test_other_systems_are_told_plainly(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert autostart.status(home=tmp_path) == {"supported": False, "enabled": False, "running": False}
    with pytest.raises(autostart.AutostartError, match="only supported on macOS"):
        autostart.enable(tmp_path, 4747, home=tmp_path)


def test_python_m_limitful_runs_the_cli():
    import subprocess
    out = subprocess.run([sys.executable, "-m", "limitful", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip()


def test_syncing_by_itself_is_quiet_when_the_pendant_is_away(tmp_path, monkeypatch):
    import time

    from limitful import watch
    from limitful.store import Store

    Store(tmp_path).close()
    library = Library(tmp_path)
    found = {"value": False}

    async def cycle(store, **kwargs):
        return watch.CycleResult(found=found["value"], pages_new=4 if found["value"] else 0, audio_files=1, lines=2)
    monkeypatch.setattr(watch, "run_cycle", cycle)

    def wait():
        for _ in range(100):
            if not library.job["running"]:
                return
            time.sleep(0.02)

    library.start_sync(quiet=True)
    wait()
    assert (library.job["error"], library.job["note"]) == (None, None) and library.auto["found"] is False
    library.start_sync()            # asked for by hand: an absent pendant is worth saying
    wait()
    assert library.job["error"].startswith("Sync stopped: the pendant is not in range")
    found["value"] = True
    library.start_sync(quiet=True)
    wait()
    assert library.job["note"].startswith("Synced 4 new pages") and library.auto["found"] is True
    assert json.dumps(library.auto)  # what the page is sent


class Schtasks:
    def __init__(self, refuse=False):
        self.calls, self.exists, self.running, self.refuse = [], False, False, refuse

    def __call__(self, *args):
        self.calls.append(args)
        code, out = 0, ""
        if args[0] == "/Create":
            code = 1 if self.refuse else 0
            self.exists = not self.refuse
            self.xml = open(args[args.index("/XML") + 1], encoding="utf-16").read()
        elif args[0] == "/Run":
            self.running = self.exists
        elif args[0] == "/Query":
            code = 0 if self.exists else 1
            out = "Status: Running" if self.running else "Status: Ready"
        elif args[0] == "/End":
            self.running = False
        elif args[0] == "/Delete":
            code = 0 if self.exists else 1
            self.exists = False
        return types.SimpleNamespace(returncode=code, stdout=out, stderr="ERROR: Access is denied.")


def test_windows_task_starts_at_logon_and_can_be_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    run = Schtasks()
    state = autostart.enable(tmp_path / "data", 4747, run=run)
    assert state == {"supported": True, "enabled": True, "running": True}
    assert "<LogonTrigger>" in run.xml and "<RestartOnFailure>" in run.xml
    assert "serve --data-dir" in run.xml and "--sync-every 120" in run.xml and "--log" in run.xml
    assert not (tmp_path / "data" / "autostart-task.xml").exists()  # the definition is not left behind
    assert autostart.disable(run=run) == {"supported": True, "enabled": False, "running": False}


def test_windows_task_refused_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(autostart.AutostartError, match="Access is denied"):
        autostart.enable(tmp_path / "data", 4747, run=Schtasks(refuse=True))
