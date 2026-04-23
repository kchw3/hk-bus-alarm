"""
Library for setting Android alarms via the Activity Manager (am) command.
Intended to be called from Termux on an Android device.
"""

import re
import shlex
import subprocess
from datetime import datetime


DEFAULT_ALARM_LABEL = "Bus schedule"

# Tried in order; first one that responds wins.
_ALARM_URIS = [
    "content://com.android.alarmclock/alarm",
    "content://com.android.deskclock/alarm",
]
# AOSP uses "label"; some older builds use "message".
_LABEL_COLS = ("label", "message")


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


def _content(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["content", *args], capture_output=True, text=True, check=False,
    )


def _find_alarms(label: str) -> tuple[str, str, list[str]]:
    """
    Query the alarm content provider for alarms whose label matches.

    Returns (uri, label_column, list_of_ids).
    - uri == ""               : content provider unreachable
    - uri != "", ids == []    : provider reachable, no matching alarm
    - uri != "", ids != []    : matching alarm(s) found
    """
    for uri in _ALARM_URIS:
        for col in _LABEL_COLS:
            try:
                r = _content(
                    "query", "--uri", uri,
                    "--projection", "_id",
                    "--where", f"{col}=?", "--arg", label,
                )
            except (FileNotFoundError, OSError):
                return "", "", []
            if r.returncode == 0:
                ids = re.findall(r'\b_id=(\d+)', r.stdout)
                return uri, col, ids
    return "", "", []


def _update_alarms(uri: str, ids: list[str], hour: int, minute: int) -> bool:
    """Update hour/minutes/enabled for each alarm by row ID. Returns True if all succeeded."""
    for alarm_id in ids:
        r = _content(
            "update", "--uri", f"{uri}/{alarm_id}",
            "--bind", f"hour:i:{hour}",
            "--bind", f"minutes:i:{minute}",
            "--bind", "enabled:i:1",
        )
        if r.returncode != 0:
            return False
    return True


def _delete_alarms(uri: str, col: str, label: str) -> None:
    """Delete all alarms matching label from the content provider."""
    _content("delete", "--uri", uri, "--where", f"{col}=?", "--arg", label)


def set_android_alarm(dt: datetime, label: str, *, debug: bool = False) -> None:
    """
    Set an Android alarm for the time in `dt`, avoiding duplicates.

    1. Queries the alarm content provider for existing alarms with `label`.
    2. If found, tries to update them in place (hour, minutes, enabled=1).
    3. If update fails, deletes the existing alarms then creates a new one via am.
    4. If no existing alarm or provider is unreachable, creates via am directly.

    debug=True  → print intended actions and the am command without executing.
    debug=False → execute on device (requires Termux / Android).
    """
    cmd = build_am_alarm_command(dt.hour, dt.minute, label)
    hhmm = dt.strftime('%H:%M')

    if debug:
        print(f"Would update or replace existing alarm(s) with label {label!r}.")
        print("am command if recreating (not executed):")
        print("  " + shlex.join(cmd))
        return

    uri, col, ids = _find_alarms(label)

    if ids:
        if _update_alarms(uri, ids, dt.hour, dt.minute):
            print(f"Updated existing alarm to {hhmm} — label: {label!r}")
            return
        print(f"In-place update failed; deleting existing alarm(s) with label {label!r}.")
        _delete_alarms(uri, col, label)
    elif not uri:
        print("Note: alarm content provider unreachable; duplicate alarms may occur.")

    print(f"Setting alarm for {hhmm} — label: {label!r}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"am command exited with code {result.returncode}")
    print("Alarm set successfully.")
