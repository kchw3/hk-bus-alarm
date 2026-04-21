# bus_route_info.py

A command-line tool to query Hong Kong bus route stops and live ETAs using the [`hk-bus-eta`](https://github.com/hkbus/hk-bus-crawling) library.

## Requirements

- Python 3.10+
- [`hk-bus-eta`](https://pypi.org/project/hk-bus-eta/) library

```bash
pip install hk-bus-eta
```

## Usage

```
python bus_route_info.py [-route_id ROUTE_ID] [-seq N] [-detail]
                         [-search_schedule_from HH:MM] [-search_schedule_to HH:MM]
                         [-search_schedule_tz TZ]
```

## Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `-route_id` | No | `81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE` | Full route ID string. Use the `+`-delimited format (e.g. `81+1+ORIG+DEST`). |
| `-seq` | No | *(all stops)* | Show only stop N (1-based). Required by `-detail` and the schedule search flags. |
| `-detail` | No | off | After each stop's table row, print all raw fields returned by `getEtas()` in a PowerShell-style list. Requires `-seq`. |
| `-search_schedule_from` | No | — | Start of the time window to search for a bus schedule, in `HH:MM` format. Must be paired with `-search_schedule_to`. Requires `-seq`. |
| `-search_schedule_to` | No | — | End of the time window. Must be later than `-search_schedule_from`. Requires `-seq`. |
| `-search_schedule_tz` | No | `+08:00` | Timezone for the schedule search window. Accepts `local` (system timezone) or a fixed offset like `+09:00` or `-05:00`. Only meaningful when the schedule search flags are set. |

### Parameter dependencies

```
-detail                    → requires -seq
-search_schedule_from      → requires -seq, must be paired with -search_schedule_to
-search_schedule_to        → requires -seq, must be paired with -search_schedule_from
-search_schedule_tz        → only meaningful when schedule search flags are set
```

## Output

### Route header

Always printed first:

```
============================================================
Route ID : 81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE
Origin   : HIGH SPEED RAIL WEST KOWLOON STATION  /  西九龍站
Dest     : WO CHE  /  禾車
============================================================
```

### Stop table

Columns are dynamically sized to the widest value in each column (plus 3 spaces of padding). Chinese characters are counted as double-width for correct alignment.

```
  Seq   Co      Stop ID                         English                              中文                   ETA
  ----------------------------------------------------------------------------------------------------------
  1     kmb     E10101...                       West Kowloon Station Bus Terminus    西九龍站巴士總站        2026-04-21T14:32+08:00 (5m),  2026-04-21T14:50+08:00 (23m)
  2     kmb     ...
```

**ETA format:** `YYYY-MM-DDTHH:MM±HH:MM (Nm)` — ISO 8601 timestamp followed by minutes from now in parentheses. Only upcoming arrivals (≥ 0 minutes away) are shown. `—` is printed if no upcoming ETAs are available.

When a schedule search is active and a match is found for a stop, the matching ETA entry is marked with ` *`:

```
  2026-04-21T14:32+08:00 (5m) *,  2026-04-21T14:50+08:00 (23m)
```

### Detail block (`-detail`)

Printed below each stop row, showing all fields returned by `getEtas()` for that stop. Field names are right-padded to align values:

```
  ETA entry 1 of 2
  ------------------
  eta       : 2026-04-21T14:32:00+08:00
  remark    : {'zh': '原定班次', 'en': 'Scheduled Bus'}
  co        : kmb

  ETA entry 2 of 2
  ------------------
  ...
```

When `-search_schedule_from`/`-search_schedule_to` are also set:

- **Match found** — only the matched entry is shown, labelled `Matched schedule`.
- **No match** — the detail block is replaced with:
  ```
  No bus schedule found between 14:00 and 15:00 (tz +08:00).
  ```

## Data source

Live ETA data is fetched in real time from the Hong Kong government open-data APIs via `hk-bus-eta`. The URLs used depend on the bus operator of the route:

| Operator | API endpoint |
|---|---|
| KMB / Long Win | `https://data.etabus.gov.hk/v1/transport/kmb/stop-eta/<STOP_ID>` |
| CTB (Citybus) | `https://rt.data.gov.hk/v2/transport/citybus/eta/<STOP_ID>/<ROUTE>/1` |
| NLB | `https://rt.data.gov.hk/v1/transport/nlb/stop.php?action=estimatedArrivals` |
| MTR | `https://rt.data.gov.hk/v1/transport/mtr/getSchedule.php` |
| GMB (green minibus) | `https://data.etagmb.gov.hk/eta/stop-seq/<STOP_ID>/<ROUTE_ID>` |

Route and stop metadata (names, stop IDs, etc.) is loaded once at startup from the pre-compiled JSON published at [`hkbus.github.io/hk-bus-crawling`](https://hkbus.github.io/hk-bus-crawling/routeFareList.min.json).

## Examples

### Show all stops for the default route

```bash
python bus_route_info.py
```

### Show all stops for a specific route

```bash
python bus_route_info.py -route_id "81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE"
```

If the route ID is not found, the script prints all known routes with the same bus number prefix so you can identify the correct ID.

### Show a single stop

```bash
python bus_route_info.py -seq 3
```

### Show a single stop with full ETA field detail

```bash
python bus_route_info.py -seq 3 -detail
```

### Search for the latest bus arriving within a time window

```bash
python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00
```

The matched entry (the latest arrival within the window) is marked with `*` in the ETA column.

### Search with a specific timezone

```bash
# Use Japan Standard Time
python bus_route_info.py -seq 3 -search_schedule_from 15:00 -search_schedule_to 16:00 -search_schedule_tz +09:00

# Use the system's local timezone
python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00 -search_schedule_tz local
```

### Search with full detail of the matched schedule

```bash
python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00 -detail
```

## Finding a route ID

Route IDs follow the pattern `<BUS_NUMBER>+<SERVICE_TYPE>+<ORIGIN>+<DESTINATION>`. To discover valid IDs, run the script with an invalid route and check the suggestions:

```bash
python bus_route_info.py -route_id "81"
# Route not found: '81'
# Available routes containing that bus number:
#   81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE
#   81+1+WO CHE+HIGH SPEED RAIL WEST KOWLOON STATION
#   ...
```