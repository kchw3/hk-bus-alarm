"""
Schedule-log helpers shared by the HK bus alarm scripts.

One record type, two sinks:
  * `write_log_csv()`  — append a row to a local CSV file (the original log format)
  * `post_records()`   — POST records to the chart ingest endpoint (`web/`)

Posting is always best-effort: any failure is reported on stderr and swallowed so a
network problem can never stop an alarm from being set.
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

#: Environment variable consulted when no token is passed on the command line.
LOG_TOKEN_ENV = "BUS_LOG_TOKEN"

#: Column order of the CSV log. Unchanged from the original implementation.
CSV_HEADER = ["timestamp", "route_id", "bus_schedule", "alarm_time", "reason"]

_POST_TIMEOUT_SECONDS = 5


@dataclass
class LogRecord:
    """One logged run of a schedule lookup.

    `eta_iso` is the full ISO 8601 timestamp of the matched schedule (empty when no
    schedule was found). It is sent to the ingest endpoint but deliberately kept out
    of the CSV so the on-device log format stays byte-compatible with older runs.
    """

    timestamp: str
    route_id: str
    bus_schedule: str
    alarm_time: str
    reason: str
    eta_iso: str = ""

    def csv_row(self) -> list[str]:
        """Return the record as a CSV row matching `CSV_HEADER`."""
        return [
            self.timestamp,
            self.route_id,
            self.bus_schedule,
            self.alarm_time,
            self.reason,
        ]

    def as_dict(self) -> dict[str, str]:
        """Return the record as the JSON object expected by `POST /api/ingest`."""
        return {
            "timestamp": self.timestamp,
            "route_id": self.route_id,
            "bus_schedule": self.bus_schedule,
            "eta_iso": self.eta_iso,
            "alarm_time": self.alarm_time,
            "reason": self.reason,
        }


def write_log_csv(log_file: str, record: LogRecord) -> None:
    """Append one CSV row to `log_file`, writing the header when the file is new or empty."""
    is_new = not os.path.exists(log_file) or os.path.getsize(log_file) == 0
    with open(log_file, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(CSV_HEADER)
        writer.writerow(record.csv_row())


def resolve_token(token: str | None) -> str:
    """Return `token`, falling back to the `BUS_LOG_TOKEN` environment variable."""
    return token if token else os.environ.get(LOG_TOKEN_ENV, "")


def post_records(
    url: str,
    token: str | None,
    records: list[LogRecord],
    *,
    timeout: int = _POST_TIMEOUT_SECONDS,
) -> bool:
    """POST `records` to the ingest endpoint. Never raises; returns True on success.

    A single record is sent as a JSON object, several as a JSON array. Failures print a
    one-line warning to stderr and return False.
    """
    if not records:
        return True

    payload = [r.as_dict() for r in records]
    body = json.dumps(payload[0] if len(payload) == 1 else payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    auth_token = resolve_token(token)
    if auth_token:
        request.add_header("Authorization", f"Bearer {auth_token}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return True
            print(
                f"Warning: schedule log upload returned HTTP {response.status}.",
                file=sys.stderr,
            )
    except urllib.error.HTTPError as exc:
        print(
            f"Warning: schedule log upload failed with HTTP {exc.code} ({exc.reason}).",
            file=sys.stderr,
        )
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Warning: schedule log upload failed: {exc}", file=sys.stderr)
    return False


def post_record(
    url: str,
    token: str | None,
    record: LogRecord,
    *,
    timeout: int = _POST_TIMEOUT_SECONDS,
) -> bool:
    """POST a single record to the ingest endpoint. Never raises; returns True on success."""
    return post_records(url, token, [record], timeout=timeout)
