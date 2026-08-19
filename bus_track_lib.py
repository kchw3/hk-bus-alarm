"""Arrival-tracking record type, shared by `track_bus_arrival.py` and its backfill.

The tracker records how one bus's ETA moves as it approaches, one row per
*distinct ETA value* rather than one per poll: an unchanged estimate advances
`last_seen` and `polls` on the existing row instead of adding another.

`TrackRecord` deliberately mirrors `LogRecord` in `bus_log_lib`, so both sinks
there work unchanged — `post_records()` only needs `as_dict()`, and
`write_log_csv()` takes the header as an argument.
"""

from dataclasses import dataclass

#: Column order of the local track CSV. Rows for the same (session_id, eta_iso)
#: supersede each other, so replaying the file in order converges on the final
#: state — the same property that makes `backfill_log.py` safe to re-run.
TRACK_CSV_HEADER = [
    "session_id",
    "route_id",
    "seq",
    "eta_iso",
    "first_seen",
    "last_seen",
    "polls",
    "window_from",
    "window_to",
]


@dataclass
class TrackRecord:
    """One distinct ETA value observed during a tracking session.

    `first_seen` / `last_seen` are the poll timestamps bracketing the period this
    estimate was being published. For the final record of a session the pair is
    what the chart plots: `eta_iso` is the last published estimate (the best guess
    at the arrival) and `last_seen` is the last moment the bus was still listed,
    which is an upper bound once the ETA has gone past.
    """

    session_id: str
    route_id: str
    seq: int
    eta_iso: str
    first_seen: str
    last_seen: str
    polls: int = 1
    window_from: str = ""
    window_to: str = ""

    def csv_row(self) -> list[str]:
        """Return the record as a CSV row matching `TRACK_CSV_HEADER`."""
        return [
            self.session_id,
            self.route_id,
            str(self.seq),
            self.eta_iso,
            self.first_seen,
            self.last_seen,
            str(self.polls),
            self.window_from,
            self.window_to,
        ]

    def as_dict(self) -> dict:
        """Return the record as the JSON object expected by `POST /api/arrivals/ingest`.

        Epoch columns are derived by the Worker from these ISO strings, exactly as
        `schedule_log` does, so a record carries one representation of each instant.
        """
        return {
            "session_id": self.session_id,
            "route_id": self.route_id,
            "seq": self.seq,
            "eta_iso": self.eta_iso,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "polls": self.polls,
            "window_from": self.window_from,
            "window_to": self.window_to,
        }
