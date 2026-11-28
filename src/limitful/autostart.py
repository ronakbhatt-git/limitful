"""Start Limitful when the user logs in, so the pendant syncs without a terminal open.

On macOS this is a LaunchAgent: a small file in ~/Library/LaunchAgents that tells
the system to run `limitful serve --sync-every 120` at login and to restart it
if it stops. On Windows it is a Task Scheduler task that starts the app
(`limitful app`: the Lifelog in its own window, a tray icon, and syncing) with
no console window, and a Start menu entry that opens it. Other systems are not
supported yet.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "dev.limitful.app"
SYNC_EVERY_S = 120


class AutostartError(Exception):
    pass


def supported() -> bool:
    return sys.platform in ("darwin", "win32")


def _windows() -> bool:
    return sys.platform == "win32"


def plist_path(label: str = LABEL, home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=20)


def _target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def job(data_dir: Path, port: int, sync_every: float = SYNC_EVERY_S, label: str = LABEL) -> dict:
    """What the login item runs."""
    data_dir = Path(data_dir).resolve()
    out = {
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "limitful", "serve", "--data-dir", str(data_dir),
                             "--port", str(port), "--sync-every", str(int(sync_every))],
        "RunAtLoad": True,
        "KeepAlive": True,          # start it again if it stops
        # No ProcessType: "Background" has macOS throttle it, and transcribing crawls.
        "StandardOutPath": str(data_dir / "limitful.log"),
        "StandardErrorPath": str(data_dir / "limitful.log"),
    }
    if os.environ.get("LIMITFUL_CONFIG"):
        out["EnvironmentVariables"] = {"LIMITFUL_CONFIG": os.environ["LIMITFUL_CONFIG"]}
    return out


def status(label: str = LABEL, home: Path | None = None, run=None) -> dict:
    if not supported():
        return {"supported": False, "enabled": False, "running": False}
    if _windows():
        return _win_status(label, run or _schtasks)
    run = run or _launchctl
    enabled = plist_path(label, home).is_file()
    return {"supported": True, "enabled": enabled,
            "running": enabled and run("print", _target(label)).returncode == 0}


def enable(data_dir: Path, port: int, start_now: bool = True, sync_every: float = SYNC_EVERY_S,
           label: str = LABEL, home: Path | None = None, run=None, shortcut=None) -> dict:
    """Install the login item. With start_now it is also started straight away."""
    if not supported():
        raise AutostartError("starting at login is only supported on macOS and Windows so far")
    if _windows():
        return _win_enable(data_dir, port, start_now, sync_every, label, run or _schtasks,
                           shortcut or add_start_menu_shortcut)
    run = run or _launchctl
    path = plist_path(label, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    if start_now:
        run("bootout", _target(label))  # an older copy, if one is loaded; fine if not
    with open(path, "wb") as out:
        plistlib.dump(job(data_dir, port, sync_every, label), out)
    if start_now:
        started = run("bootstrap", f"gui/{os.getuid()}", str(path))
        if started.returncode != 0:
            raise AutostartError(f"the login item was written to {path} but could not be started: "
                                 f"{(started.stderr or started.stdout).strip()}")
    return status(label, home, run)


def disable(label: str = LABEL, home: Path | None = None, run=None, stop: bool = True) -> dict:
    """Remove the login item, stopping it if it is running (unless `stop` is False, on Windows)."""
    if _windows():
        run = run or _schtasks
        if stop:
            run("/End", "/TN", _win_name(label))
        run("/Delete", "/TN", _win_name(label), "/F")
        return status(label, home, run)
    run = run or _launchctl
    if supported():
        path = plist_path(label, home)
        path.unlink(missing_ok=True)   # first, in case stopping it stops us
        run("bootout", _target(label))
    return status(label, home, run)


# --- Windows: Task Scheduler -----------------------------------------------------------


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    # No console window flashing up when the windowless app asks.
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, timeout=20,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _win_name(label: str) -> str:
    return f"Limitful\\{label}"


def _windowless_python() -> str:
    """pythonw.exe beside the running interpreter, so no console window opens at login."""
    quiet = Path(sys.executable).with_name("pythonw.exe")
    return str(quiet if quiet.is_file() else sys.executable)


def app_arguments(data_dir: Path, port: int, sync_every: float = SYNC_EVERY_S) -> str:
    """What pythonw.exe is given to start the app, by the login task and the Start menu alike."""
    data_dir = Path(data_dir).resolve()
    return " ".join([
        "-m", "limitful", "--log", f'"{data_dir / "limitful.log"}"', "app",
        "--data-dir", f'"{data_dir}"', "--port", str(port), "--sync-every", str(int(sync_every))])


def start_menu_shortcut_path() -> Path:
    return (Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Limitful.lnk")


def add_start_menu_shortcut(data_dir: Path, port: int, sync_every: float = SYNC_EVERY_S) -> Path:
    """A Limitful entry in the Start menu that opens the app (or its window, if it is running)."""
    from .app import icon_file

    data_dir = Path(data_dir).resolve()
    path = start_menu_shortcut_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The values go in through the environment, so no quoting can break the script.
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LF_LINK); "
              "$s.TargetPath = $env:LF_TARGET; $s.Arguments = $env:LF_ARGS; "
              "$s.WorkingDirectory = $env:LF_DIR; $s.IconLocation = $env:LF_ICON; "
              "$s.Description = 'Limitful: your Lifelog'; $s.Save()")
    env = dict(os.environ, LF_LINK=str(path), LF_TARGET=_windowless_python(),
               LF_ARGS=app_arguments(data_dir, port, sync_every), LF_DIR=str(data_dir),
               LF_ICON=str(icon_file(data_dir)))
    made = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=60, env=env,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if made.returncode != 0:
        raise AutostartError(f"the Start menu entry was not made: {(made.stderr or made.stdout).strip()}")
    return path


def win_task_xml(data_dir: Path, port: int, sync_every: float = SYNC_EVERY_S) -> str:
    """The Task Scheduler definition: start at logon, restart after a crash, never time out."""
    from xml.sax.saxutils import escape

    data_dir = Path(data_dir).resolve()
    arguments = app_arguments(data_dir, port, sync_every)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Limitful: sync the pendant and open the Lifelog</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(os.environ.get("USERDOMAIN", ""))}\\{escape(os.environ.get("USERNAME", ""))}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(_windowless_python())}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(str(data_dir))}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def stop_running(label: str = LABEL, run=None) -> None:
    """Windows: end the login task's process (an older `serve`, say), so a new one can take the port."""
    if _windows():
        (run or _schtasks)("/End", "/TN", _win_name(label))


def _win_status(label: str, run) -> dict:
    found = run("/Query", "/TN", _win_name(label), "/FO", "LIST")
    enabled = found.returncode == 0
    return {"supported": True, "enabled": enabled,
            "running": enabled and "running" in (found.stdout or "").lower()}


def _win_enable(data_dir: Path, port: int, start_now: bool, sync_every: float, label: str, run,
                shortcut) -> dict:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    try:
        shortcut(data_dir, port, sync_every)
    except (AutostartError, OSError, subprocess.SubprocessError) as err:
        print(f"Note: {err}", file=sys.stderr)  # starting at login still works without it
    definition = Path(data_dir) / "autostart-task.xml"
    definition.write_text(win_task_xml(data_dir, port, sync_every), encoding="utf-16")
    try:
        made = run("/Create", "/TN", _win_name(label), "/XML", str(definition), "/F")
    finally:
        definition.unlink(missing_ok=True)
    if made.returncode != 0:
        raise AutostartError(f"Task Scheduler refused the task: {(made.stderr or made.stdout).strip()}")
    if start_now:
        started = run("/Run", "/TN", _win_name(label))
        if started.returncode != 0:
            raise AutostartError(f"the task was created but could not be started: "
                                 f"{(started.stderr or started.stdout).strip()}")
    return _win_status(label, run)
