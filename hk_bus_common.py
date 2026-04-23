"""
Shared utilities for HK bus ETA scripts.
"""

import re
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, time as dtime


DEFAULT_ROUTE_ID = "81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE"


@dataclass
class RouteQuery:
    route_id: str
    seq: int


@dataclass
class ScheduleWindow:
    schedule_from: dtime
    schedule_to: dtime
    schedule_tz: timezone | None


# ---------------------------------------------------------------------------
# ETA helpers
# ---------------------------------------------------------------------------

def _offset(dt: datetime) -> str:
    """Return the UTC offset string e.g. '+08:00' or '' if naive."""
    utcoff = dt.utcoffset()
    if utcoff is None:
        return ""
    total_seconds = int(utcoff.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    h, m = divmod(total_seconds // 60, 60)
    return f"{sign}{h:02d}:{m:02d}"


def eta_to_datetime(entry: dict) -> datetime | None:
    """Parse the 'eta' field of an ETA entry dict; return None on failure."""
    raw = entry.get("eta")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def format_eta_entry(entry: dict) -> str:
    """Format a single ETA entry as '2026-04-21T14:32+08:00 (15m)'."""
    now = datetime.now(tz=timezone.utc)
    eta_dt = eta_to_datetime(entry)
    if eta_dt is None:
        return "—"
    diff_min = int((eta_dt - now).total_seconds() / 60)
    ts = eta_dt.strftime("%Y-%m-%dT%H:%M") + _offset(eta_dt)
    return f"{ts} ({diff_min}m)"


def find_schedule(etas: list, from_dt: datetime, to_dt: datetime) -> dict | None:
    """
    Return the ETA entry with the largest (latest) timestamp whose eta falls
    within [from_dt, to_dt], or None if no such entry exists.
    """
    best_entry = None
    best_dt = None
    for entry in etas:
        eta_dt = eta_to_datetime(entry)
        if eta_dt is None:
            continue
        if from_dt <= eta_dt <= to_dt:
            if best_dt is None or eta_dt > best_dt:
                best_dt = eta_dt
                best_entry = entry
    return best_entry


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------

def parse_hhmm(value: str) -> dtime:
    """Parse a 'HH:MM' string into a datetime.time; raise ArgumentTypeError on failure."""
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid time format {value!r}. Expected HH:MM (e.g. 14:32)."
        )


def parse_tz(value: str) -> timezone:
    """
    Parse a timezone specifier into a datetime.timezone:
      "local"   → the system's local UTC offset (captured at parse time)
      "+HH:MM"  → fixed positive offset  (e.g. "+08:00", "+09:00")
      "-HH:MM"  → fixed negative offset  (e.g. "-05:00")
    Raises argparse.ArgumentTypeError on invalid input.
    """
    if value == "local":
        local_offset = datetime.now(tz=timezone.utc).astimezone().utcoffset()
        return timezone(local_offset)

    m = re.fullmatch(r'([+-])(\d{2}):(\d{2})', value)
    if not m:
        raise argparse.ArgumentTypeError(
            f"Invalid timezone {value!r}. "
            "Expected 'local' or an offset like '+08:00' / '-05:00'."
        )
    sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    if hh > 14 or mm > 59:
        raise argparse.ArgumentTypeError(
            f"Invalid UTC offset {value!r}: hours must be ≤ 14, minutes ≤ 59."
        )
    delta = timedelta(hours=hh, minutes=mm)
    return timezone(delta if sign == "+" else -delta)
