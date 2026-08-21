# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install hk-bus-eta
```

Requires Python 3.10+.

## Running the script

```bash
# All stops for default route
python bus_route_info.py

# Single stop
python bus_route_info.py -seq 3

# Single stop with full ETA field detail
python bus_route_info.py -seq 3 -detail

# Find latest bus within a time window (default tz +08:00)
python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00

# Same with system local timezone
python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00 -search_schedule_tz local
```

## Schedule history chart (`web/`)

```
set_alarm_with_bus_eta.py --POST /api/ingest--> Worker --> D1 (schedule_log)
        |                                          |
        +-- also appends the local CSV             +-- GET /api/data.json (public)
                                                   +-- static page: Plotly chart
```

- **Series identity is `(route_id, seq)`, never `route_id` alone.** `seq` is carried from the CLI flag through `LogRecord`/`TrackRecord`, the CSV, the ingest payload and both D1 primary keys. The stop *name* is never stored: `/api/stops.json` resolves it at render time from the same upstream file `hk-bus-eta` reads, so a re-survey upstream cannot rewrite history, and a failed lookup degrades to `stop 5`. `stop_id` rides alongside `seq` in D1 (not in either CSV) as the stable anchor, and both ingest paths `COALESCE` it so a CSV replay cannot erase one.
- **Upload** — `bus_log_lib.py` holds `LogRecord` plus both sinks (`write_log_csv`, `post_record`). `_log_run()` in `set_alarm_with_bus_eta.py` is the only caller and swallows everything: a bad log path or a failed upload is a stderr warning, never an exception, and the CSV is written before the upload is attempted so a failed row stays replayable. The failure message names the replay command and includes the response body, which is what separates a Worker rejection from an edge one; `backfill_log.py` re-sends the whole file and ingest upserts, so replaying already-delivered rows is a no-op. Uploads must send `USER_AGENT` — Cloudflare's Browser Integrity Check 403s urllib's default agent on the custom domain.
- **Worker** (`web/src/index.js`) — bearer-token ingest, public read endpoint, static assets. D1 upserts on `(ts, route_id, seq)` so re-ingest is idempotent and two stops polled in the same second cannot overwrite each other; `seq` is *required* on ingest rather than defaulted, because a silent default is exactly how the two-stop merge happened; `ts_epoch`/`eta_epoch` columns exist so ordering and `?days=` filtering stay correct regardless of UTC offset.
- **Chart** (`web/public/app.js`) — series are grouped by `(route_id, seq)` via `groupBySeries()`; keying on `route_id` alone let `perDateSummary()` overwrite one stop's schedule with another's. x is the calendar date, y is time of day plotted on a fixed dummy day (`1970-01-01 HH:MM:SS`) so Plotly's date axis can format `%H:%M`. Timestamps are split with a regex, never `new Date()`, so a `+08:00` bus is not re-expressed in the viewer's timezone. Points come straight from `find_schedule()`; the line follows the last record of each date. `perDateSummary()` deliberately takes opposite ends of the day: the schedule from the last poll, but the alarm and reason from the first — later polls are clamped to `now+2m`, so their alarm times track the clock rather than the bus. The day-to-day line is a spline (`smoothing` below Plotly's max so it does not overshoot a marker), and `weekendBands()` puts a `layer: "below"` rect behind each Sat/Sun, reading the weekday in UTC from the plain date string so no band shifts a day in another timezone.

API details are in README.md; the full Cloudflare setup (D1, ingest token, GitHub auto-deploy via Workers Builds, custom domain `hk-bus-alarm-chart.iteneti.top`) is in DEPLOYMENT.md.

## Arrival tracking (`track_bus_arrival.py`, `/arrivals/`)

```
track_bus_arrival.py --POST /api/arrivals/ingest--> Worker --> D1 (arrival_track)
        |                                              |
        +-- also appends the local CSV                 +-- GET /api/arrivals/data.json
                                                       +-- static page: /arrivals/
```

The API never publishes an arrival, so it is bracketed rather than measured: the **first published ETA** (lower bound, earliest green mark) up to the **last poll that still listed the bus** (upper bound, red), with the **last published ETA** (blue) inside it. The dotted connector spans first-green to red, not blue to red. Everything else follows from that.

- **The loop** — polls every 60s, every 15s once the ETA is within 3 minutes. Two `None` results from `find_schedule()` mean opposite things and must stay distinguished: before the first match it means "not in the feed yet, keep waiting"; after it, "the bus is gone, stop". A failed poll returns `None` from `poll_fn` — deliberately a third state meaning "no information", so a network blip cannot end a track early. `run_tracking()` takes injectable `now_fn`/`sleep_fn`/`poll_fn`/`out`; keep it that way, it is the only reason the loop is testable without waiting an hour. Progress is one flushed line per poll by default — the flush matters because cron redirects stdout to a file, where buffering makes a healthy run look hung; `-quiet` trims to significant events, `-debug` adds the raw feed.
- **Overdue entries are load-bearing here.** `find_schedule()` has no past filter, so an overdue bus keeps matching its window — which is what makes the upper bound observable at all. Adding a `not_before` filter to `find_schedule()` would silently break this feature, not just change a display.
- **One row per distinct ETA, not per poll.** An unchanged estimate advances `last_seen`/`polls` in memory; the row is rewritten only when the ETA changes and once more on exit. That final flush (in a `finally`, with SIGTERM/SIGINT raising through) is the *only* thing that records the last sighting, because an overdue ETA stops changing. Ingest upserts with `MIN(first_seen)`/`MAX(last_seen)` so a replay widens the bracket and can never narrow it.
- **Target identity** — selection is plain `find_schedule()` every poll, no bespoke matching. Since it returns the *latest* entry in the window, a wide window can move the target mid-track; the mitigation is a narrow window, and the tracker warns on a forward jump over 10 minutes rather than absorbing it silently.
- **The window identifies, it must not terminate.** After acquisition the matching upper bound becomes `max(to_dt, last_eta + track_grace)`, re-anchored on every new estimate. This is not cosmetic: a bus estimated at 07:28:43 against `-search_schedule_to 07:29` needs only 18s of forward drift to fall out of the window, and `find_schedule()` returning `None` is indistinguishable from the bus leaving the feed — so the run reports an arrival that never happened, with `last_seen` *before* the ETA and therefore no red marker. The grace is deliberately small so a following bus cannot be picked up; a jump larger than it ends the track instead of guessing. `beyond_window` on the result records when the grace was load-bearing.
- **The page** (`web/public/arrivals/`) shares the Worker, the D1 database, the `INGEST_TOKEN` and the deploy pipeline. It fetches `/api/arrivals/data.json` by absolute path — it sits one level down, so the main page's relative `./api/…` idiom would resolve wrongly. Setup is in DEPLOYMENT-ARRIVALS.md.

## Architecture

Single-file script (`bus_route_info.py`). Key layers:

- **Data access** — `HKEta()` from `hk-bus-eta` loads route/stop metadata from `hkbus.github.io` at startup, then `hketa.getEtas(route_id, seq, language)` fetches live ETAs per stop from the relevant operator API.
- **Route ID format** — `<BUS_NUMBER>+<SERVICE_TYPE>+<ORIGIN>+<DESTINATION>` (e.g. `81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE`). `seq` passed to `getEtas` is 0-based; the CLI `-seq` flag is 1-based.
- **Schedule search** — `find_schedule()` returns the latest ETA entry within a `[from_dt, to_dt]` window. The matched entry is flagged with ` *` in the table and optionally shown alone with `-detail`. `upcoming_etas()` does the past-entry filtering for both listings: the cutoff is exact to the second (truncating the minute count to an int used to grant a silent ~60s reprieve), and its `keep=` argument exempts the matched entry so the ` *` cannot vanish from the column while the detail block still prints that bus under `Matched schedule`. A kept past entry shows negative minutes.
- **Two ETA formatters, deliberately** — `format_eta_stamp()` prints seconds and feeds the `bus_route_info.py` table and the calendar event's ETA listing. `format_eta_entry()` stays at minute precision and must remain so: it produces the alarm scripts' console output and the logged `bus_schedule` column, and `-add_alarm_ha` feeds `FOUND:HH:MM` into a Home Assistant automation. Do not unify them.
- **Display** — CJK characters are counted as double-width (`display_width` / `ljust_display`) for correct terminal column alignment. The ETA column and the `-detail` block are *meant* to disagree: the column is filtered to buses still catchable, `print_detail()` dumps every entry `getEtas()` returned. A bus that departed seconds before the query showing up only in the detail block is correct, not a bug — do not "fix" it by filtering the raw dump, which is the evidence you want when an alarm fires for the wrong bus.
- **Timezone handling** — the schedule search window defaults to `+08:00`; `parse_tz()` accepts `local` or a `±HH:MM` offset string.
