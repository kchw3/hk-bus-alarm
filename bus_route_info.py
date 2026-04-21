#!/usr/bin/env python3
"""
Usage:
    python bus_route_info.py [-route_id "<ROUTE_ID>"] [-seq <N>] [-detail]
                             [-search_schedule_from HH:MM] [-search_schedule_to HH:MM]
                             [-search_schedule_tz TZ]

Examples:
    # All stops with ETAs for the default route
    python bus_route_info.py

    # Specific route, all stops
    python bus_route_info.py -route_id "81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE"

    # Single stop
    python bus_route_info.py -seq 3

    # Single stop, full field detail
    python bus_route_info.py -seq 3 -detail

    # Single stop, highlight bus arriving in a time window (default tz +08:00)
    python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00

    # Same but using system local timezone
    python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00 -search_schedule_tz local

    # Same but with an explicit offset
    python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00 -search_schedule_tz +09:00

    # Single stop, highlight + full detail of the found schedule
    python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00 -detail

Flags that require -seq: -detail, -search_schedule_from / -search_schedule_to / -search_schedule_tz

Requires:
    pip install hk-bus-eta
"""

import sys
import re
import argparse
import unicodedata
from datetime import datetime, timezone, timedelta, date, time as dtime
from hk_bus_eta import HKEta

DEFAULT_ROUTE_ID = "81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE"


# ---------------------------------------------------------------------------
# Display-width helpers (CJK characters occupy 2 terminal columns)
# ---------------------------------------------------------------------------

def display_width(s: str) -> int:
    """Return the number of terminal columns needed to print string s."""
    w = 0
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ("W", "F") else 1
    return w


def ljust_display(s: str, width: int) -> str:
    """Left-justify s in a field of terminal width `width`, padding with spaces."""
    pad = width - display_width(s)
    return s + " " * max(pad, 0)


# ---------------------------------------------------------------------------
# ETA formatting helpers
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


def format_etas(etas: list, found_schedule: dict | None = None) -> str:
    """
    Build the ETA column string.
    - Only includes upcoming (non-negative) entries.
    - If found_schedule is provided and matches one of the entries, appends ' *'
      to that entry's formatted string.
    Returns "—" if no upcoming ETAs.
    """
    now = datetime.now(tz=timezone.utc)
    parts = []
    for entry in etas:
        eta_dt = eta_to_datetime(entry)
        if eta_dt is None:
            continue
        diff_min = int((eta_dt - now).total_seconds() / 60)
        if diff_min < 0:
            continue
        ts = eta_dt.strftime("%Y-%m-%dT%H:%M") + _offset(eta_dt)
        text = f"{ts} ({diff_min}m)"
        # Mark the found_schedule entry with an asterisk
        if found_schedule is not None and entry is found_schedule:
            text += " *"
        parts.append(text)
    return ",  ".join(parts) if parts else "—"


# ---------------------------------------------------------------------------
# Schedule search
# ---------------------------------------------------------------------------

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
# Detail (PowerShell-style list) printer
# ---------------------------------------------------------------------------

def print_detail(etas: list, found_schedule: dict | None = None) -> None:
    """
    Print every field of every ETA entry in PowerShell Format-List style.
    If found_schedule is set, only that single entry is printed (with a header
    indicating it was the matched schedule). Otherwise all entries are printed.
    """
    if not etas:
        print("  (no ETA entries returned)")
        return

    # Decide which entries to show
    if found_schedule is not None:
        entries_to_show = [found_schedule]
        label_prefix = "Matched schedule"
        total_label = 1
    else:
        entries_to_show = etas
        label_prefix = "ETA entry"
        total_label = len(etas)

    # Collect all unique field names across entries being shown
    all_keys: list[str] = []
    seen: set[str] = set()
    for entry in entries_to_show:
        for k in entry.keys():
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    col_w = max(display_width(k) for k in all_keys) + 3

    for idx, entry in enumerate(entries_to_show, start=1):
        if found_schedule is not None:
            header = f"{label_prefix}"
        else:
            header = f"{label_prefix} {idx} of {total_label}"
        print(f"\n  {header}")
        print("  " + "-" * (len(header) + 2))
        for key in all_keys:
            value = entry.get(key, "")
            label = ljust_display(key, col_w)
            print(f"  {label}: {value}")

    print()


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
        # Capture local offset as a fixed timezone so it is consistent for the run
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_route_info(
    route_id: str,
    seq_filter: int | None = None,
    detail: bool = False,
    schedule_from: dtime | None = None,
    schedule_to: dtime | None = None,
    schedule_tz: timezone | None = None,   # None → caller supplies +08:00 default
) -> None:
    print("Loading HK bus data (this may take a moment)…\n")
    hketa = HKEta()

    route = hketa.route_list.get(route_id)
    if route is None:
        print(f"Route not found: {route_id!r}")
        print("\nAvailable routes containing that bus number:")
        bus_no = route_id.split("+")[0]
        matches = [k for k in hketa.route_list if k.startswith(bus_no + "+")]
        for m in matches[:20]:
            print(f"  {m}")
        return

    # --- Origin / Destination ---
    orig = route.get("orig", {})
    dest = route.get("dest", {})

    print("=" * 60)
    print(f"Route ID : {route_id}")
    print(f"Origin   : {orig.get('en', 'N/A')}  /  {orig.get('zh', 'N/A')}")
    print(f"Dest     : {dest.get('en', 'N/A')}  /  {dest.get('zh', 'N/A')}")
    print("=" * 60)

    # --- Stops ---
    stop_list = route.get("stops", {})
    if not stop_list:
        print("No stops found in route data.")
        return

    # Flatten: {"kmb": [...], "ctb": [...], ...} → list of (co, stop_id)
    all_stops = []
    for co, ids in stop_list.items():
        if isinstance(ids, list):
            for stop_id in ids:
                all_stops.append((co, stop_id))

    total = len(all_stops)

    # --- Validate -seq ---
    if seq_filter is not None:
        if seq_filter < 1 or seq_filter > total:
            print(f"\nError: -seq {seq_filter} is out of range. "
                  f"Valid range is 1–{total}.")
            sys.exit(1)
        stops_to_print = [(seq_filter, all_stops[seq_filter - 1])]
        print(f"\nFetching ETA for stop {seq_filter} of {total}…\n")
    else:
        stops_to_print = [(seq, stop) for seq, stop in enumerate(all_stops, start=1)]
        print(f"\nFetching ETAs for {total} stops…\n")

    # --- Build search window datetimes (today's date + HH:MM + resolved tz) ---
    search_from_dt: datetime | None = None
    search_to_dt: datetime | None = None
    if schedule_from is not None and schedule_to is not None:
        tz = schedule_tz if schedule_tz is not None else timezone(timedelta(hours=8))
        today = date.today()
        search_from_dt = datetime.combine(today, schedule_from, tzinfo=tz)
        search_to_dt   = datetime.combine(today, schedule_to,   tzinfo=tz)

    # --- Collect row data ---
    headers = ("Seq", "Co", "Stop ID", "English", "中文")
    rows = []
    raw_etas_per_row: list[list] = []
    found_schedules: list[dict | None] = []   # one entry per row

    for seq, (co, stop_id) in stops_to_print:
        stop_data = hketa.stop_list.get(stop_id, {})
        name_en = stop_data.get("name", {}).get("en", "—")
        name_zh = stop_data.get("name", {}).get("zh", "—")
        try:
            etas = hketa.getEtas(route_id=route_id, seq=seq - 1, language="en")
        except Exception:
            etas = []

        # Determine found_schedule for this row
        found = None
        if search_from_dt is not None and search_to_dt is not None:
            found = find_schedule(etas, search_from_dt, search_to_dt)

        eta_str = format_etas(etas, found_schedule=found)
        rows.append((str(seq), co, stop_id, name_en, name_zh, eta_str))
        raw_etas_per_row.append(etas)
        found_schedules.append(found)

    # --- Column display-widths: max(header, data) + 3 ---
    n_cols = len(headers)
    widths = []
    for i, h in enumerate(headers):
        max_data = max((display_width(r[i]) for r in rows), default=0)
        widths.append(max(display_width(h), max_data) + 3)

    col_sep = "  "

    def fmt_row(cells):
        fixed = col_sep.join(ljust_display(cells[i], widths[i]) for i in range(n_cols))
        return "  " + fixed + col_sep + cells[n_cols]

    # --- Print table header + separator ---
    header_line = fmt_row(headers + ("ETA",))
    sep_width = sum(widths) + len(col_sep) * (n_cols - 1)
    print(header_line)
    print("  " + "-" * sep_width)

    schedule_search_active = search_from_dt is not None and search_to_dt is not None

    for i, row in enumerate(rows):
        print(fmt_row(row))
        if detail:
            found = found_schedules[i]
            if not schedule_search_active:
                # No schedule search requested — print all ETA entries as usual
                print_detail(raw_etas_per_row[i])
            elif found is not None:
                # Schedule search active and a match was found — print only that entry
                print_detail(raw_etas_per_row[i], found_schedule=found)
            else:
                # Schedule search active but nothing fell in the window
                print(
                    f"\n  No bus schedule found between "
                    f"{search_from_dt.strftime('%H:%M')} and "
                    f"{search_to_dt.strftime('%H:%M')} "
                    f"(tz {_offset(search_from_dt)}).\n"
                )

    if not detail:
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Print HK bus route stops and ETAs.",
        usage='%(prog)s [-route_id ROUTE_ID] [-seq N] [-detail] '
              '[-search_schedule_from HH:MM] [-search_schedule_to HH:MM]',
    )
    parser.add_argument(
        "-route_id",
        default=DEFAULT_ROUTE_ID,
        metavar="ROUTE_ID",
        help=f'Route ID (default: "{DEFAULT_ROUTE_ID}")',
    )
    parser.add_argument(
        "-seq", type=int, default=None, metavar="N",
        help="Print only stop N (1-based). Omit to print all stops.",
    )
    parser.add_argument(
        "-detail", action="store_true", default=False,
        help="Print all raw ETA fields (PowerShell-style list). Requires -seq.",
    )
    parser.add_argument(
        "-search_schedule_from", type=parse_hhmm, default=None, metavar="HH:MM",
        help="Start of time window to search for a bus schedule (e.g. 14:00). Requires -seq.",
    )
    parser.add_argument(
        "-search_schedule_to", type=parse_hhmm, default=None, metavar="HH:MM",
        help="End of time window to search for a bus schedule (e.g. 15:00). Requires -seq.",
    )

    parser.add_argument(
        "-search_schedule_tz", type=parse_tz,
        default=None,   # resolved to +08:00 inside print_route_info when None
        metavar="TZ",
        help=(
            "Timezone for -search_schedule_from / -search_schedule_to. "
            "Accepted values: 'local' (system local tz) or a fixed offset like "
            "'+08:00' / '-05:00'. Default: +08:00."
        ),
    )

    args = parser.parse_args()

    # --- Cross-parameter validation ---
    if args.detail and args.seq is None:
        parser.error("-detail requires -seq.")

    has_from = args.search_schedule_from is not None
    has_to   = args.search_schedule_to   is not None

    if has_from != has_to:
        parser.error("-search_schedule_from and -search_schedule_to must both be specified together.")

    if (has_from or has_to) and args.seq is None:
        parser.error("-search_schedule_from / -search_schedule_to require -seq.")

    if has_from and has_to:
        if args.search_schedule_to <= args.search_schedule_from:
            parser.error(
                f"-search_schedule_to ({args.search_schedule_to.strftime('%H:%M')}) "
                f"must be later than -search_schedule_from "
                f"({args.search_schedule_from.strftime('%H:%M')})."
            )

    print_route_info(
        route_id=args.route_id,
        seq_filter=args.seq,
        detail=args.detail,
        schedule_from=args.search_schedule_from,
        schedule_to=args.search_schedule_to,
        schedule_tz=args.search_schedule_tz,
    )