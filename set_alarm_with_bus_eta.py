#!/usr/bin/env python3
"""
Usage:
    python set_alarm_with_bus_eta.py -seq N
        -search_schedule_from HH:MM -search_schedule_to HH:MM
        (-add_alarm | -add_alarm_debug | -add_alarm_ha)
        [-route_id ROUTE_ID]
        [-search_schedule_tz TZ]
        [-alarm_label LABEL]
        [-alarm_minutes_before_schedule N]
        [-log_file PATH]
        [-log_url URL] [-log_token TOKEN]

Examples:
    # Set an alarm for the latest bus between 14:00–15:00 at stop 3
    python set_alarm_with_bus_eta.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -add_alarm

    # Dry-run: print the am command without executing it
    python set_alarm_with_bus_eta.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -add_alarm_debug

    # Custom alarm label
    python set_alarm_with_bus_eta.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -add_alarm -alarm_label "Take bus 81"

    # Set alarm 10 minutes before the found bus schedule
    python set_alarm_with_bus_eta.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -alarm_minutes_before_schedule 10 -add_alarm

    # Home Assistant mode: prints FOUND:HH:MM or NOT_FOUND:HH:MM only
    python set_alarm_with_bus_eta.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -add_alarm_ha

    # Also upload each run to the chart ingest endpoint (token from $BUS_LOG_TOKEN)
    python set_alarm_with_bus_eta.py -seq 3 \\
        -search_schedule_from 14:00 -search_schedule_to 15:00 \\
        -log_url https://<worker>.workers.dev/api/ingest \\
        -add_alarm

    # Different timezone
    python set_alarm_with_bus_eta.py -seq 3 \\
        -search_schedule_from 15:00 -search_schedule_to 16:00 \\
        -search_schedule_tz +09:00 -add_alarm_debug

Requires:
    pip install hk-bus-eta
    Android device with Termux (for -add_alarm)
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
from bus_alarm_lib import DEFAULT_ALARM_LABEL, set_android_alarm
from bus_log_lib import LOG_TOKEN_ENV, LogRecord, post_record, write_log_csv


_MIN_ALARM_LEAD_MINUTES = 2


def _log_run(
    record: LogRecord,
    log_file: str | None,
    log_url: str | None,
    log_token: str | None,
) -> None:
    """Record one run locally and upload it.

    Logging is strictly secondary to setting the alarm, so nothing here raises:
    a full disk, an unwritable path or an unreachable endpoint only produces a
    warning on stderr. The local CSV is written first, so a failed upload leaves
    a copy that `backfill_log.py` can replay later (ingest upserts, so replaying
    rows that did make it through is harmless).
    """
    stored_locally = False
    if log_file is not None:
        try:
            write_log_csv(log_file, record)
            stored_locally = True
        except OSError as exc:
            print(f"Warning: could not write log file {log_file}: {exc}", file=sys.stderr)

    if log_url is None:
        return

    if post_record(log_url, log_token, record):
        return

    if stored_locally:
        print(
            f"         The record is still in {log_file}; upload it later with:\n"
            f"           python backfill_log.py {log_file} -log_url {log_url}",
            file=sys.stderr,
        )
    else:
        print(
            "         The record was not stored locally either, so it cannot be "
            "replayed. Add -log_file PATH to keep a replayable copy.",
            file=sys.stderr,
        )


@dataclass
class AlarmConfig:
    alarm_label: str
    alarm_default_time: dtime | None
    alarm_minutes_before: int


def run(
    query: RouteQuery,
    window: ScheduleWindow,
    alarm: AlarmConfig,
    *,
    mode: str,
    log_file: str | None = None,
    log_url: str | None = None,
    log_token: str | None = None,
) -> None:
    """Fetch the latest bus ETA within the given window and set an Android alarm accordingly.

    mode: "execute" — set the alarm on device via am.
          "debug"   — print the am command without executing.
          "ha"      — print FOUND:HH:MM or NOT_FOUND:HH:MM only (for Home Assistant).
    """
    ha = (mode == "ha")

    if not ha:
        print("Loading HK bus data (this may take a moment)…\n")
    hketa = HKEta()

    route = hketa.route_list.get(query.route_id)
    if route is None:
        if not ha:
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
        if not ha:
            print(f"Error: -seq {query.seq} is out of range. Valid range is 1–{total}.")
        sys.exit(1)

    co, stop_id = all_stops[query.seq - 1]
    stop_data = hketa.stop_list.get(stop_id, {})
    name_en = stop_data.get("name", {}).get("en", "—")
    name_zh = stop_data.get("name", {}).get("zh", "—")

    if not ha:
        print(f"Stop {query.seq}/{total}  [{co}]  {stop_id}  {name_en} / {name_zh}\n")

    try:
        etas = hketa.getEtas(route_id=query.route_id, seq=query.seq - 1, language="en")
    except Exception as exc:
        if not ha:
            print(f"Error fetching ETAs: {exc}")
        sys.exit(1)

    tz = window.schedule_tz
    today = date.today()
    from_dt = datetime.combine(today, window.schedule_from, tzinfo=tz)
    to_dt   = datetime.combine(today, window.schedule_to,   tzinfo=tz)

    found = find_schedule(etas, from_dt, to_dt)

    # --- Determine initial alarm_dt and track log fields ---
    schedule_detail = ""
    schedule_eta_iso = ""
    alarm_reason = ""

    if found is not None:
        found_dt = eta_to_datetime(found)
        schedule_detail = format_eta_entry(found)
        schedule_eta_iso = found_dt.isoformat(timespec="seconds")
        if not ha:
            print(f"Found schedule: {schedule_detail}")
        alarm_dt = found_dt - timedelta(minutes=alarm.alarm_minutes_before)
        if alarm.alarm_minutes_before:
            alarm_reason = f"{alarm.alarm_minutes_before}m before schedule"
            if not ha:
                print(
                    f"Alarm adjusted {alarm.alarm_minutes_before}m before schedule: "
                    f"{alarm_dt.strftime('%H:%M')} {_offset(alarm_dt)}"
                )
        else:
            alarm_reason = "at schedule time"
    else:
        if alarm.alarm_default_time is None:
            alarm_dt = datetime.now(tz=tz)
            alarm_reason = "no schedule; set to now+2m"
            if not ha:
                print(
                    f"No bus schedule found between "
                    f"{from_dt.strftime('%H:%M')} and {to_dt.strftime('%H:%M')} "
                    f"(tz {_offset(from_dt)}). "
                    f"Setting alarm to {_MIN_ALARM_LEAD_MINUTES}m from now."
                )
        else:
            alarm_dt = datetime.combine(today, alarm.alarm_default_time, tzinfo=tz)
            alarm_reason = "no schedule; fallback default"
            if not ha:
                print(
                    f"No bus schedule found between "
                    f"{from_dt.strftime('%H:%M')} and {to_dt.strftime('%H:%M')} "
                    f"(tz {_offset(from_dt)}). "
                    f"Using default alarm time: {alarm.alarm_default_time.strftime('%H:%M')} {_offset(alarm_dt)}"
                )

    # --- Clamp: alarm must be at least _MIN_ALARM_LEAD_MINUTES from now ---
    min_alarm_dt = datetime.now(tz=tz) + timedelta(minutes=_MIN_ALARM_LEAD_MINUTES)
    if alarm_dt < min_alarm_dt:
        if not ha:
            print(
                f"Alarm time {alarm_dt.strftime('%H:%M')} {_offset(alarm_dt)} is less than "
                f"{_MIN_ALARM_LEAD_MINUTES}m from now; "
                f"setting to {min_alarm_dt.strftime('%H:%M')} {_offset(min_alarm_dt)} instead."
            )
        # Only append clamp suffix when the base reason doesn't already imply it
        if alarm_reason != "no schedule; set to now+2m":
            alarm_reason += "; clamped to now+2m"
        alarm_dt = min_alarm_dt

    # --- Write / upload log row ---
    if log_file is not None or log_url is not None:
        _log_run(
            LogRecord(
                timestamp=datetime.now(tz=tz).isoformat(timespec="seconds"),
                route_id=query.route_id,
                bus_schedule=schedule_detail,
                alarm_time=alarm_dt.strftime("%H:%M"),
                reason=alarm_reason,
                eta_iso=schedule_eta_iso,
            ),
            log_file,
            log_url,
            log_token,
        )

    if ha:
        status = "FOUND" if found is not None else "NOT_FOUND"
        print(f"{status}:{alarm_dt.strftime('%H:%M')}")
        return

    print()
    set_android_alarm(alarm_dt, alarm.alarm_label, debug=(mode == "debug"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Find a HK bus ETA within a time window and set an Android alarm.",
        usage=(
            "%(prog)s -seq N "
            "-search_schedule_from HH:MM -search_schedule_to HH:MM "
            "(-add_alarm | -add_alarm_debug | -add_alarm_ha) "
            "[-route_id ROUTE_ID] [-search_schedule_tz TZ] [-alarm_label LABEL] "
            "[-alarm_default_time HH:MM] [-alarm_minutes_before_schedule N] "
            "[-log_file PATH] [-log_url URL] [-log_token TOKEN]"
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
        help="Start of time window (e.g. 14:00).",
    )
    _ = parser.add_argument(
        "-search_schedule_to", type=parse_hhmm, required=True, metavar="HH:MM",
        help="End of time window (e.g. 15:00).",
    )
    _ = parser.add_argument(
        "-search_schedule_tz", type=parse_tz, default=timezone(timedelta(hours=8)), metavar="TZ",
        help=(
            "Timezone for the search window. "
            "Accepted: 'local' or a fixed offset like '+08:00' / '-05:00'. "
            "Default: +08:00."
        ),
    )
    _ = parser.add_argument(
        "-alarm_label",
        default=DEFAULT_ALARM_LABEL,
        metavar="LABEL",
        help=f'Alarm label shown on the Android clock app (default: "{DEFAULT_ALARM_LABEL}").',
    )
    _ = parser.add_argument(
        "-alarm_default_time", type=parse_hhmm, default=None, metavar="HH:MM",
        help=(
            "Fallback alarm time (HH:MM) used when no bus schedule is found in the search window. "
            "Uses the same timezone as -search_schedule_tz (default +08:00)."
        ),
    )
    _ = parser.add_argument(
        "-alarm_minutes_before_schedule", type=int, default=0, metavar="N",
        help="Set the alarm N minutes before the found bus schedule (default: 0).",
    )

    _ = parser.add_argument(
        "-log_file", default=None, metavar="PATH",
        help=(
            "Path to a CSV log file. Each run appends one row "
            "(timestamp, route_id, bus_schedule, alarm_time, reason). "
            "Header is written automatically when the file is new or empty. "
            "Logging is disabled if this argument is omitted."
        ),
    )
    _ = parser.add_argument(
        "-log_url", default=None, metavar="URL",
        help=(
            "Ingest endpoint of the schedule chart (e.g. "
            "https://<worker>.workers.dev/api/ingest). Each run uploads one record. "
            "Independent of -log_file; upload failures only warn and never stop the alarm."
        ),
    )
    _ = parser.add_argument(
        "-log_token", default=None, metavar="TOKEN",
        help=(
            f"Bearer token for -log_url. Defaults to the {LOG_TOKEN_ENV} environment "
            "variable, which keeps the token out of the process list."
        ),
    )

    alarm_mode = parser.add_mutually_exclusive_group(required=True)
    _ = alarm_mode.add_argument(
        "-add_alarm", dest="add_alarm", action="store_true",
        help="Execute the am command to set the Android alarm.",
    )
    _ = alarm_mode.add_argument(
        "-add_alarm_debug", dest="add_alarm_debug", action="store_true",
        help="Print the am command to stdout without executing it.",
    )
    _ = alarm_mode.add_argument(
        "-add_alarm_ha", dest="add_alarm_ha", action="store_true",
        help=(
            "Home Assistant mode: print FOUND:HH:MM if a schedule was found, "
            "or NOT_FOUND:HH:MM using the fallback alarm time. No other output."
        ),
    )

    args = parser.parse_args()

    if args.search_schedule_to <= args.search_schedule_from:
        parser.error(
            f"-search_schedule_to ({args.search_schedule_to.strftime('%H:%M')}) "
            f"must be later than -search_schedule_from "
            f"({args.search_schedule_from.strftime('%H:%M')})."
        )

    if args.add_alarm_ha:
        mode = "ha"
    elif args.add_alarm_debug:
        mode = "debug"
    else:
        mode = "execute"

    run(
        query=RouteQuery(route_id=args.route_id, seq=args.seq),
        window=ScheduleWindow(
            schedule_from=args.search_schedule_from,
            schedule_to=args.search_schedule_to,
            schedule_tz=args.search_schedule_tz,
        ),
        alarm=AlarmConfig(
            alarm_label=args.alarm_label,
            alarm_default_time=args.alarm_default_time,
            alarm_minutes_before=args.alarm_minutes_before_schedule,
        ),
        mode=mode,
        log_file=args.log_file,
        log_url=args.log_url,
        log_token=args.log_token,
    )
