"""
Library for setting Android alarms via the Activity Manager (am) command.
Intended to be called from Termux on an Android device.
"""

import shlex
import subprocess
from datetime import datetime


DEFAULT_ALARM_LABEL = "Bus schedule"


def build_am_alarm_command(hour: int, minute: int, label: str) -> list[str]:
    """Return the `am start` argument list for setting a clock alarm."""
    return [
        "am", "start",
        "-a", "android.intent.action.SET_ALARM",
        "--ei", "android.intent.extra.alarm.HOUR", str(hour),
        "--ei", "android.intent.extra.alarm.MINUTES", str(minute),
        "--es", "android.intent.extra.alarm.MESSAGE", label,
        "--ez", "android.intent.extra.alarm.SKIP_UI", "true",
    ]


def set_android_alarm(dt: datetime, label: str, *, debug: bool = False) -> None:
    """
    Set an Android alarm for the time in `dt`.

    debug=True  → print the command to stdout instead of running it.
    debug=False → execute via subprocess.run (requires Termux / Android).
    """
    cmd = build_am_alarm_command(dt.hour, dt.minute, label)
    if debug:
        print("Command (not executed):")
        print("  " + shlex.join(cmd))
    else:
        print(f"Setting alarm for {dt.strftime('%H:%M')} — label: {label!r}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"am command exited with code {result.returncode}"
            )
        print("Alarm set successfully.")
