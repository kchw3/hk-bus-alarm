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

- **Upload** — `bus_log_lib.py` holds `LogRecord` plus both sinks (`write_log_csv`, `post_record`). `_log_run()` in `set_alarm_with_bus_eta.py` is the only caller and swallows everything: a bad log path or a failed upload is a stderr warning, never an exception, and the CSV is written before the upload is attempted so a failed row stays replayable. The failure message names the replay command and includes the response body, which is what separates a Worker rejection from an edge one; `backfill_log.py` re-sends the whole file and ingest upserts, so replaying already-delivered rows is a no-op. Uploads must send `USER_AGENT` — Cloudflare's Browser Integrity Check 403s urllib's default agent on the custom domain.
- **Worker** (`web/src/index.js`) — bearer-token ingest, public read endpoint, static assets. D1 upserts on `(ts, route_id)` so re-ingest is idempotent; `ts_epoch`/`eta_epoch` columns exist so ordering and `?days=` filtering stay correct regardless of UTC offset.
- **Chart** (`web/public/app.js`) — x is the calendar date, y is time of day plotted on a fixed dummy day (`1970-01-01 HH:MM:SS`) so Plotly's date axis can format `%H:%M`. Timestamps are split with a regex, never `new Date()`, so a `+08:00` bus is not re-expressed in the viewer's timezone. Points come straight from `find_schedule()`; the line follows the last record of each date. `perDateSummary()` deliberately takes opposite ends of the day: the schedule from the last poll, but the alarm and reason from the first — later polls are clamped to `now+2m`, so their alarm times track the clock rather than the bus.

API details are in README.md; the full Cloudflare setup (D1, ingest token, GitHub auto-deploy via Workers Builds, custom domain `hk-bus-alarm-chart.iteneti.top`) is in DEPLOYMENT.md.

## Architecture

Single-file script (`bus_route_info.py`). Key layers:

- **Data access** — `HKEta()` from `hk-bus-eta` loads route/stop metadata from `hkbus.github.io` at startup, then `hketa.getEtas(route_id, seq, language)` fetches live ETAs per stop from the relevant operator API.
- **Route ID format** — `<BUS_NUMBER>+<SERVICE_TYPE>+<ORIGIN>+<DESTINATION>` (e.g. `81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE`). `seq` passed to `getEtas` is 0-based; the CLI `-seq` flag is 1-based.
- **Schedule search** — `find_schedule()` returns the latest ETA entry within a `[from_dt, to_dt]` window. The matched entry is flagged with ` *` in the table and optionally shown alone with `-detail`.
- **Display** — CJK characters are counted as double-width (`display_width` / `ljust_display`) for correct terminal column alignment.
- **Timezone handling** — the schedule search window defaults to `+08:00`; `parse_tz()` accepts `local` or a `±HH:MM` offset string.
