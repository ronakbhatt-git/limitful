"""The desktop app: its icon, its window, one copy at a time, and updating itself."""

import os
import subprocess
import sys
import threading
import types

import pytest

from limitful import app as app_mod
from limitful import cli, pendant_lock, updater
from limitful import watch as watch_mod
from limitful.store import Store
from limitful.web import make_server


def test_the_icon_is_the_logo(tmp_path):
    image = app_mod.draw_icon(128)
    assert image.size == (128, 128) and image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0                      # rounded corner: transparent
    assert image.getpixel((20, 100))[:3] == (0x0b, 0x6b, 0x58)  # green tile
    assert image.getpixel((64, 40))[:3] == (0xf4, 0xc6, 0x3a)   # the petal pointing up
    path = app_mod.icon_file(tmp_path)
    assert path.read_bytes()[:4] == b"\x00\x00\x01\x00"         # a Windows .ico


def test_the_window_is_an_app_window_when_edge_is_there(monkeypatch):
    launched = []
    app_mod.open_window("http://localhost:4747", browser="C:/Edge/msedge.exe",
                        popen=lambda argv, **kw: launched.append(argv))
    assert launched == [["C:/Edge/msedge.exe", "--app=http://localhost:4747", "--window-size=1280,860"]]
    opened = []
    monkeypatch.setattr(app_mod.webbrowser, "open", opened.append)
    app_mod.open_window("http://localhost:4747", browser="")
    assert opened == ["http://localhost:4747"]


def test_starting_it_again_opens_the_running_copy(tmp_path, monkeypatch):
    server = make_server(tmp_path, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opened = []
    monkeypatch.setattr(app_mod, "open_window", opened.append)
    try:
        port = server.server_port
        assert cli.main(["app", "--data-dir", str(tmp_path), "--port", str(port)]) == 0
        assert opened == [f"http://localhost:{port}"]
        assert cli.main(["app", "--data-dir", str(tmp_path), "--port", str(port), "--background"]) == 0
        assert len(opened) == 1  # in the background it opens nothing
    finally:
        server.shutdown()
        server.server_close()


def test_quitting_stops_the_server_and_an_update_relaunches(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", None)  # no tray here: the app waits until it is stopped
    relaunched = []
    monkeypatch.setattr(updater, "start_relaunch", lambda port, argv, sync: relaunched.append((argv, sync)))
    server = make_server(tmp_path, 0)
    app = app_mod.App(server, "http://localhost", ["python", "-m", "limitful", "app"], auto_update=False)
    app.pending_update = updater.Update(True, "updated", needs_sync=True)
    threading.Timer(0.3, app.quit).start()
    assert app.run(show_window=False) == 0
    assert updater.port_free(server.server_port)
    assert relaunched == [(["python", "-m", "limitful", "app"], True)]


def test_an_update_waits_for_the_sync_to_finish(tmp_path, monkeypatch):
    server = make_server(tmp_path, 0)
    try:
        app = app_mod.App(server, "http://localhost", [], auto_update=False)
        app.library.job["running"] = True
        monkeypatch.setattr(app_mod.time, "sleep", lambda s: app.library.job.update(running=False))
        app._restart_when_idle()
        assert app.stopped.is_set()
    finally:
        server.server_close()


# --- The pendant lock ----------------------------------------------------------------


def test_only_one_copy_syncs_the_pendant(tmp_path):
    with pendant_lock.held(tmp_path):
        with pytest.raises(pendant_lock.Busy):
            with pendant_lock.held(tmp_path):
                pass
        store = Store(tmp_path)
        try:
            import asyncio
            result = asyncio.run(watch_mod.run_cycle(store, ack=True, get_transcriber=None))
        finally:
            store.close()
        assert result == watch_mod.CycleResult(found=False, busy=True)
    with pendant_lock.held(tmp_path):  # free again once released
        pass


def test_the_lock_is_held_across_processes(tmp_path):
    code = ("import sys, time; from limitful import pendant_lock\n"
            "with pendant_lock.held(sys.argv[1]):\n    print('held', flush=True); time.sleep(30)\n")
    holder = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(pendant_lock.Busy):
            with pendant_lock.held(tmp_path):
                pass
    finally:
        holder.kill()
        holder.wait()
    with pendant_lock.held(tmp_path):  # the system let go of it when the process ended
        pass


# --- Updating itself -----------------------------------------------------------------


def _git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture
def clones(tmp_path, monkeypatch):
    """GitHub (a bare repo), the PC's clone of it, and a second clone to push changes from."""
    for name, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                        ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(name, value)
    origin, pc, dev = tmp_path / "origin.git", tmp_path / "pc", tmp_path / "dev"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(dev)], check=True, capture_output=True)
    _git(dev, "checkout", "-b", "main")
    (dev / "pyproject.toml").write_text("[project]\n")
    (dev / "uv.lock").write_text("v1\n")
    _git(dev, "add", ".")
    _git(dev, "commit", "-m", "first")
    _git(dev, "push", "origin", "main")
    subprocess.run(["git", "clone", str(origin), str(pc)], check=True, capture_output=True)

    def push(name, text):
        (dev / name).write_text(text)
        _git(dev, "add", ".")
        _git(dev, "commit", "-m", f"change {name}")
        _git(dev, "push", "origin", "main")
        return _git(dev, "rev-parse", "HEAD")

    return types.SimpleNamespace(pc=pc, push=push)


def test_it_fast_forwards_to_main(clones):
    assert updater.check_and_pull(clones.pc) == updater.Update(False, "already up to date")
    head = clones.push("notes.py", "x = 1\n")
    result = updater.check_and_pull(clones.pc)
    assert result.updated and not result.needs_sync
    assert _git(clones.pc, "rev-parse", "HEAD") == head and (clones.pc / "notes.py").is_file()
    clones.push("uv.lock", "v2\n")
    assert updater.check_and_pull(clones.pc).needs_sync  # new dependencies: uv sync after quitting


def test_it_leaves_local_edits_and_other_branches_alone(clones):
    clones.push("notes.py", "x = 1\n")
    (clones.pc / "uv.lock").write_text("edited here\n")
    assert updater.check_and_pull(clones.pc) == updater.Update(False, "the clone has local edits")
    assert (clones.pc / "uv.lock").read_text() == "edited here\n" and not (clones.pc / "notes.py").exists()
    _git(clones.pc, "checkout", "--", "uv.lock")
    _git(clones.pc, "checkout", "-b", "trying-something")
    assert updater.check_and_pull(clones.pc).reason == "the clone is on trying-something, not main"


def test_it_never_merges_diverged_history(clones):
    clones.push("notes.py", "x = 1\n")
    (clones.pc / "mine.py").write_text("y = 2\n")
    _git(clones.pc, "add", ".")
    _git(clones.pc, "commit", "-m", "local")
    mine = _git(clones.pc, "rev-parse", "HEAD")
    result = updater.check_and_pull(clones.pc)
    assert not result.updated and "not on origin/main" in result.reason
    assert _git(clones.pc, "rev-parse", "HEAD") == mine


def test_outside_a_clone_it_does_nothing(tmp_path):
    result = updater.check_and_pull(tmp_path)
    assert not result.updated and not any(tmp_path.iterdir())


def test_the_relaunch_waits_for_the_port_then_syncs_and_starts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "port_free", lambda port: True)
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    monkeypatch.setattr(updater, "find_uv", lambda: "uv")
    monkeypatch.setattr(updater.subprocess, "run", lambda argv, **kw: calls.append(("run", argv)))
    monkeypatch.setattr(updater.subprocess, "Popen", lambda argv, **kw: calls.append(("start", argv)))
    assert updater._main(["--port", "4747", "--root", str(tmp_path), "--sync", "--",
                          "pythonw", "-m", "limitful", "app"]) == 0
    assert calls == [("run", ["uv", "sync", "--directory", str(tmp_path)]),
                     ("start", ["pythonw", "-m", "limitful", "app"])]
