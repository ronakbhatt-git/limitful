"""One copy of Limitful talks to the pendant at a time, across processes.

The app, `limitful sync` and `limitful watch` can all be running at once. Two of
them syncing together would fight over the Bluetooth link, so each takes this
lock first. It is a lock on a file in the data folder, so the system releases it
when a process exits, even after a crash.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

NAME = "pendant.lock"


class Busy(Exception):
    """Another process holds the lock."""


@contextmanager
def held(data_dir: Path):
    """Hold the lock for the block, or raise Busy at once if another process has it."""
    path = Path(data_dir) / NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _lock(fd)
        except OSError:
            raise Busy("another copy of Limitful is syncing the pendant") from None
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
