#!/usr/bin/env python3
"""
Upload an existing CSV schedule log to the chart ingest endpoint.

Usage:
    python backfill_log.py LOG_FILE -log_url URL [-log_token TOKEN]
                           [-batch_size N] [-dry_run]

Examples:
    # Send the whole history of 81.log (token from $BUS_LOG_TOKEN)
    python backfill_log.py 81.log -log_url https://<worker>.workers.dev/api/ingest

    # Preview what would be sent without contacting the endpoint
    python backfill_log.py 81.log -log_url http://localhost:8787/api/ingest -dry_run

Rows whose `bus_schedule` column is empty (no bus found in the window) are skipped —
they carry no point to plot. Ingest is idempotent, so re-running is safe.
"""

import argparse
import csv
import sys
from datetime import datetime

from bus_log_lib import LOG_TOKEN_ENV, CSV_HEADER, LogRecord, post_records


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


def read_records(log_file: str) -> list[LogRecord]:
    """Read `log_file` and return one LogRecord per row that has a schedule."""
    records: list[LogRecord] = []
    skipped = 0

    with open(log_file, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < len(CSV_HEADER):
                continue
            timestamp, route_id, bus_schedule, alarm_time, reason = row[:5]
            if timestamp == CSV_HEADER[0]:
                continue  # header line
            eta_iso = parse_eta_iso(bus_schedule)
            if not eta_iso:
                skipped += 1
                continue
            records.append(
                LogRecord(
                    timestamp=timestamp,
                    route_id=route_id,
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
        usage="%(prog)s LOG_FILE -log_url URL [-log_token TOKEN] [-batch_size N] [-dry_run]",
    )
    _ = parser.add_argument("log_file", metavar="LOG_FILE", help="Path to the CSV log file.")
    _ = parser.add_argument(
        "-log_url", required=True, metavar="URL",
        help="Ingest endpoint, e.g. https://<worker>.workers.dev/api/ingest",
    )
    _ = parser.add_argument(
        "-log_token", default=None, metavar="TOKEN",
        help=f"Bearer token for the endpoint. Defaults to ${LOG_TOKEN_ENV}.",
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

    try:
        records = read_records(args.log_file)
    except OSError as exc:
        print(f"Error reading {args.log_file}: {exc}", file=sys.stderr)
        return 1

    if not records:
        print("No records to upload.")
        return 0

    print(f"Read {len(records)} record(s) from {args.log_file}.")

    if args.dry_run:
        for record in records:
            print(f"  {record.timestamp}  {record.route_id}  {record.eta_iso}")
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
