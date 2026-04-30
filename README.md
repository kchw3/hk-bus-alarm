# HK Bus Alarm

A pair of command-line tools to query Hong Kong bus route stops and live ETAs, with the ability to set an Android alarm or create a Google Calendar event for a found bus schedule.

## Requirements

- Python 3.10+
- [`hk-bus-eta`](https://pypi.org/project/hk-bus-eta/) library
- Android device with Termux (required only for `set_alarm_with_bus_eta.py -add_alarm`)
- Google Calendar API libraries (required only for `google_calendar_lib.py`)

```bash
pip install hk-bus-eta
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

## Files

| File | Purpose |
|---|---|
| `bus_route_info.py` | Query and display route stops and live ETAs |
| `set_alarm_with_bus_eta.py` | Find a bus within a time window and set an Android alarm |
| `hk_bus_common.py` | Shared library (ETA parsing, schedule search, argument parsers) |
| `bus_alarm_lib.py` | Android alarm library (`am start` command builder and executor) |
| `google_calendar_lib.py` | Google Calendar library (OAuth2 auth, event creation) |
| `add_bus_schedule_to_calendar.py` | CLI to find a bus schedule and create a Google Calendar event |

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
    (-add_alarm | -add_alarm_debug | -add_alarm_ha)
    [-route_id ROUTE_ID]
    [-search_schedule_tz TZ]
    [-alarm_label LABEL]
    [-alarm_default_time HH:MM]
    [-alarm_minutes_before_schedule N]
    [-log_file PATH]
```

### Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `-seq` | Yes | — | Stop number (1-based) to query. |
| `-search_schedule_from` | Yes | — | Start of the time window (`HH:MM`). |
| `-search_schedule_to` | Yes | — | End of the time window (`HH:MM`). Must be later than `-search_schedule_from`. |
| `-add_alarm` | Yes* | — | Execute the `am` command to set the Android alarm. Mutually exclusive with `-add_alarm_debug` and `-add_alarm_ha`. |
| `-add_alarm_debug` | Yes* | — | Print the `am` command to stdout without executing it. Mutually exclusive with `-add_alarm` and `-add_alarm_ha`. |
| `-add_alarm_ha` | Yes* | — | Home Assistant mode: print `FOUND:HH:MM` if a schedule was found, or `NOT_FOUND:HH:MM` using the fallback alarm time. No other output is produced. Mutually exclusive with `-add_alarm` and `-add_alarm_debug`. |
| `-route_id` | No | `81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE` | Full route ID string. |
| `-search_schedule_tz` | No | `+08:00` | Timezone for the search window and default alarm time. Accepts `local` or a fixed offset like `+09:00` / `-05:00`. |
| `-alarm_label` | No | `Bus schedule` | Label shown on the Android clock alarm. |
| `-alarm_default_time` | No | — | Fallback alarm time (`HH:MM`) used when no bus schedule is found in the search window. Uses the same timezone as `-search_schedule_tz`. If omitted and no schedule is found, the alarm is set to `now + 2 minutes`. |
| `-alarm_minutes_before_schedule` | No | `0` | Set the alarm this many minutes before the found schedule time. |
| `-log_file` | No | — | Path to a CSV log file. Each run appends one row with `timestamp`, `route_id`, `bus_schedule`, `alarm_time`, and `reason`. The header is written automatically when the file is new or empty. Logging is disabled if omitted. |

*Exactly one of `-add_alarm` / `-add_alarm_debug` / `-add_alarm_ha` is required.

### Alarm time resolution

1. If a bus schedule is found in the window: `alarm_time = schedule_time − alarm_minutes_before_schedule`
2. If no schedule is found and `-alarm_default_time` is set: `alarm_time = alarm_default_time`
3. If no schedule is found and `-alarm_default_time` is not set: `alarm_time = now + 2 minutes`
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

# Home Assistant mode: prints FOUND:14:32 or NOT_FOUND:13:00
python set_alarm_with_bus_eta.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -alarm_default_time 13:00 \
    -add_alarm_ha

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

# Log each run to a CSV file
python set_alarm_with_bus_eta.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -log_file ~/bus_alarm.log \
    -add_alarm
```

---

## google_calendar_lib.py

Library for creating Google Calendar events timed to a bus schedule datetime. Authenticates via OAuth 2.0 using credentials downloaded from Google Cloud Console.

### One-time Google Cloud setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project and enable the **Google Calendar API**.
3. Under *APIs & Services → Credentials*, create an **OAuth 2.0 Client ID** (application type: *Desktop app*).
4. Download the client-secrets file and save it as `credentials.json` (or any path you pass to the function).
5. On first use the script prints an authorisation URL to the terminal. Visit it in any browser and approve access. Your browser will then try to redirect to `https://localhost:8080/` and fail to load — that is expected. Copy the full URL from the browser address bar and paste it into the terminal prompt. The granted token is saved to `token.json` and reused (with silent refresh) on all subsequent calls.

### Functions

#### `get_calendar_service(credentials_file, token_file)`

Authenticates and returns a Google Calendar API service object. All other functions accept this object so you can authenticate once and make multiple calls.

| Parameter | Default | Description |
|---|---|---|
| `credentials_file` | `credentials.json` | Path to the OAuth 2.0 client-secrets JSON from Google Cloud Console. |
| `token_file` | `token.json` | Path where the granted OAuth token is cached between runs. |

#### `create_calendar_event(service, calendar_id, summary, start_dt, *, end_dt, duration_minutes, description, location)`

Low-level function that creates a single event on the specified calendar.

| Parameter | Default | Description |
|---|---|---|
| `service` | — | Service object from `get_calendar_service()`. |
| `calendar_id` | — | `'primary'` for the user's default calendar, or the calendar's email-style ID (e.g. `abc123@group.calendar.google.com`). |
| `summary` | — | Event title shown in Google Calendar. |
| `start_dt` | — | Timezone-aware `datetime` for the event start. Naive datetimes are treated as UTC. |
| `end_dt` | `None` | Event end datetime. If omitted, defaults to `start_dt + duration_minutes`. |
| `duration_minutes` | `30` | Event duration when `end_dt` is not supplied. |
| `description` | `""` | Optional event body text. |
| `location` | `""` | Optional location string. |

Returns the created event resource dict (contains `id`, `htmlLink`, etc.).

#### `add_bus_schedule_event(schedule_dt, summary, *, credentials_file, token_file, calendar_id, duration_minutes, description, location)`

Convenience wrapper: authenticates and creates an event in one call.

| Parameter | Default | Description |
|---|---|---|
| `schedule_dt` | — | Timezone-aware `datetime` of the bus arrival (used as event start). |
| `summary` | `Bus schedule` | Event title. |
| `credentials_file` | `credentials.json` | Path to OAuth 2.0 client-secrets JSON. |
| `token_file` | `token.json` | Path to cached OAuth token. |
| `calendar_id` | `primary` | Target calendar (`'primary'` or a specific calendar's ID). |
| `duration_minutes` | `30` | Event duration in minutes. |
| `description` | `""` | Optional event body text. |
| `location` | `""` | Optional location string. |

Prints the URL of the created event and returns the event resource dict.

### Usage example

```python
from google_calendar_lib import get_calendar_service, create_calendar_event, add_bus_schedule_event
from datetime import datetime, timezone, timedelta

hkt = timezone(timedelta(hours=8))
schedule_dt = datetime(2026, 4, 21, 14, 32, tzinfo=hkt)

# One-liner convenience wrapper (authenticates internally)
event = add_bus_schedule_event(
    schedule_dt,
    summary="Take bus 81",
    calendar_id="primary",
    duration_minutes=60,
    description="Bus from West Kowloon to Wo Che",
)

# Or use the lower-level functions if you need multiple events
service = get_calendar_service("credentials.json", "token.json")
event = create_calendar_event(
    service,
    calendar_id="abc123@group.calendar.google.com",
    summary="Take bus 81",
    start_dt=schedule_dt,
    duration_minutes=60,
    description="Bus from West Kowloon to Wo Che",
    location="West Kowloon Station Bus Terminus",
)
print(event["htmlLink"])
```

---

## add_bus_schedule_to_calendar.py

Finds the latest bus ETA within a time window for a given stop, then creates a Google Calendar event timed to that schedule. Supports a debug mode (`-add_event_debug`) that prints the full event details to stdout without calling the Calendar API, allowing offline testing without credentials.

### Usage

```
python add_bus_schedule_to_calendar.py -seq N
    -search_schedule_from HH:MM -search_schedule_to HH:MM
    (-add_event | -add_event_debug)
    [-route_id ROUTE_ID]
    [-search_schedule_tz TZ]
    [-calendar_id ID]
    [-credentials_file PATH]
    [-token_file PATH]
    [-duration_minutes N]
```

### Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `-seq` | Yes | — | Stop number (1-based) to query. |
| `-search_schedule_from` | Yes | — | Start of the time window (`HH:MM`). |
| `-search_schedule_to` | Yes | — | End of the time window (`HH:MM`). Must be later than `-search_schedule_from`. |
| `-add_event` | Yes* | — | Create the event in Google Calendar. Mutually exclusive with `-add_event_debug`. |
| `-add_event_debug` | Yes* | — | Print the event details to stdout without calling the Calendar API. Mutually exclusive with `-add_event`. |
| `-route_id` | No | `81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE` | Full route ID string. |
| `-search_schedule_tz` | No | `+08:00` | Timezone for the search window. Accepts `local` or a fixed offset like `+09:00` / `-05:00`. |
| `-calendar_id` | No | `primary` | Target Google Calendar ID. Use `'primary'` for your default calendar, or a calendar's email-style ID (e.g. `abc123@group.calendar.google.com`). |
| `-credentials_file` | No | `credentials.json` | Path to the OAuth 2.0 client-secrets JSON from Google Cloud Console. |
| `-token_file` | No | `token.json` | Path to the cached OAuth token file. |
| `-duration_minutes` | No | `15` | Calendar event duration in minutes. |

*Exactly one of `-add_event` / `-add_event_debug` is required.

### Event start time

- **Schedule found** — event start is set to the matched bus schedule time.
- **No schedule found** — event start is set to the script's run time (now). The event is still created; the description notes that no schedule was found in the window.

### Event content

The event is always created with the title **`Bus schedule`**. The description includes all information equivalent to what `bus_route_info.py` prints:

- Route header (Route ID, Origin, Dest)
- Stop details (sequence number, operator, stop ID, English and Chinese names)
- Search window and matched schedule timestamp (or a note that none was found)
- All upcoming ETAs, with the matched entry marked `*`
- Raw fields of the matched ETA entry (omitted when no schedule was found)

### Debug mode output

`-add_event_debug` prints the event without contacting Google. Useful for verifying the schedule search and description before committing to a live API call:

```
Found schedule: 2026-04-21T14:32+08:00 (5m)

Event details (not submitted to Google Calendar):
  Title    : Bus schedule
  Start    : 2026-04-21T14:32:00+08:00
  End      : 2026-04-21T14:47:00+08:00
  Calendar : primary

Description:
--------------------------------------------------
==================================================
Route ID : 81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE
Origin   : HIGH SPEED RAIL WEST KOWLOON STATION  /  西九龍站
Dest     : WO CHE  /  禾車
==================================================

Stop     : 3 of 15
Operator : kmb
Stop ID  : E10101XXXX
English  : West Kowloon Station Bus Terminus
Chinese  : 西九龍站巴士總站

Search window : 14:00–15:00  (tz +08:00)
Matched       : 2026-04-21T14:32+08:00 (5m)

All upcoming ETAs:
  2026-04-21T14:32+08:00 (5m)  *
  2026-04-21T14:50+08:00 (23m)

Matched schedule (raw fields):
  eta    : 2026-04-21T14:32:00+08:00
  remark : {'zh': '原定班次', 'en': 'Scheduled Bus'}
  co     : kmb
--------------------------------------------------
```

### Examples

```bash
# Dry-run: print event details without calling the Calendar API
python add_bus_schedule_to_calendar.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -add_event_debug

# Create the event in the primary calendar
python add_bus_schedule_to_calendar.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -add_event

# Create the event in a specific calendar
python add_bus_schedule_to_calendar.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -calendar_id "abc123@group.calendar.google.com" \
    -add_event

# Custom credentials file, 60-minute event, dry-run
python add_bus_schedule_to_calendar.py -seq 3 \
    -search_schedule_from 14:00 -search_schedule_to 15:00 \
    -credentials_file ~/my_creds.json -duration_minutes 60 \
    -add_event_debug
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
