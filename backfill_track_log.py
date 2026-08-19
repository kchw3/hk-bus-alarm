#!/usr/bin/env python3
"""
Upload an existing CSV arrival-track log to the arrivals ingest endpoint.

Use it when `track_bus_arrival.py` warned that an upload failed — the CSV row is
always written before the upload is attempted, so the local file is the complete
record. Ingest upserts on `(session_id, eta_iso)`, so re-running this is harmless.

Rows for the same key supersede each other in file order, which is exactly how
the tracker writes them: a row is appended when an ETA first appears and again
when its final `last_seen` is flushed. Replaying in order converges on the same
state the tracker ended with.

Usage:
    python backfill_track_log.py TRACK_LOG -log_url URL [-log_token TOKEN]
                                 [-batch_size N] [-dry_run]

Example:
    python backfill_track_log.py ~/bus_track.log \\
        -log_url https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest
"""

import argparse
import csv
import sys

from bus_log_lib import LOG_TOKEN_ENV, post_records
from bus_track_lib import TRACK_CSV_HEADER, TrackRecord


def _as_int(value: str, default: int = 0) -> int:
    """Parse an integer cell, tolerating blanks and junk rather than aborting a replay."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def read_records(track_log: str) -> list[TrackRecord]:
    """Read `track_log` and return one TrackRecord per data row."""
    records: list[TrackRecord] = []
    skipped = 0

    with open(track_log, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < len(TRACK_CSV_HEADER):
                skipped += 1
                continue
            (
                session_id, route_id, seq, eta_iso,
                first_seen, last_seen, polls, window_from, window_to,
            ) = row[:len(TRACK_CSV_HEADER)]
            if session_id == TRACK_CSV_HEADER[0]:
                continue  # header line
            if not session_id or not eta_iso:
                skipped += 1
                continue
            records.append(
                TrackRecord(
                    session_id=session_id,
                    route_id=route_id,
                    seq=_as_int(seq),
                    eta_iso=eta_iso,
                    first_seen=first_seen,
                    last_seen=last_seen,
                    polls=_as_int(polls, 1),
                    window_from=window_from,
                    window_to=window_to,
                )
            )

    if skipped:
        print(f"Skipped {skipped} malformed row(s).")
    return records


def main() -> int:
    """Parse arguments and upload the track log. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description="Upload an existing CSV arrival-track log to the arrivals ingest endpoint.",
        usage="%(prog)s TRACK_LOG -log_url URL [-log_token TOKEN] [-batch_size N] [-dry_run]",
    )
    _ = parser.add_argument("track_log", metavar="TRACK_LOG", help="Path to the CSV track log.")
    _ = parser.add_argument(
        "-log_url", required=True, metavar="URL",
        help="Ingest endpoint, e.g. https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest",
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
        records = read_records(args.track_log)
    except OSError as exc:
        print(f"Error reading {args.track_log}: {exc}", file=sys.stderr)
        return 1

    if not records:
        print("No records to upload.")
        return 0

    print(f"Read {len(records)} record(s) from {args.track_log}.")

    if args.dry_run:
        for record in records:
            print(f"  {record.session_id}  {record.eta_iso}  last seen {record.last_seen}")
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
