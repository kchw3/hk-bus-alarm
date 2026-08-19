# ETA format: what was fixed, and the one decision still open

## Applied

The ETA column in `bus_route_info.py` truncated seconds off a timestamp the
`-detail` block printed in full, and entries near their arrival time vanished
from the column while still appearing in that block. Both are fixed:

- **Seconds are shown.** `format_eta_stamp()` (`hk_bus_common.py`) renders
  `2026-04-21T14:32:47+08:00 (5m)`. It backs the `bus_route_info.py` table and
  the calendar event's *All upcoming ETAs* listing.
- **The past-entry cutoff is exact to the second.** `upcoming_etas()`
  (`hk_bus_common.py`) compares raw seconds. Previously
  `int((eta_dt - now).total_seconds() / 60) < 0` truncated toward zero, granting
  a silent grace window of up to 60 s — an ETA 30 s in the past survived and
  printed as `(0m)`. Combined with the operator re-estimating on every poll,
  that is what made entries flicker in and out near their arrival time.
- **The ` *` marker survives a stale match.** `find_schedule()` never filtered on
  "not in the past", so a match more than a minute old was dropped by the column
  filter and its marker disappeared, while the detail block below still printed
  the bus under `Matched schedule` — an unexplained mismatch inside one run's
  output. `upcoming_etas(..., keep=found)` exempts that one entry; it now lists
  with negative minutes and keeps its marker:
  `2026-04-21T14:32:47+08:00 (-3m) *`.
- **`add_bus_schedule_to_calendar.py` got the same three changes**, replacing its
  own duplicate copies of the filtering and formatting.

**Deliberately unchanged:** `format_eta_entry()` stays at minute precision. It
produces the alarm scripts' `Found schedule:` line, the calendar's `Matched`
line, and the logged `bus_schedule` column, and `-add_alarm_ha` feeds
`FOUND:HH:MM` into a Home Assistant automation that depends on that format.
`set_alarm_with_bus_eta.py`, `bus_log_lib.py`, `backfill_log.py` and `web/` were
not touched.

## Still open: should `find_schedule()` return a bus that already left?

`find_schedule()` (`hk_bus_common.py:65-80`) only checks the window — it has no
"not in the past" test, by design shared across all three scripts. In
`set_alarm_with_bus_eta.py` this has a visible consequence: when the only
in-window entry is a departed bus, it is still selected
(`set_alarm_with_bus_eta.py:198`), the `_MIN_ALARM_LEAD_MINUTES` clamp then
rescues the alarm (`set_alarm_with_bus_eta.py:244-255`), and the run logs
`reason = "...; clamped to now+2m"` against a `bus_schedule` timestamp in the
past. The alarm itself is sane; the logged pairing is misleading, and the chart's
"clamped" points partly reflect this.

The question is whether that is correct behaviour — arguably yes, since a bus you
are about to miss is still the bus you wanted — or whether `find_schedule()`
should take an optional `not_before` so callers can ask for genuinely catchable
departures only.

**Since this was written, a second caller now depends on the current
behaviour.** `track_bus_arrival.py` keeps polling an overdue bus precisely
*because* `find_schedule()` still returns it, and that is what makes the "last
sighting" upper bound observable — the entry leaving the feed is the tracker's
stop condition. Any `not_before` filter must therefore be opt-in per call, never
a change to the default, or arrival tracking stops working.

Deferred because it changes *which bus gets picked*, not how one is displayed,
and it would alter `set_alarm_with_bus_eta.py` behaviour. Deciding it needs a
look at real logged data: how often does `81.log` show a `clamped to now+2m`
reason paired with a non-empty `bus_schedule` in the past?

## Verification

```bash
python bus_route_info.py -seq 3 -detail
```
The time in the ETA column matches the raw `eta` field in the detail block
character-for-character.

```bash
python bus_route_info.py -seq 3 \
  -search_schedule_from 14:00 -search_schedule_to 15:00 -detail
```
Anything printed as `Matched schedule` also carries a ` *` in the column above.

```bash
python set_alarm_with_bus_eta.py -seq 3 \
  -search_schedule_from 14:00 -search_schedule_to 15:00 -add_alarm_ha
```
Still prints exactly `FOUND:HH:MM` or `NOT_FOUND:HH:MM` — the regression guard
for the Home Assistant automation.
