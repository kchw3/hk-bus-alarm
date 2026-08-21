"""Regression tests for carrying `seq` end-to-end.

Two things are being protected here:

  1. `seq` reaches every sink, so two stops of one route stay distinguishable.
  2. Nothing an external system consumes changed shape. `-add_alarm_ha` feeds
     `FOUND:HH:MM` into a Home Assistant automation and `bus_schedule` is parsed
     back out of the CSV by backfill_log.py — both are contracts, not just output.

Run with:  python -m unittest discover -s tests
"""

import csv
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backfill_log import read_records
from bus_log_lib import CSV_HEADER, LEGACY_CSV_HEADER, LogRecord, write_log_csv
from bus_track_lib import TRACK_CSV_HEADER, TrackRecord
from hk_bus_common import flatten_stops, format_eta_entry, stop_info

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RID = "81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE"


class FakeHketa:
    """The two attributes `stop_info()` actually reads."""

    stop_list = {
        "S1": {"name": {"en": "NING PO STREET", "zh": "寧波街"}},
        "S5": {"name": {"en": "KOWLOON CENTRAL POST OFFICE (YT134)", "zh": "九龍中央郵政局 (YT134)"}},
    }


ROUTE = {"stops": {"kmb": ["S1", "S2", "S3", "S4", "S5", "S6"]}}


class TestStopResolution(unittest.TestCase):
    def test_seq_is_one_based(self):
        """The CLI flag is 1-based; getEtas() is 0-based. `seq` must mean the flag."""
        self.assertEqual(stop_info(FakeHketa(), ROUTE, 1).stop_id, "S1")
        self.assertEqual(stop_info(FakeHketa(), ROUTE, 5).stop_id, "S5")

    def test_resolves_names_and_total(self):
        stop = stop_info(FakeHketa(), ROUTE, 5)
        self.assertEqual(stop.name_en, "KOWLOON CENTRAL POST OFFICE (YT134)")
        self.assertEqual(stop.name_zh, "九龍中央郵政局 (YT134)")
        self.assertEqual(stop.total, 6)
        self.assertEqual(stop.co, "kmb")

    def test_out_of_range_returns_none(self):
        for seq in (0, -1, 7):
            self.assertIsNone(stop_info(FakeHketa(), ROUTE, seq))

    def test_unknown_stop_id_does_not_raise(self):
        """A stop missing from stop_list must degrade, not crash a live alarm run."""
        self.assertEqual(stop_info(FakeHketa(), ROUTE, 2).name_en, "—")

    def test_flatten_preserves_order_across_operators(self):
        route = {"stops": {"kmb": ["A", "B"], "ctb": ["C"], "bad": "not-a-list"}}
        self.assertEqual(flatten_stops(route), [("kmb", "A"), ("kmb", "B"), ("ctb", "C")])


class TestLogRecord(unittest.TestCase):
    def _record(self, **kw):
        base = dict(
            timestamp="2026-08-21T07:00:00+08:00", route_id=RID, seq=5,
            bus_schedule="2026-08-21T07:26+08:00 (26m)", alarm_time="06:56",
            reason="30m before schedule", eta_iso="2026-08-21T07:26:00+08:00",
            stop_id="S5",
        )
        base.update(kw)
        return LogRecord(**base)

    def test_csv_has_seq_third_and_no_stop_id(self):
        self.assertEqual(CSV_HEADER[2], "seq")
        self.assertEqual(len(CSV_HEADER), len(LEGACY_CSV_HEADER) + 1)
        row = self._record().csv_row()
        self.assertEqual(row[2], "5")
        self.assertNotIn("S5", row, "stop_id must stay out of the CSV")

    def test_other_csv_columns_keep_their_values(self):
        """Only a column was added — no existing value may change."""
        row = self._record().csv_row()
        self.assertEqual(row[0], "2026-08-21T07:00:00+08:00")
        self.assertEqual(row[1], RID)
        self.assertEqual(row[3], "2026-08-21T07:26+08:00 (26m)")
        self.assertEqual(row[4], "06:56")
        self.assertEqual(row[5], "30m before schedule")

    def test_wire_format_carries_seq_and_stop_id(self):
        sent = self._record().as_dict()
        self.assertEqual(sent["seq"], 5)
        self.assertEqual(sent["stop_id"], "S5")

    def test_bus_schedule_stays_minute_precision(self):
        """`bus_schedule` is parsed back by backfill_log.py; seconds would break it."""
        eta = datetime.now(tz=timezone(timedelta(hours=8))) + timedelta(minutes=26)
        formatted = format_eta_entry({"eta": eta.isoformat(timespec="seconds")})
        self.assertRegex(formatted, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2} \(-?\d+m\)$")


class TestTrackRecord(unittest.TestCase):
    def test_track_csv_layout_is_unchanged(self):
        """The track log needs no migration, so its columns must not move."""
        self.assertEqual(
            TRACK_CSV_HEADER,
            ["session_id", "route_id", "seq", "eta_iso", "first_seen",
             "last_seen", "polls", "window_from", "window_to"],
        )

    def test_stop_id_is_sent_but_not_written(self):
        rec = TrackRecord(
            session_id="s", route_id=RID, seq=5, eta_iso="2026-08-21T07:28:00+08:00",
            first_seen="a", last_seen="b", stop_id="S5",
        )
        self.assertEqual(rec.as_dict()["stop_id"], "S5")
        self.assertEqual(len(rec.csv_row()), len(TRACK_CSV_HEADER))
        self.assertNotIn("S5", rec.csv_row())


class TestBackfillReadsBothLayouts(unittest.TestCase):
    LEGACY = (
        "timestamp,route_id,bus_schedule,alarm_time,reason\n"
        f"2026-04-25T13:44:27+08:00,{RID},2026-04-25T14:08+08:00 (24m),13:46,"
        "30m before schedule; clamped to now+2m\n"
    )

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, text):
        path = os.path.join(self.dir, "log.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_legacy_file_needs_an_explicit_seq(self):
        """Guessing would file a whole history under the wrong stop."""
        with self.assertRaises(ValueError):
            read_records(self._write(self.LEGACY))

    def test_legacy_file_with_seq_flag(self):
        records = read_records(self._write(self.LEGACY), default_seq=5)
        self.assertEqual([r.seq for r in records], [5])
        self.assertEqual(records[0].eta_iso, "2026-04-25T14:08:00+08:00")

    def test_current_file_uses_its_own_seq(self):
        """A migrated file's own column wins; -seq must not override it."""
        path = os.path.join(self.dir, "new.csv")
        for seq in (5, 7):
            write_log_csv(path, LogRecord(
                timestamp=f"2026-08-21T0{seq}:00:00+08:00", route_id=RID, seq=seq,
                bus_schedule="2026-08-21T07:26+08:00 (26m)", alarm_time="06:56",
                reason="r", eta_iso="2026-08-21T07:26:00+08:00",
            ))
        self.assertEqual([r.seq for r in read_records(path, default_seq=99)], [5, 7])

    def test_sed_migrated_file_matches_a_natively_written_one(self):
        """The sed recipe in DEPLOYMENT.md must produce exactly the tool's layout."""
        path = self._write(self.LEGACY)
        subprocess.run(
            ["sed", "-i", "-E",
             "1s/^timestamp,route_id,/timestamp,route_id,seq,/; 1!s/^([^,]+,[^,]+),/\\1,5,/",
             path],
            check=True,
        )
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], CSV_HEADER)
        self.assertEqual(rows[1][2], "5")
        self.assertEqual(rows[1][1], RID, "the route id must survive intact")
        self.assertEqual(rows[1][3], "2026-04-25T14:08+08:00 (24m)")
        # And it now reads without -seq.
        self.assertEqual([r.seq for r in read_records(path)], [5])


class TestHomeAssistantContract(unittest.TestCase):
    """`-add_alarm_ha` output is parsed by a Home Assistant automation."""

    def test_ha_branch_prints_exactly_one_status_line(self):
        with open(os.path.join(REPO, "set_alarm_with_bus_eta.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('print(f"{status}:{alarm_dt.strftime(\'%H:%M\')}")', source)
        self.assertIn('status = "FOUND" if found is not None else "NOT_FOUND"', source)

    def test_stop_line_stays_behind_the_ha_guard(self):
        """Adding stop detail must not leak an extra line into HA's stdout."""
        with open(os.path.join(REPO, "set_alarm_with_bus_eta.py"), encoding="utf-8") as fh:
            source = fh.read()
        idx = source.index("f\"Stop {stop.seq}/{stop.total}")
        preceding = source[:idx]
        self.assertTrue(
            preceding.rstrip().endswith("print(") and "if not ha:" in preceding[-120:],
            "the stop line must remain inside an `if not ha:` block",
        )


class TestTrackingLoopStillWorks(unittest.TestCase):
    """`run_tracking()` gained a stop_id parameter; the loop itself must be intact."""

    def test_stop_id_reaches_every_row_and_the_track_still_ends(self):
        import track_bus_arrival as tba

        tz = timezone(timedelta(hours=8))
        base = datetime(2026, 8, 21, 7, 0, 0, tzinfo=tz)
        clock = {"t": base}

        def now_fn():
            return clock["t"]

        def sleep_fn(seconds):
            clock["t"] += timedelta(seconds=seconds)

        # Feed: the bus is listed with a drifting ETA, then vanishes.
        etas = [
            [{"eta": "2026-08-21T07:26:00+08:00"}],
            [{"eta": "2026-08-21T07:26:00+08:00"}],
            [{"eta": "2026-08-21T07:28:00+08:00"}],
            [],
        ]
        seen = []

        def poll_fn():
            return etas.pop(0) if etas else []

        class Sink:
            def emit(self, record):
                seen.append(record)

        result = tba.run_tracking(
            poll_fn=poll_fn,
            from_dt=datetime(2026, 8, 21, 7, 20, tzinfo=tz),
            to_dt=datetime(2026, 8, 21, 7, 30, tzinfo=tz),
            route_id=RID,
            seq=5,
            stop_id="S5",
            sink=Sink(),
            now_fn=now_fn,
            sleep_fn=sleep_fn,
            quiet=True,
            out=lambda line: None,
        )

        self.assertEqual(result.outcome, "arrived")
        self.assertTrue(seen, "the loop must have emitted rows")
        self.assertTrue(all(r.stop_id == "S5" for r in seen))
        self.assertTrue(all(r.seq == 5 for r in seen))
        self.assertIn("|5|", result.session_id, "session_id still pins the seq")
        # One row per distinct ETA, not per poll.
        self.assertEqual(len({r.eta_iso for r in seen}), 2)


if __name__ == "__main__":
    unittest.main()
