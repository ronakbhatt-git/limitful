"""Limitful as a desktop app: the Lifelog in its own window, a tray icon, and syncing, in one process.

`limitful app` starts the same server as `limitful serve`, syncing the pendant
by itself, then opens the Lifelog in an app window (Microsoft Edge's app mode on
Windows: no tabs or address bar) and puts an icon in the system tray. Closing
the window leaves Limitful running in the tray; Quit in the tray menu stops it.

Only one copy runs. Starting it again, from the Start menu for example, just
opens the window of the copy that is already running.

It also keeps itself up to date: see updater.py.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import updater

WINDOW_SIZE = "1280,860"


# --- The icon ------------------------------------------------------------------------


def draw_icon(size: int = 256):
    """The Limitful logo (assets/logo.svg) as a Pillow image: six amber petals on green."""
    from PIL import Image, ImageDraw

    scale = size / 128
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([4 * scale, 4 * scale, 124 * scale, 124 * scale], radius=28 * scale, fill="#0b6b58")
    # One petal, pointing up from the centre: a narrow wedge with a round tip.
    petal = [(-2.73, 0.0), (-6.72, -35.28)]
    petal += [(6.72 * math.cos(math.pi - a), -35.28 - 6.72 * math.sin(math.pi - a))
              for a in (i * math.pi / 16 for i in range(1, 16))]
    petal += [(6.72, -35.28), (2.73, 0.0)]
    for k in range(6):
        turn = math.radians(60 * k)
        cos, sin = math.cos(turn), math.sin(turn)
        draw.polygon([((64 + x * cos - y * sin) * scale, (64 + x * sin + y * cos) * scale) for x, y in petal],
                     fill="#f4c63a")
    r = 4.41 * scale
    draw.ellipse([64 * scale - r, 64 * scale - r, 64 * scale + r, 64 * scale + r], fill="#f4c63a")
    return image


def icon_file(data_dir: Path) -> Path:
    """The icon as a .ico in the data folder, for the Start menu shortcut."""
    path = Path(data_dir) / "limitful.ico"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        draw_icon(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


# --- The window ----------------------------------------------------------------------


def app_browser() -> str | None:
    """A Chromium browser that can open a page as an app window: Edge first, as every Windows PC has it."""
    if sys.platform != "win32":
        return None
    roots = [os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for root in filter(None, roots):
        for relative in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
            candidate = Path(root) / relative
            if candidate.is_file():
                return str(candidate)
    return None


def open_window(url: str, browser: str | None = None, popen=subprocess.Popen) -> None:
    browser = browser if browser is not None else app_browser()
    if browser:
        popen([browser, f"--app={url}", f"--window-size={WINDOW_SIZE}"], close_fds=True)
    else:
        webbrowser.open(url)


def already_running(port: int) -> bool:
    """Whether a Lifelog is answering on the port (so this copy should just open its window)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as answer:
            return answer.status == 200 and b"running" in answer.read()
    except OSError:
        return False


# --- The app -------------------------------------------------------------------------


class App:
    def __init__(self, server, url: str, relaunch_argv: list[str], auto_update: bool = True) -> None:
        self.server = server
        self.library = server.library
        self.url = url
        self.relaunch_argv = relaunch_argv
        self.auto_update = auto_update and updater.repo_root() is not None
        self.stopped = threading.Event()
        self.pending_update: updater.Update | None = None
        self.last_update = "not checked yet"
        self.icon = None

    # Things the tray menu does.

    def open(self, *_args) -> None:
        open_window(self.url)

    def sync_now(self, *_args) -> None:
        try:
            self.library.start_sync()
        except RuntimeError:
            pass  # already syncing
        self.open()

    def check_updates(self, *_args) -> None:
        threading.Thread(target=self._update_once, daemon=True).start()

    def quit(self, *_args) -> None:
        self.stopped.set()
        if self.icon is not None:
            self.icon.stop()

    # Updates.

    def _update_once(self) -> None:
        result = updater.check_and_pull()
        self.last_update = result.reason
        print(f"Update check: {result.reason}", flush=True)
        if result.updated:
            self.pending_update = result
            self._restart_when_idle()

    def _restart_when_idle(self) -> None:
        # Never in the middle of a sync or a transcription: wait for the current job to end.
        while self.library.job.get("running") and not self.stopped.is_set():
            time.sleep(5)
        self.quit()

    def _update_loop(self) -> None:
        self.stopped.wait(60)  # let the first sync start first
        while not self.stopped.is_set() and self.pending_update is None:
            self._update_once()
            self.stopped.wait(updater.CHECK_EVERY_S)

    def _tooltip_loop(self) -> None:
        while not self.stopped.wait(3):
            if self.icon is not None:
                what = self.library.job.get("what") if self.library.job.get("running") else ""
                self.icon.title = f"Limitful: {what}" if what else "Limitful"

    # Running.

    def run(self, show_window: bool = True) -> int:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        if show_window:
            self.open()
        if self.auto_update:
            threading.Thread(target=self._update_loop, daemon=True).start()
        try:
            self._tray()
        except KeyboardInterrupt:
            pass
        finally:
            self.server.shutdown()
            self.server.server_close()
        if self.pending_update is not None:
            print("Restarting to finish the update.", flush=True)
            updater.start_relaunch(self.server.server_port, self.relaunch_argv, self.pending_update.needs_sync)
        return 0

    def _tray(self) -> None:
        try:
            import pystray
        except Exception:  # not installed (it is only needed on Windows) or no desktop to show it on
            self.stopped.wait()
            return
        from . import autostart

        def at_login(_item) -> bool:
            return bool(autostart.status().get("enabled"))

        def toggle_login(*_args) -> None:
            try:
                if at_login(None):
                    autostart.disable(stop=False)  # leave this copy running
                else:
                    autostart.enable(self.library.data_dir, self.server.server_port, start_now=False)
            except autostart.AutostartError as err:
                print(f"Start at login: {err}", flush=True)

        menu = pystray.Menu(
            pystray.MenuItem("Open Lifelog", self.open, default=True),
            pystray.MenuItem("Sync now", self.sync_now),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start when I log in", toggle_login, checked=at_login),
            pystray.MenuItem("Check for updates", self.check_updates, enabled=lambda _item: self.auto_update),
            pystray.MenuItem(lambda _item: f"Updates: {self.last_update}", lambda *_: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Limitful", self.quit),
        )
        self.icon = pystray.Icon("limitful", draw_icon(64), "Limitful", menu)
        threading.Thread(target=self._tooltip_loop, daemon=True).start()
        self.icon.run()


def relaunch_argv() -> list[str]:
    """How to start this same app again, with no console window on Windows."""
    from .autostart import _windowless_python

    python = _windowless_python() if sys.platform == "win32" else sys.executable
    return [python, "-m", "limitful", *sys.argv[1:]]
