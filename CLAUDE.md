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

## Architecture

Single-file script (`bus_route_info.py`). Key layers:

- **Data access** — `HKEta()` from `hk-bus-eta` loads route/stop metadata from `hkbus.github.io` at startup, then `hketa.getEtas(route_id, seq, language)` fetches live ETAs per stop from the relevant operator API.
- **Route ID format** — `<BUS_NUMBER>+<SERVICE_TYPE>+<ORIGIN>+<DESTINATION>` (e.g. `81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE`). `seq` passed to `getEtas` is 0-based; the CLI `-seq` flag is 1-based.
- **Schedule search** — `find_schedule()` returns the latest ETA entry within a `[from_dt, to_dt]` window. The matched entry is flagged with ` *` in the table and optionally shown alone with `-detail`.
- **Display** — CJK characters are counted as double-width (`display_width` / `ljust_display`) for correct terminal column alignment.
- **Timezone handling** — the schedule search window defaults to `+08:00`; `parse_tz()` accepts `local` or a `±HH:MM` offset string.
