# HK Bus Alarm

A pair of command-line tools to query Hong Kong bus route stops and live ETAs, with the ability to set an Android alarm for a found bus schedule.

## Requirements

- Python 3.10+
- [`hk-bus-eta`](https://pypi.org/project/hk-bus-eta/) library
- Android device with Termux (required only for `set_alarm_with_bus_eta.py -add_alarm`)

```bash
pip install hk-bus-eta
```

## Files

| File | Purpose |
|---|---|
| `bus_route_info.py` | Query and display route stops and live ETAs |
| `set_alarm_with_bus_eta.py` | Find a bus within a time window and set an Android alarm |
| `hk_bus_common.py` | Shared library (ETA parsing, schedule search, argument parsers) |
| `bus_alarm_lib.py` | Android alarm library (`am start` command builder and executor) |

---

## bus_route_info.py

### Usage

```
python bus_route_info.py [-route_id ROUTE_ID] [-seq N] [-detail]
                         [-search_schedule_from HH:MM] [-search_schedule_to HH:MM]
                         [-search_schedule_tz TZ]
```

### Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `-route_id` | No | `81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE` | Full route ID string in `+`-delimited format (e.g. `81+1+ORIG+DEST`). |
| `-seq` | No | *(all stops)* | Show only stop N (1-based). Required by `-detail` and the schedule search flags. |
| `-detail` | No | off | Print all raw fields returned by `getEtas()` in a PowerShell-style list below each stop row. Requires `-seq`. |
| `-search_schedule_from` | No | — | Start of the time window to search for a bus schedule (`HH:MM`). Must be paired with `-search_schedule_to`. Requires `-seq`. |
| `-search_schedule_to` | No | — | End of the time window. Must be later than `-search_schedule_from`. Requires `-seq`. |
| `-search_schedule_tz` | No | `+08:00` | Timezone for the schedule search window. Accepts `local` or a fixed offset like `+09:00` / `-05:00`. |

#### Parameter dependencies

```
-detail                    → requires -seq
-search_schedule_from      → requires -seq, must be paired with -search_schedule_to
-search_schedule_to        → requires -seq, must be paired with -search_schedule_from
-search_schedule_tz        → only meaningful when schedule search flags are set
```

### Output

#### Route header

```
============================================================
Route ID : 81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE
Origin   : HIGH SPEED RAIL WEST KOWLOON STATION  /  西九龍站
Dest     : WO CHE  /  禾車
============================================================
```

#### Stop table

Columns are dynamically sized to the widest value in each column (plus 3 spaces of padding). Chinese characters are counted as double-width for correct terminal alignment.

```
  Seq   Co      Stop ID        English                           中文              ETA
  --------------------------------------------------------------------------------------
  1     kmb     E10101...      West Kowloon Station Bus Terminus 西九龍站巴士總站   2026-04-21T14:32+08:00 (5m),  2026-04-21T14:50+08:00 (23m)
```

**ETA format:** `YYYY-MM-DDTHH:MM±HH:MM (Nm)` — ISO 8601 timestamp followed by minutes from now. Only upcoming arrivals (≥ 0 min) are shown. `—` if none available.

When a schedule search is active and a match is found, the matching entry is marked with ` *`:

```
  2026-04-21T14:32+08:00 (5m) *,  2026-04-21T14:50+08:00 (23m)
```

#### Detail block (`-detail`)

Printed below each stop row, showing all raw fields from `getEtas()`:

```
  ETA entry 1 of 2
  ------------------
  eta       : 2026-04-21T14:32:00+08:00
  remark    : {'zh': '原定班次', 'en': 'Scheduled Bus'}
  co        : kmb
```

When `-search_schedule_from`/`-search_schedule_to` are also set:
- **Match found** — only the matched entry is shown, labelled `Matched schedule`.
- **No match** — replaced with: `No bus schedule found between 14:00 and 15:00 (tz +08:00).`

### Examples

```bash
# All stops for the default route
python bus_route_info.py

# All stops for a specific route
python bus_route_info.py -route_id "81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE"

# Single stop
python bus_route_info.py -seq 3

# Single stop with full ETA field detail
python bus_route_info.py -seq 3 -detail

# Find the latest bus in a time window (default tz +08:00)
python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00

# Same with Japan Standard Time
python bus_route_info.py -seq 3 -search_schedule_from 15:00 -search_schedule_to 16:00 -search_schedule_tz +09:00

# Same with system local timezone
python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00 -search_schedule_tz local

# Time window search with full detail of the matched entry
python bus_route_info.py -seq 3 -search_schedule_from 14:00 -search_schedule_to 15:00 -detail
```

---

## set_alarm_with_bus_eta.py

Finds the latest bus ETA within a time window for a given stop, then sets (or previews) an Android alarm for it via the Activity Manager (`am`) command. Intended to be run from Termux on an Android device.

### Usage

```
python set_alarm_with_bus_eta.py -seq N
    -search_schedule_from HH:MM -search_schedule_to HH:MM
    (-add_alarm | -add_alarm_debug)
    [-route_id ROUTE_ID]
    [-search_schedule_tz TZ]
    [-alarm_label LABEL]
    [-alarm_default_time HH:MM]
    [-alarm_minutes_before_schedule N]
```

### Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `-seq` | Yes | — | Stop number (1-based) to query. |
| `-search_schedule_from` | Yes | — | Start of the time window (`HH:MM`). |
| `-search_schedule_to` | Yes | — | End of the time window (`HH:MM`). Must be later than `-search_schedule_from`. |
| `-add_alarm` | Yes* | — | Execute the `am` command to set the Android alarm. Mutually exclusive with `-add_alarm_debug`. |
| `-add_alarm_debug` | Yes* | — | Print the `am` command to stdout without executing it. Mutually exclusive with `-add_alarm`. |
| `-route_id` | No | `81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE` | Full route ID string. |
| `-search_schedule_tz` | No | `+08:00` | Timezone for the search window and default alarm time. Accepts `local` or a fixed offset like `+09:00` / `-05:00`. |
| `-alarm_label` | No | `Bus schedule` | Label shown on the Android clock alarm. |
| `-alarm_default_time` | No | — | Fallback alarm time (`HH:MM`) used when no bus schedule is found in the search window. Uses the same timezone as `-search_schedule_tz`. If omitted, the script exits with an error when no schedule is found. |
| `-alarm_minutes_before_schedule` | No | `0` | Set the alarm this many minutes before the found schedule time. |

*Exactly one of `-add_alarm` / `-add_alarm_debug` is required.

### Alarm time resolution

1. If a bus schedule is found in the window: `alarm_time = schedule_time − alarm_minutes_before_schedule`
2. If no schedule is found and `-alarm_default_time` is set: `alarm_time = alarm_default_time`
3. If no schedule is found and `-alarm_default_time` is not set: exits with an error.
4. In all cases: if the computed `alarm_time` is less than 2 minutes from now, it is clamped to `now + 2 minutes`.

### Examples

```bash
# Set an alarm for the latest bus between 14:00–15:00 at stop 3
python set_alarm_with_bus_eta.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -add_alarm

# Dry-run: print the am command without executing it
python set_alarm_with_bus_eta.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -add_alarm_debug

# Set alarm 10 minutes before the found schedule
python set_alarm_with_bus_eta.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -alarm_minutes_before_schedule 10 \
    -add_alarm

# Fall back to 13:00 if no bus is found in the window
python set_alarm_with_bus_eta.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -alarm_default_time 13:00 \
    -add_alarm

# Custom label and timezone
python set_alarm_with_bus_eta.py -seq 3 \
    -search_schedule_from 15:00 -search_schedule_to 16:00 \
    -search_schedule_tz +09:00 \
    -alarm_label "Take bus 81" \
    -add_alarm_debug
```

---

## Data source

Live ETA data is fetched from the Hong Kong government open-data APIs via `hk-bus-eta`. The endpoint used depends on the bus operator:

| Operator | API endpoint |
|---|---|
| KMB / Long Win | `https://data.etabus.gov.hk/v1/transport/kmb/stop-eta/<STOP_ID>` |
| CTB (Citybus) | `https://rt.data.gov.hk/v2/transport/citybus/eta/<STOP_ID>/<ROUTE>/1` |
| NLB | `https://rt.data.gov.hk/v1/transport/nlb/stop.php?action=estimatedArrivals` |
| MTR | `https://rt.data.gov.hk/v1/transport/mtr/getSchedule.php` |
| GMB (green minibus) | `https://data.etagmb.gov.hk/eta/stop-seq/<STOP_ID>/<ROUTE_ID>` |

Route and stop metadata is loaded once at startup from the pre-compiled JSON at [`hkbus.github.io/hk-bus-crawling`](https://hkbus.github.io/hk-bus-crawling/routeFareList.min.json).

## Finding a route ID

Route IDs follow the pattern `<BUS_NUMBER>+<SERVICE_TYPE>+<ORIGIN>+<DESTINATION>`. Run either script with an invalid route to see suggestions:

```bash
python bus_route_info.py -route_id "81"
# Route not found: '81'
# Available routes containing that bus number:
#   81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE
#   81+1+WO CHE+HIGH SPEED RAIL WEST KOWLOON STATION
#   ...
```
