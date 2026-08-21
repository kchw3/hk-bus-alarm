#!/usr/bin/env python3
"""
Upload an existing CSV schedule log to the chart ingest endpoint.

Usage:
    python backfill_log.py LOG_FILE -log_url URL [-log_token TOKEN]
                           [-seq N] [-batch_size N] [-dry_run]

Examples:
    # Send the whole history of 81.log (token from $BUS_LOG_TOKEN)
    python backfill_log.py 81.log -log_url https://hk-bus-alarm-chart.iteneti.top/api/ingest

    # Preview what would be sent without contacting the endpoint
    python backfill_log.py 81.log -log_url http://localhost:8787/api/ingest -dry_run

    # Replay a log written before the `seq` column, attributing it to stop 5
    python backfill_log.py 81.log -seq 5 -log_url http://localhost:8787/api/ingest

Rows whose `bus_schedule` column is empty (no bus found in the window) are skipped —
they carry no point to plot. Ingest is idempotent, so re-running is safe.
"""

import argparse
import csv
import sys
from datetime import datetime

from bus_log_lib import (
    CSV_HEADER,
    LEGACY_CSV_HEADER,
    LOG_TOKEN_ENV,
    LogRecord,
    post_records,
)


def parse_eta_iso(bus_schedule: str) -> str:
    """Extract the ISO timestamp from a `bus_schedule` cell.

    `format_eta_entry()` writes '2026-08-17T07:26+08:00 (57m)'; the minutes-from-now
    suffix is dropped and the timestamp normalised. Returns '' if unparseable.
    """
    stamp = bus_schedule.split(" (")[0].strip()
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(stamp).isoformat(timespec="seconds")
    except ValueError:
        return ""


def _has_seq_column(log_file: str) -> bool:
    """True if `log_file` uses the current layout, i.e. its header names `seq`.

    A pre-`seq` log has one fewer column, and the two layouts cannot be told
    apart row by row — `route_id` is followed by either the seq or the schedule,
    and an empty schedule cell looks like neither. The header is the only
    reliable signal, so a headerless file is treated as legacy and needs `-seq`.
    """
    with open(log_file, newline="", encoding="utf-8") as fh:
        first = next(csv.reader(fh), [])
    return len(first) > 2 and first[2] == "seq"


def read_records(log_file: str, default_seq: int | None = None) -> list[LogRecord]:
    """Read `log_file` and return one LogRecord per row that has a schedule.

    Reads either CSV layout. For a pre-`seq` log every row is attributed to
    `default_seq`, which the caller must supply — guessing it would silently
    file a whole history under the wrong stop.
    """
    records: list[LogRecord] = []
    skipped = 0
    has_seq = _has_seq_column(log_file)
    if not has_seq and default_seq is None:
        raise ValueError(
            f"{log_file} predates the `seq` column. Re-run with -seq N to say which "
            "stop these rows were logged at."
        )

    width = len(CSV_HEADER) if has_seq else len(LEGACY_CSV_HEADER)

    with open(log_file, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < width:
                continue
            if row[0] == CSV_HEADER[0]:
                continue  # header line
            if has_seq:
                timestamp, route_id, raw_seq, bus_schedule, alarm_time, reason = row[:6]
                try:
                    seq = int(raw_seq)
                except ValueError:
                    skipped += 1
                    continue
            else:
                timestamp, route_id, bus_schedule, alarm_time, reason = row[:5]
                seq = default_seq
            eta_iso = parse_eta_iso(bus_schedule)
            if not eta_iso:
                skipped += 1
                continue
            records.append(
                LogRecord(
                    timestamp=timestamp,
                    route_id=route_id,
                    seq=seq,
                    bus_schedule=bus_schedule,
                    alarm_time=alarm_time,
                    reason=reason,
                    eta_iso=eta_iso,
                )
            )

    if skipped:
        print(f"Skipped {skipped} row(s) with no schedule.")
    return records


def main() -> int:
    """Parse arguments and upload the log file. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description="Upload an existing CSV schedule log to the chart ingest endpoint.",
        usage="%(prog)s LOG_FILE -log_url URL [-log_token TOKEN] [-seq N] [-batch_size N] [-dry_run]",
    )
    _ = parser.add_argument("log_file", metavar="LOG_FILE", help="Path to the CSV log file.")
    _ = parser.add_argument(
        "-log_url", required=True, metavar="URL",
        help="Ingest endpoint, e.g. https://hk-bus-alarm-chart.iteneti.top/api/ingest",
    )
    _ = parser.add_argument(
        "-log_token", default=None, metavar="TOKEN",
        help=f"Bearer token for the endpoint. Defaults to ${LOG_TOKEN_ENV}.",
    )
    _ = parser.add_argument(
        "-seq", type=int, default=None, metavar="N",
        help=(
            "Stop sequence to attribute rows to, for a log written before the "
            "`seq` column existed. Ignored when the file already has the column."
        ),
    )
    _ = parser.add_argument(
        "-batch_size", type=int, default=100, metavar="N",
        help="Records per POST request (default: 100).",
    )
    _ = parser.add_argument(
        "-dry_run", action="store_true",
        help="Print what would be sent without contacting the endpoint.",
    )
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("-batch_size must be at least 1.")
    if args.seq is not None and args.seq < 1:
        parser.error("-seq must be at least 1.")

    try:
        records = read_records(args.log_file, default_seq=args.seq)
    except OSError as exc:
        print(f"Error reading {args.log_file}: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not records:
        print("No records to upload.")
        return 0

    print(f"Read {len(records)} record(s) from {args.log_file}.")

    if args.dry_run:
        for record in records:
            print(
                f"  {record.timestamp}  {record.route_id}  "
                f"seq {record.seq}  {record.eta_iso}"
            )
        print(f"\nDry run — nothing sent to {args.log_url}.")
        return 0

    sent = 0
    for start in range(0, len(records), args.batch_size):
        batch = records[start:start + args.batch_size]
        if not post_records(args.log_url, args.log_token, batch, timeout=30):
            print(
                f"Upload failed after {sent} record(s); "
                "ingest is idempotent, so it is safe to re-run.",
                file=sys.stderr,
            )
            return 1
        sent += len(batch)
        print(f"  uploaded {sent}/{len(records)}")

    print(f"Uploaded {sent} record(s) to {args.log_url}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
