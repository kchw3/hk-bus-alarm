#!/usr/bin/env python3
"""
Usage:
    python add_bus_schedule_to_calendar.py -seq N
        -search_schedule_from HH:MM -search_schedule_to HH:MM
        (-add_event | -add_event_debug)
        [-route_id ROUTE_ID]
        [-search_schedule_tz TZ]
        [-calendar_id ID]
        [-credentials_file PATH]
        [-token_file PATH]
        [-duration_minutes N]

Examples:
    # Dry-run: print the event details without calling the Calendar API
    python add_bus_schedule_to_calendar.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -add_event_debug

    # Create the event in the primary calendar
    python add_bus_schedule_to_calendar.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -add_event

    # Create the event in a specific calendar
    python add_bus_schedule_to_calendar.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -calendar_id "abc123@group.calendar.google.com" \\
        -add_event

    # Use a custom credentials file and 60-minute event duration
    python add_bus_schedule_to_calendar.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -credentials_file ~/my_creds.json -duration_minutes 60 \\
        -add_event_debug

Requires:
    pip install hk-bus-eta google-api-python-client google-auth-httplib2 google-auth-oauthlib
"""

import sys
import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta, time as dtime

from hk_bus_eta import HKEta

from hk_bus_common import (
    DEFAULT_ROUTE_ID,
    RouteQuery,
    ScheduleWindow,
    _offset,
    eta_to_datetime,
    find_schedule,
    format_eta_entry,
    parse_hhmm,
    parse_tz,
)
from google_calendar_lib import get_calendar_service, create_calendar_event

EVENT_SUMMARY = "Bus schedule"
DEFAULT_CALENDAR_ID = "primary"
DEFAULT_CREDENTIALS_FILE = "credentials.json"
DEFAULT_TOKEN_FILE = "token.json"
DEFAULT_DURATION_MINUTES = 30


@dataclass
class CalendarConfig:
    calendar_id: str
    credentials_file: str
    token_file: str
    duration_minutes: int


def build_event_description(
    route_id: str,
    route: dict,
    seq: int,
    total: int,
    co: str,
    stop_id: str,
    name_en: str,
    name_zh: str,
    etas: list,
    found: dict | None,
    from_dt: datetime,
    to_dt: datetime,
) -> str:
    """Build the calendar event description from all route/stop/ETA information."""
    orig = route.get("orig", {})
    dest = route.get("dest", {})
    now = datetime.now(tz=timezone.utc)

    lines = [
        "=" * 50,
        f"Route ID : {route_id}",
        f"Origin   : {orig.get('en', 'N/A')}  /  {orig.get('zh', 'N/A')}",
        f"Dest     : {dest.get('en', 'N/A')}  /  {dest.get('zh', 'N/A')}",
        "=" * 50,
        "",
        f"Stop     : {seq} of {total}",
        f"Operator : {co}",
        f"Stop ID  : {stop_id}",
        f"English  : {name_en}",
        f"Chinese  : {name_zh}",
        "",
        f"Search window : {from_dt.strftime('%H:%M')}–{to_dt.strftime('%H:%M')}  (tz {_offset(from_dt)})",
    ]

    if found is not None:
        lines.append(f"Matched       : {format_eta_entry(found)}")
    else:
        lines.append("Matched       : (no schedule found — event start set to script run time)")
    lines.append("")

    # All upcoming ETAs, marking the matched one
    upcoming = [
        (entry, eta_to_datetime(entry))
        for entry in etas
        if eta_to_datetime(entry) is not None
    ]
    upcoming = [
        (entry, dt) for entry, dt in upcoming
        if int((dt - now).total_seconds() / 60) >= 0
    ]

    if upcoming:
        lines.append("All upcoming ETAs:")
        for entry, eta_dt in upcoming:
            diff_min = int((eta_dt - now).total_seconds() / 60)
            ts = eta_dt.strftime("%Y-%m-%dT%H:%M") + _offset(eta_dt)
            marker = "  *" if entry is found else ""
            lines.append(f"  {ts} ({diff_min}m){marker}")
        lines.append("")
    else:
        lines.append("All upcoming ETAs: (none)")
        lines.append("")

    # Raw fields of the matched schedule entry
    if found is not None:
        lines.append("Matched schedule (raw fields):")
        for key, value in found.items():
            lines.append(f"  {key} : {value}")

    return "\n".join(lines)


def run(
    query: RouteQuery,
    window: ScheduleWindow,
    calendar: CalendarConfig,
    *,
    debug: bool,
) -> None:
    """Fetch the latest bus ETA within the given window and add it as a Google Calendar event."""
    print("Loading HK bus data (this may take a moment)…\n")
    hketa = HKEta()

    route = hketa.route_list.get(query.route_id)
    if route is None:
        print(f"Route not found: {query.route_id!r}")
        bus_no = query.route_id.split("+")[0]
        matches = [k for k in hketa.route_list if k.startswith(bus_no + "+")]
        if matches:
            print("Available routes containing that bus number:")
            for m in matches[:20]:
                print(f"  {m}")
        sys.exit(1)

    stop_list = route.get("stops", {})
    all_stops = [
        (co, stop_id)
        for co, ids in stop_list.items()
        if isinstance(ids, list)
        for stop_id in ids
    ]
    total = len(all_stops)

    if query.seq < 1 or query.seq > total:
        print(f"Error: -seq {query.seq} is out of range. Valid range is 1–{total}.")
        sys.exit(1)

    co, stop_id = all_stops[query.seq - 1]
    stop_data = hketa.stop_list.get(stop_id, {})
    name_en = stop_data.get("name", {}).get("en", "—")
    name_zh = stop_data.get("name", {}).get("zh", "—")

    print(f"Stop {query.seq}/{total}  [{co}]  {stop_id}  {name_en} / {name_zh}\n")

    try:
        etas = hketa.getEtas(route_id=query.route_id, seq=query.seq - 1, language="en")
    except Exception as exc:
        print(f"Error fetching ETAs: {exc}")
        sys.exit(1)

    tz = window.schedule_tz if window.schedule_tz is not None else timezone(timedelta(hours=8))
    today = date.today()
    from_dt = datetime.combine(today, window.schedule_from, tzinfo=tz)
    to_dt   = datetime.combine(today, window.schedule_to,   tzinfo=tz)

    found = find_schedule(etas, from_dt, to_dt)

    if found is not None:
        found_dt = eta_to_datetime(found)
        print(f"Found schedule: {format_eta_entry(found)}\n")
    else:
        found_dt = datetime.now(tz=timezone.utc)
        print(
            f"No bus schedule found between "
            f"{from_dt.strftime('%H:%M')} and {to_dt.strftime('%H:%M')} "
            f"(tz {_offset(from_dt)}). "
            f"Event start time set to now: {found_dt.strftime('%Y-%m-%dT%H:%M')} {_offset(found_dt)}\n"
        )

    description = build_event_description(
        route_id=query.route_id,
        route=route,
        seq=query.seq,
        total=total,
        co=co,
        stop_id=stop_id,
        name_en=name_en,
        name_zh=name_zh,
        etas=etas,
        found=found,
        from_dt=from_dt,
        to_dt=to_dt,
    )

    if debug:
        print("Event details (not submitted to Google Calendar):")
        print(f"  Title    : {EVENT_SUMMARY}")
        print(f"  Start    : {found_dt.isoformat()}")
        print(f"  End      : {(found_dt + timedelta(minutes=calendar.duration_minutes)).isoformat()}")
        print(f"  Calendar : {calendar.calendar_id}")
        print()
        print("Description:")
        print("-" * 50)
        print(description)
        print("-" * 50)
    else:
        print(f"Authenticating with Google Calendar…")
        service = get_calendar_service(calendar.credentials_file, calendar.token_file)
        event = create_calendar_event(
            service,
            calendar_id=calendar.calendar_id,
            summary=EVENT_SUMMARY,
            start_dt=found_dt,
            duration_minutes=calendar.duration_minutes,
            description=description,
        )
        print(f"Event created: {event.get('htmlLink')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Find a HK bus ETA and create a Google Calendar event for it.",
        usage=(
            "%(prog)s -seq N "
            "-search_schedule_from HH:MM -search_schedule_to HH:MM "
            "(-add_event | -add_event_debug) "
            "[-route_id ROUTE_ID] [-search_schedule_tz TZ] "
            "[-calendar_id ID] [-credentials_file PATH] [-token_file PATH] "
            "[-duration_minutes N]"
        ),
    )

    _ = parser.add_argument(
        "-route_id",
        default=DEFAULT_ROUTE_ID,
        metavar="ROUTE_ID",
        help=f'Route ID (default: "{DEFAULT_ROUTE_ID}")',
    )
    _ = parser.add_argument(
        "-seq", type=int, required=True, metavar="N",
        help="Stop number (1-based) to query.",
    )
    _ = parser.add_argument(
        "-search_schedule_from", type=parse_hhmm, required=True, metavar="HH:MM",
        help="Start of time window to search for a bus schedule (e.g. 14:00).",
    )
    _ = parser.add_argument(
        "-search_schedule_to", type=parse_hhmm, required=True, metavar="HH:MM",
        help="End of time window (e.g. 15:00). Must be later than -search_schedule_from.",
    )
    _ = parser.add_argument(
        "-search_schedule_tz", type=parse_tz, default=None, metavar="TZ",
        help=(
            "Timezone for the search window. "
            "Accepted: 'local' or a fixed offset like '+08:00' / '-05:00'. "
            "Default: +08:00."
        ),
    )
    _ = parser.add_argument(
        "-calendar_id",
        default=DEFAULT_CALENDAR_ID,
        metavar="ID",
        help=(
            f"Target Google Calendar ID (default: \"{DEFAULT_CALENDAR_ID}\"). "
            "Use 'primary' for your default calendar, or a calendar's "
            "email-style ID (e.g. abc123@group.calendar.google.com)."
        ),
    )
    _ = parser.add_argument(
        "-credentials_file",
        default=DEFAULT_CREDENTIALS_FILE,
        metavar="PATH",
        help=f"Path to OAuth 2.0 client-secrets JSON (default: {DEFAULT_CREDENTIALS_FILE}).",
    )
    _ = parser.add_argument(
        "-token_file",
        default=DEFAULT_TOKEN_FILE,
        metavar="PATH",
        help=f"Path to cached OAuth token file (default: {DEFAULT_TOKEN_FILE}).",
    )
    _ = parser.add_argument(
        "-duration_minutes", type=int, default=DEFAULT_DURATION_MINUTES, metavar="N",
        help=f"Calendar event duration in minutes (default: {DEFAULT_DURATION_MINUTES}).",
    )

    event_mode = parser.add_mutually_exclusive_group(required=True)
    _ = event_mode.add_argument(
        "-add_event", dest="add_event", action="store_true",
        help="Create the event in Google Calendar.",
    )
    _ = event_mode.add_argument(
        "-add_event_debug", dest="add_event_debug", action="store_true",
        help="Print the event details to stdout without calling the Calendar API.",
    )

    args = parser.parse_args()

    if args.search_schedule_to <= args.search_schedule_from:
        parser.error(
            f"-search_schedule_to ({args.search_schedule_to.strftime('%H:%M')}) "
            f"must be later than -search_schedule_from "
            f"({args.search_schedule_from.strftime('%H:%M')})."
        )

    run(
        query=RouteQuery(route_id=args.route_id, seq=args.seq),
        window=ScheduleWindow(
            schedule_from=args.search_schedule_from,
            schedule_to=args.search_schedule_to,
            schedule_tz=args.search_schedule_tz,
        ),
        calendar=CalendarConfig(
            calendar_id=args.calendar_id,
            credentials_file=args.credentials_file,
            token_file=args.token_file,
            duration_minutes=args.duration_minutes,
        ),
        debug=args.add_event_debug,
    )
