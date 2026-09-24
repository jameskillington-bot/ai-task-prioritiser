"""Weekly background run on macOS (launchd), the pop-up, and the app icon.

launchd runs a missed job as soon as the Mac wakes, so a Monday 10:00 run still
happens if the Mac was asleep at the time.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

LABEL = "com.delayrepay.weekly"
AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
APP = Path.home() / "Applications" / "Delay Repay.app"
DAYS = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6}


def project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _mac() -> bool:
    return sys.platform == "darwin"


def plist(day: str, time_: str) -> dict:
    hour, minute = (int(x) for x in time_.split(":"))
    root = project_dir()
    log = root / "data" / "schedule.log"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(root / "run.sh"), "scheduled"],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": {"Weekday": DAYS[day.lower()], "Hour": hour, "Minute": minute},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "ProcessType": "Background",
    }


def install(day: str = "monday", time_: str = "10:00") -> list[str]:
    if not _mac():
        raise SystemExit("The weekly schedule and app are for macOS. Elsewhere, use cron: see README.")
    root = project_dir()
    (root / "data").mkdir(exist_ok=True)
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    with open(AGENT, "wb") as f:
        plistlib.dump(plist(day, time_), f)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(AGENT)], check=True)
    done = [f"Weekly check scheduled: every {day.capitalize()} at {time_} (log: data/schedule.log)."]
    done.append(make_app())
    return done


def uninstall() -> list[str]:
    out = []
    if _mac():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if AGENT.exists():
        AGENT.unlink()
        out.append("Removed the weekly schedule.")
    if APP.exists():
        subprocess.run(["rm", "-rf", str(APP)], check=True)
        out.append("Removed the Delay Repay app.")
    return out or ["Nothing was installed."]


def _ui_command() -> str:
    root = project_dir()
    return f"cd {shlex.quote(str(root))} && nohup ./run.sh ui >> data/ui.log 2>&1 &"


def make_app() -> str:
    """A double-clickable app in ~/Applications that opens the Delay Repay page."""
    APP.parent.mkdir(parents=True, exist_ok=True)
    script = f'do shell script "{_ui_command()}"'
    subprocess.run(["osacompile", "-o", str(APP), "-e", script], check=True)
    return f"Added the Delay Repay app: {APP} (drag it to your Dock if you like)."


def notify(count: int, total: float) -> None:
    """Ask the user to open the page when journeys need them. Gives up after an hour."""
    if not _mac() or count == 0:
        return
    what = f"{count} delayed journey{'s' if count != 1 else ''} need{'s' if count == 1 else ''} you"
    msg = f"{what} (up to £{total:.2f})."
    open_cmd = _ui_command().replace('"', '\\"')
    script = (
        f'set r to display dialog "{msg}" with title "Delay Repay" '
        f'buttons {{"Later", "Open Delay Repay"}} default button "Open Delay Repay" giving up after 3600\n'
        f'if button returned of r is "Open Delay Repay" then do shell script "{open_cmd}"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True)
