#!/usr/bin/env python3
"""
Track one bus until it arrives, by watching how its ETA moves.

The operator API never says when a bus actually arrived — it publishes estimates
for the next hour or so and drops an entry once the bus has gone. So the arrival
is deduced from two things this script records:

  * the LAST published ETA before the entry vanished — the best single guess;
  * the LAST poll at which the entry was still listed. Once a bus is overdue the
    feed keeps showing it with an ETA in the past, so this is an upper bound.

The real arrival lies between the two.

Usage:
    python track_bus_arrival.py -seq N
        -search_schedule_from HH:MM -search_schedule_to HH:MM
        [-route_id ROUTE_ID] [-search_schedule_tz TZ]
        [-log_file PATH] [-log_url URL] [-log_token TOKEN]
        [-max_runtime_minutes N] [-track_grace_minutes N] [-quiet] [-debug]

Examples:
    # Start about an hour ahead and track the bus due between 13:40 and 13:50
    python track_bus_arrival.py -seq 8 \\
        -search_schedule_from 13:40 -search_schedule_to 13:50 \\
        -log_file ~/bus_track.log

    # Also upload each observation to the arrivals chart
    python track_bus_arrival.py -seq 8 \\
        -search_schedule_from 13:40 -search_schedule_to 13:50 \\
        -log_file ~/bus_track.log \\
        -log_url https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest

Keep the window narrow. The schedule is selected with find_schedule(), which
returns the LATEST ETA inside the window, so a window wide enough to admit a
second bus can move the target mid-track.

The window IDENTIFIES the bus; it does not bound the tracking. Once a bus has
been acquired the matching window follows it, up to -track_grace_minutes past
-search_schedule_to, so ordinary forward drift cannot be mistaken for the bus
leaving the feed.

Requires:
    pip install hk-bus-eta
"""

import argparse
import signal
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from hk_bus_eta import HKEta

from hk_bus_common import (
    DEFAULT_ROUTE_ID,
    RouteQuery,
    ScheduleWindow,
    _offset,
    eta_to_datetime,
    find_schedule,
    flatten_stops,
    parse_hhmm,
    parse_tz,
    stop_info,
)
from bus_log_lib import LOG_TOKEN_ENV, post_record, write_log_csv
from bus_track_lib import TRACK_CSV_HEADER, TrackRecord


#: Poll rate while the bus is still far out, and during the acquire phase.
POLL_SLOW_SECONDS = 60
#: Poll rate once the bus is close, so the final estimates are finely sampled.
POLL_FAST_SECONDS = 15
#: Switch to the fast rate when the ETA is this near. An overdue ETA makes the
#: remaining time negative, so the fast rate persists until the bus is gone.
FAST_WINDOW_SECONDS = 180
#: A forward jump larger than this suggests find_schedule() has moved to a later
#: bus rather than the tracked one being delayed. Warned about, not acted on.
TARGET_JUMP_WARN_SECONDS = 600
#: Once a bus has been acquired, the matching window's upper bound follows it by
#: this much. Without it a bus whose ETA drifts past `-search_schedule_to` stops
#: matching, and the tracker reads that as "left the feed" — reporting an arrival
#: that never happened and losing the overdue sighting that bounds it. The window
#: identifies the target; it must not also terminate the track.
DEFAULT_TRACK_GRACE_MINUTES = 5
DEFAULT_MAX_RUNTIME_MINUTES = 180


class _StopTracking(Exception):
    """Raised by the SIGTERM/SIGINT handler so the final record is still flushed."""


def _install_signal_handlers() -> None:
    """Turn SIGTERM/SIGINT into an exception, so `finally` can flush the last row."""

    def handler(signum, frame):  # noqa: ARG001 - signature fixed by `signal`
        raise _StopTracking()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


class TrackSink:
    """Local CSV plus best-effort upload, in that order.

    Mirrors `_log_run()` in set_alarm_with_bus_eta.py: the CSV row is written
    before the upload is attempted, so a failed upload always leaves a replayable
    copy, and nothing here raises — a tracking run must not die because the
    network did.
    """

    def __init__(self, log_file: str | None, log_url: str | None, log_token: str | None):
        self.log_file = log_file
        self.log_url = log_url
        self.log_token = log_token
        self._replay_hint_shown = False

    def emit(self, record: TrackRecord) -> None:
        stored_locally = False
        if self.log_file is not None:
            try:
                write_log_csv(self.log_file, record, TRACK_CSV_HEADER)
                stored_locally = True
            except OSError as exc:
                print(f"Warning: could not write log file {self.log_file}: {exc}", file=sys.stderr)

        if self.log_url is None:
            return
        if post_record(self.log_url, self.log_token, record):
            return

        # One replay hint per run; a failing endpoint would otherwise repeat it
        # on every poll for the whole tracking session.
        if self._replay_hint_shown:
            return
        self._replay_hint_shown = True
        if stored_locally:
            print(
                f"         Records are still in {self.log_file}; upload them later with:\n"
                f"           python backfill_track_log.py {self.log_file} -log_url {self.log_url}",
                file=sys.stderr,
            )
        else:
            print(
                "         The record was not stored locally either, so it cannot be "
                "replayed. Add -log_file PATH to keep a replayable copy.",
                file=sys.stderr,
            )


@dataclass
class TrackResult:
    """Outcome of one tracking session.

    `outcome` is one of:
      "arrived"      — the entry left the feed; `final` holds the last observation.
      "not_acquired" — the run timed out without ever matching a bus.
      "timeout"      — matched, but the entry never left the feed before the cap.
      "interrupted"  — SIGTERM/SIGINT; whatever was observed has been flushed.
    """

    session_id: str
    outcome: str
    final: TrackRecord | None
    polls: int
    #: True when the tracked ETA ended up past `-search_schedule_to`, i.e. the
    #: tracking grace is the only reason the run did not stop early.
    beyond_window: bool = False


def _poll_interval(now: datetime, eta_dt: datetime | None) -> int:
    """Seconds to wait before the next poll, given the last known ETA."""
    if eta_dt is None:
        return POLL_SLOW_SECONDS
    remaining = (eta_dt - now).total_seconds()
    return POLL_FAST_SECONDS if remaining <= FAST_WINDOW_SECONDS else POLL_SLOW_SECONDS


def _emit(line: str) -> None:
    """Write one progress line, flushing immediately.

    Without the flush, redirecting to a file (`>> ~/track.out`, as the cron
    recipe does) buffers output for minutes at a time, and a healthy run is
    indistinguishable from a hung one for as long as the buffer holds.
    """
    print(line, flush=True)


def _progress(now: datetime, poll_no: int, note: str, wait: int | None = None) -> str:
    """One line per poll: when it ran, what was seen, and whether anything was written."""
    tail = f"  ·  next in {wait}s" if wait is not None else ""
    return f"  {now.strftime('%H:%M:%S')}  poll {poll_no:<4} {note}{tail}"


def _feed_dump(etas: list) -> str:
    """Every ETA the feed returned, for `-debug`.

    The selected entry alone cannot show whether the window is admitting a
    second bus; the full list can.
    """
    stamps = [str(e.get("eta")) for e in etas] or ["(empty)"]
    return "              feed: " + ", ".join(stamps)


def _eta_note(eta_dt: datetime, now: datetime) -> str:
    """'ETA 13:47:00 (+3.0m)' — the sign is what distinguishes overdue from upcoming."""
    remaining = (eta_dt - now).total_seconds() / 60
    return f"ETA {eta_dt.strftime('%H:%M:%S')} ({remaining:+.1f}m)"


def run_tracking(
    *,
    poll_fn,
    from_dt: datetime,
    to_dt: datetime,
    route_id: str,
    seq: int,
    sink: TrackSink,
    stop_id: str = "",
    now_fn,
    sleep_fn=time.sleep,
    max_runtime_minutes: int = DEFAULT_MAX_RUNTIME_MINUTES,
    track_grace_minutes: int = DEFAULT_TRACK_GRACE_MINUTES,
    quiet: bool = False,
    debug: bool = False,
    out=_emit,
) -> TrackResult:
    """Poll until the tracked bus leaves the feed, recording every distinct ETA.

    `poll_fn()` returns the ETA list, or None if the poll itself failed — a failed
    poll is "no information", deliberately not the same as "the bus is gone", so a
    transient network error cannot end a track early.

    `now_fn`, `sleep_fn` and `poll_fn` are injected so the loop is testable
    without the network and without real sleeps.

    Output is one line per poll by default, so an unattended run visibly confirms
    it is still polling even across the long stretches where nothing changes.
    `quiet` drops that to significant events only; `debug` adds every ETA the
    feed returned, which is what shows whether the window is admitting a second
    bus.
    """
    started = now_fn()
    session_id = f"{route_id}|{seq}|{started.isoformat(timespec='seconds')}"
    deadline = started + timedelta(minutes=max_runtime_minutes)
    window_from = from_dt.isoformat(timespec="seconds")
    window_to = to_dt.isoformat(timespec="seconds")
    grace = timedelta(minutes=track_grace_minutes)

    current: TrackRecord | None = None
    current_eta_dt: datetime | None = None
    # True when `current.last_seen`/`polls` have advanced since the row was last
    # written out. The flush on exit is what produces the "last sighting" upper
    # bound, because an overdue ETA stops changing and would never be rewritten.
    dirty = False
    acquired = False
    polls = 0
    outcome = "timeout"

    try:
        while True:
            now = now_fn()
            if now >= deadline:
                outcome = "timeout" if acquired else "not_acquired"
                out(_progress(
                    now, polls,
                    f"STOPPING — hit the {max_runtime_minutes}m runtime cap",
                ))
                break

            etas = poll_fn()
            polls += 1
            if etas is None:
                wait = _poll_interval(now, current_eta_dt)
                out(_progress(now, polls, "poll FAILED — keeping last known state", wait))
                sleep_fn(wait)
                continue

            # The window identifies the target; once it has, the upper bound
            # follows the bus so ordinary forward drift cannot end the track.
            # Before acquisition this is exactly `to_dt`, so selection of *which*
            # bus to track is unchanged.
            match_to = to_dt
            if current_eta_dt is not None:
                match_to = max(to_dt, current_eta_dt + grace)
            found = find_schedule(etas, from_dt, match_to)
            poll_ts = now.isoformat(timespec="seconds")

            if found is None:
                if acquired:
                    # The entry left the feed: the bus has gone.
                    out(_progress(
                        now, polls,
                        f"GONE from feed (searched to {match_to.strftime('%H:%M:%S')}) "
                        "— tracking complete",
                    ))
                    outcome = "arrived"
                    break
                if not quiet:
                    out(_progress(
                        now, polls,
                        f"waiting — nothing in {from_dt.strftime('%H:%M')}"
                        f"–{to_dt.strftime('%H:%M')} yet",
                        POLL_SLOW_SECONDS,
                    ))
                if debug:
                    out(_feed_dump(etas))
                sleep_fn(POLL_SLOW_SECONDS)
                continue

            eta_dt = eta_to_datetime(found)
            eta_iso = eta_dt.isoformat(timespec="seconds")
            acquired = True

            if current is None or current.eta_iso != eta_iso:
                if current_eta_dt is not None:
                    jump = (eta_dt - current_eta_dt).total_seconds()
                    if jump > TARGET_JUMP_WARN_SECONDS:
                        print(
                            f"Warning: selected ETA jumped forward {jump / 60:.0f}m "
                            f"({current.eta_iso} -> {eta_iso}). find_schedule() may have "
                            f"switched to a later bus; narrow the search window.",
                            file=sys.stderr,
                        )
                if dirty:
                    # Persist the outgoing row's final last_seen before moving on.
                    sink.emit(current)
                    dirty = False
                current = TrackRecord(
                    session_id=session_id,
                    route_id=route_id,
                    seq=seq,
                    stop_id=stop_id,
                    eta_iso=eta_iso,
                    first_seen=poll_ts,
                    last_seen=poll_ts,
                    polls=1,
                    window_from=window_from,
                    window_to=window_to,
                )
                was = current_eta_dt
                current_eta_dt = eta_dt
                sink.emit(current)
                change = (
                    "ACQUIRED" if was is None
                    else f"CHANGED from {was.strftime('%H:%M:%S')}"
                )
                # Say so whenever the grace is what kept this estimate matchable
                # — that is the signal the search window is too tight, and the
                # only point at which a following bus could have been picked up.
                beyond = " [beyond window, grace applied]" if eta_dt > to_dt else ""
                out(_progress(
                    now, polls,
                    f"{_eta_note(eta_dt, now)}  {change} → row written{beyond}",
                    _poll_interval(now, eta_dt),
                ))
            else:
                current.last_seen = poll_ts
                current.polls += 1
                dirty = True
                if not quiet:
                    overdue = eta_dt <= now
                    out(_progress(
                        now, polls,
                        f"{_eta_note(eta_dt, now)}  unchanged, no write"
                        + (" (overdue, still listed)" if overdue else "")
                        + f"  [seen {current.polls}x]",
                        _poll_interval(now, eta_dt),
                    ))

            if debug:
                out(_feed_dump(etas))
            sleep_fn(_poll_interval(now, eta_dt))
    except (_StopTracking, KeyboardInterrupt):
        outcome = "interrupted"
    finally:
        if current is not None and dirty:
            sink.emit(current)

    return TrackResult(
        session_id=session_id,
        outcome=outcome,
        final=current,
        polls=polls,
        beyond_window=current_eta_dt is not None and current_eta_dt > to_dt,
    )


def _print_summary(result: TrackResult, out=print) -> None:
    """Report what was deduced, without overstating it."""
    final = result.final

    if final is None:
        out("\nNo bus was ever matched in the search window.")
        out("Check -seq, the window, and that the run started while the bus was still ahead.")
        return

    last_eta = datetime.fromisoformat(final.eta_iso)
    last_seen = datetime.fromisoformat(final.last_seen)

    out("")
    out(f"Session          : {result.session_id}")
    out(f"Polls            : {result.polls}")
    out(f"Last published ETA: {final.eta_iso}")
    out(f"Last sighting     : {final.last_seen}")

    if result.outcome != "arrived":
        out(
            f"\nTracking ended early ({result.outcome}) — the entry was still in the feed, "
            "so the bus had not arrived yet. The figures above are the last observation, "
            "not an arrival."
        )
        return

    if result.beyond_window:
        out(
            "\nNote: the final estimate drifted past -search_schedule_to. The tracking "
            "grace\n      kept the track alive; without it this run would have reported "
            "an arrival\n      the moment the ETA crossed the window edge."
        )

    if last_seen > last_eta:
        overdue = (last_seen - last_eta).total_seconds() / 60
        out(
            f"\nBus arrived between {last_eta.strftime('%H:%M:%S')} and "
            f"{last_seen.strftime('%H:%M:%S')} {_offset(last_eta)} "
            f"(still listed {overdue:.1f}m past its own ETA)."
        )
    else:
        out(
            f"\nBus left the feed at {last_seen.strftime('%H:%M:%S')} {_offset(last_seen)}, "
            f"before its final ETA of {last_eta.strftime('%H:%M:%S')} elapsed — "
            "arrival is bounded above only by that last sighting."
        )


def run(
    query: RouteQuery,
    window: ScheduleWindow,
    *,
    log_file: str | None,
    log_url: str | None,
    log_token: str | None,
    max_runtime_minutes: int,
    track_grace_minutes: int,
    quiet: bool,
    debug: bool,
) -> None:
    """Resolve the stop, then track it until the bus is gone."""
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

    stop = stop_info(hketa, route, query.seq)
    if stop is None:
        total = len(flatten_stops(route))
        print(f"Error: -seq {query.seq} is out of range. Valid range is 1–{total}.")
        sys.exit(1)

    co, stop_id = stop.co, stop.stop_id
    name_en, name_zh = stop.name_en, stop.name_zh
    total = stop.total

    tz = window.schedule_tz if window.schedule_tz is not None else timezone(timedelta(hours=8))
    today = date.today()
    from_dt = datetime.combine(today, window.schedule_from, tzinfo=tz)
    to_dt = datetime.combine(today, window.schedule_to, tzinfo=tz)

    print(f"Stop {query.seq}/{total}  [{co}]  {stop_id}  {name_en} / {name_zh}")
    print(
        f"Window   : {from_dt.strftime('%H:%M')}–{to_dt.strftime('%H:%M')} "
        f"(tz {_offset(from_dt)})"
    )
    print(
        f"Polling  : every {POLL_SLOW_SECONDS}s, every {POLL_FAST_SECONDS}s "
        f"within {FAST_WINDOW_SECONDS // 60}m of the ETA. Ctrl-C to stop."
    )
    print(
        f"Grace    : once acquired, followed up to {track_grace_minutes}m past "
        f"{to_dt.strftime('%H:%M')}, so drift is not mistaken for arrival."
    )
    print(
        "           One line per poll below. A row is only written when the ETA "
        "changes,\n           so \"unchanged, no write\" is the normal steady "
        "state, not a problem.\n"
    )

    def poll_fn():
        try:
            return hketa.getEtas(route_id=query.route_id, seq=query.seq - 1, language="en")
        except Exception as exc:  # pylint: disable=broad-except
            print(f"Warning: ETA fetch failed: {exc}", file=sys.stderr)
            return None

    _install_signal_handlers()

    result = run_tracking(
        poll_fn=poll_fn,
        from_dt=from_dt,
        to_dt=to_dt,
        route_id=query.route_id,
        seq=query.seq,
        stop_id=stop.stop_id,
        sink=TrackSink(log_file, log_url, log_token),
        now_fn=lambda: datetime.now(tz=tz),
        max_runtime_minutes=max_runtime_minutes,
        track_grace_minutes=track_grace_minutes,
        quiet=quiet,
        debug=debug,
    )
    _print_summary(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Track one HK bus until it arrives, recording how its ETA moves.",
        usage=(
            "%(prog)s -seq N "
            "-search_schedule_from HH:MM -search_schedule_to HH:MM "
            "[-route_id ROUTE_ID] [-search_schedule_tz TZ] "
            "[-log_file PATH] [-log_url URL] [-log_token TOKEN] "
            "[-max_runtime_minutes N] [-track_grace_minutes N] [-quiet] [-debug]"
        ),
    )
    _ = parser.add_argument(
        "-route_id", default=DEFAULT_ROUTE_ID, metavar="ROUTE_ID",
        help=f'Route ID (default: "{DEFAULT_ROUTE_ID}")',
    )
    _ = parser.add_argument(
        "-seq", type=int, required=True, metavar="N",
        help="Stop number (1-based) to track.",
    )
    _ = parser.add_argument(
        "-search_schedule_from", type=parse_hhmm, required=True, metavar="HH:MM",
        help="Start of the window identifying the bus to track (e.g. 13:40).",
    )
    _ = parser.add_argument(
        "-search_schedule_to", type=parse_hhmm, required=True, metavar="HH:MM",
        help="End of that window (e.g. 13:50). Keep it narrow — see the module docstring.",
    )
    _ = parser.add_argument(
        "-search_schedule_tz", type=parse_tz, default=timezone(timedelta(hours=8)), metavar="TZ",
        help=(
            "Timezone for the search window. Accepted: 'local' or a fixed offset "
            "like '+08:00' / '-05:00'. Default: +08:00."
        ),
    )
    _ = parser.add_argument(
        "-log_file", default=None, metavar="PATH",
        help=(
            "Path to a CSV track log. One row per distinct ETA value; rows for the "
            "same (session_id, eta_iso) supersede each other, so the file replays "
            "cleanly with backfill_track_log.py."
        ),
    )
    _ = parser.add_argument(
        "-log_url", default=None, metavar="URL",
        help=(
            "Arrivals ingest endpoint (e.g. "
            "https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest). "
            "Independent of -log_file; upload failures only warn."
        ),
    )
    _ = parser.add_argument(
        "-log_token", default=None, metavar="TOKEN",
        help=(
            f"Bearer token for -log_url. Defaults to the {LOG_TOKEN_ENV} environment "
            "variable, which keeps the token out of the process list."
        ),
    )
    _ = parser.add_argument(
        "-max_runtime_minutes", type=int, default=DEFAULT_MAX_RUNTIME_MINUTES, metavar="N",
        help=(
            f"Safety cap on total runtime (default: {DEFAULT_MAX_RUNTIME_MINUTES}). "
            "Stops a never-acquired or never-vanishing target from looping forever."
        ),
    )
    _ = parser.add_argument(
        "-track_grace_minutes", type=int, default=DEFAULT_TRACK_GRACE_MINUTES, metavar="N",
        help=(
            f"Once a bus is acquired, follow it up to N minutes past "
            f"-search_schedule_to (default: {DEFAULT_TRACK_GRACE_MINUTES}). Without "
            "this an ETA drifting past the window edge reads as 'left the feed' and "
            "the run reports an arrival that never happened. Raise it for a route "
            "prone to long delays; lower it if a following bus keeps stealing the track."
        ),
    )
    _ = parser.add_argument(
        "-quiet", action="store_true", default=False,
        help=(
            "Only print significant events (a new estimate, the bus leaving the "
            "feed), not the unchanged polls in between."
        ),
    )
    _ = parser.add_argument(
        "-debug", action="store_true", default=False,
        help=(
            "Additionally dump every ETA the feed returned on each poll. Use this "
            "to see whether the search window is admitting a second bus."
        ),
    )

    args = parser.parse_args()

    if args.search_schedule_to <= args.search_schedule_from:
        parser.error(
            f"-search_schedule_to ({args.search_schedule_to.strftime('%H:%M')}) "
            f"must be later than -search_schedule_from "
            f"({args.search_schedule_from.strftime('%H:%M')})."
        )
    if args.max_runtime_minutes < 1:
        parser.error("-max_runtime_minutes must be at least 1.")
    if args.track_grace_minutes < 0:
        parser.error("-track_grace_minutes cannot be negative.")

    run(
        query=RouteQuery(route_id=args.route_id, seq=args.seq),
        window=ScheduleWindow(
            schedule_from=args.search_schedule_from,
            schedule_to=args.search_schedule_to,
            schedule_tz=args.search_schedule_tz,
        ),
        log_file=args.log_file,
        log_url=args.log_url,
        log_token=args.log_token,
        max_runtime_minutes=args.max_runtime_minutes,
        track_grace_minutes=args.track_grace_minutes,
        quiet=args.quiet,
        debug=args.debug,
    )
