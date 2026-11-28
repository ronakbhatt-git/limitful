"""Keep Limitful current by itself: fetch the repo it was cloned from and fast-forward.

Limitful runs from a git clone (`uv run limitful ...`). The app asks this module
every few hours whether origin/main has moved on. When it has, and the clone is
on main with no local edits, it fast-forwards. If the dependencies changed it
also runs `uv sync`, but only after the app has quit, because Windows will not
let uv replace files a running Python has open. `relaunch` does that part from
a separate process, then starts the app again.

Anything unusual (another branch checked out, local edits, history that cannot
fast-forward, no git) means it leaves the clone alone and says why.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

BRANCH = "main"
CHECK_EVERY_S = 6 * 3600
# What decides the installed packages; when either changes, `uv sync` has to run.
DEPENDENCY_FILES = ("uv.lock", "pyproject.toml")


@dataclass(frozen=True)
class Update:
    updated: bool
    reason: str
    needs_sync: bool = False


def repo_root() -> Path | None:
    """The git clone this copy of Limitful runs from, if it runs from one."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / ".git").exists() and (root / "pyproject.toml").is_file() else None


def _git(root: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          timeout=timeout, **_quiet())


def _quiet() -> dict:
    """No console window flashing up from the windowless app on Windows."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def check_and_pull(root: Path | None = None, git=_git) -> Update:
    """Fast-forward to origin/main if it is ahead. Never merges, rebases or discards anything."""
    root = root or repo_root()
    if root is None:
        return Update(False, "not running from a git clone")
    try:
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != BRANCH:
            return Update(False, f"the clone is on {branch or 'no branch'}, not {BRANCH}")
        if git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            return Update(False, "the clone has local edits")
        fetched = git(root, "fetch", "--quiet", "origin", BRANCH)
        if fetched.returncode != 0:
            return Update(False, f"could not reach GitHub: {(fetched.stderr or '').strip()[:200]}")
        here = git(root, "rev-parse", "HEAD").stdout.strip()
        there = git(root, "rev-parse", f"origin/{BRANCH}").stdout.strip()
        if not there or here == there:
            return Update(False, "already up to date")
        if git(root, "merge-base", "--is-ancestor", "HEAD", f"origin/{BRANCH}").returncode != 0:
            return Update(False, f"this clone has commits that are not on origin/{BRANCH}")
        changed = git(root, "diff", "--name-only", "HEAD", f"origin/{BRANCH}").stdout.split()
        merged = git(root, "merge", "--ff-only", "--quiet", f"origin/{BRANCH}")
        if merged.returncode != 0:
            return Update(False, f"could not fast-forward: {(merged.stderr or '').strip()[:200]}")
    except (OSError, subprocess.SubprocessError) as err:
        return Update(False, f"git did not run: {err}")
    return Update(True, f"updated {here[:7]} to {there[:7]}",
                  needs_sync=any(name in DEPENDENCY_FILES for name in changed))


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv.exe", Path.home() / ".cargo" / "bin" / "uv.exe",
                      Path.home() / ".local" / "bin" / "uv"):
        if candidate.is_file():
            return str(candidate)
    return None


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def start_relaunch(port: int, argv: list[str], needs_sync: bool, root: Path | None = None) -> None:
    """Start the helper that waits for this app to quit, syncs if needed, and starts it again."""
    root = root or repo_root()
    helper = [sys.executable, "-m", "limitful.updater", "--port", str(port),
              "--root", str(root), *(["--sync"] if needs_sync else []), "--", *argv]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(helper, cwd=str(root), close_fds=True, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **({} if sys.platform == "win32" else {"start_new_session": True}))


def relaunch(port: int, root: Path, sync: bool, argv: list[str], wait_s: float = 60) -> int:
    """The helper itself. It imports nothing that `uv sync` might need to replace."""
    deadline = time.monotonic() + wait_s
    while not port_free(port) and time.monotonic() < deadline:
        time.sleep(0.5)
    time.sleep(1)  # let the old process finish exiting and drop its file handles
    if sync:
        uv = find_uv()
        if uv:
            subprocess.run([uv, "sync", "--directory", str(root)], capture_output=True, timeout=900, **_quiet())
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
    subprocess.Popen(argv, cwd=str(root), close_fds=True, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def _main(args: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="limitful.updater")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(args)
    argv = parsed.argv[1:] if parsed.argv[:1] == ["--"] else parsed.argv
    return relaunch(parsed.port, parsed.root, parsed.sync, argv)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
