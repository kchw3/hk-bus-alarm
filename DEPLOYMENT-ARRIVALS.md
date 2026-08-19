# Deployment guide — arrival tracking

Setup for the arrival tracking feature: `track_bus_arrival.py` on the device, an
`arrival_track` table in D1, and the chart at `/arrivals/`.

This rides on the infrastructure [DEPLOYMENT.md](DEPLOYMENT.md) already set up.
Read that first if the schedule chart is not live yet.

| | Value |
|---|---|
| Chart | <https://hk-bus-alarm-chart.iteneti.top/arrivals/> |
| Ingest endpoint | `https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest` |
| Data endpoint | `https://hk-bus-alarm-chart.iteneti.top/api/arrivals/data.json` |
| D1 database | `hk-bus-alarm` (the existing one) |
| D1 table | `arrival_track` (new) |

## What you do **not** need to do

Almost everything is inherited, so resist the urge to create new resources:

- **No new Worker.** `/arrivals/` and `/api/arrivals/*` are served by the same
  `hk-bus-alarm-chart` Worker.
- **No new secret.** The arrivals ingest endpoint authenticates with the same
  `INGEST_TOKEN` as `/api/ingest`, and the device uses the same
  `$BUS_LOG_TOKEN`.
- **No new domain or route.** The Custom Domain covers *every* path on
  `hk-bus-alarm-chart.iteneti.top`.
- **No new deploy pipeline.** Workers Builds already redeploys on every push to
  `main` that touches `web/`.
- **Not Cloudflare Pages.** Despite the static page, this project is a Worker
  with [Static Assets](web/wrangler.jsonc) — `web/public/arrivals/` ships as part
  of the same Worker deploy. There is no Pages project to configure.

## Step 1 — Create the table

The only genuinely new step. `web/schema.sql` now contains `arrival_track`
alongside `schedule_log`, and every statement is `IF NOT EXISTS`, so re-applying
the whole file is safe and leaves existing schedule data untouched.

**Dashboard:** *Storage & Databases → D1 → `hk-bus-alarm` → Console*, paste the
contents of [`web/schema.sql`](web/schema.sql), **Execute**.

**CLI:** `cd web && npx wrangler d1 execute hk-bus-alarm --remote --file ./schema.sql`

Verify:

```sql
SELECT name FROM sqlite_master WHERE type='table';
-- expect: schedule_log, arrival_track
```

## Step 2 — Deploy the Worker and page

Push to `main`. Workers Builds picks up the new routes in `web/src/index.js` and
the new assets under `web/public/arrivals/`. Deploying by hand instead:
`cd web && npx wrangler deploy`.

Confirm <https://hk-bus-alarm-chart.iteneti.top/arrivals/> loads and says
"No arrival tracks recorded yet."

## Step 3 — Run the tracker on the device

Start it roughly an hour before the bus you want to track. It polls every 60s,
speeding to every 15s once the ETA is within 3 minutes, and exits when the bus
leaves the operator feed.

```bash
export BUS_LOG_TOKEN='<the same token as the schedule chart>'

python track_bus_arrival.py -seq 8 \
    -search_schedule_from 13:40 -search_schedule_to 13:50 \
    -log_file ~/bus_track.log \
    -log_url https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest
```

**Keep the window narrow.** The bus is selected with `find_schedule()`, which
returns the *latest* ETA inside the window. A window wide enough to admit a
second bus can move the target mid-track. The tracker warns on stderr if the
selected ETA jumps forward by more than 10 minutes, which is the signature of
exactly that.

**Android will suspend a long-running process.** From Termux:

```bash
termux-wake-lock                       # release with termux-wake-unlock
nohup python track_bus_arrival.py ... > ~/track.out 2>&1 &
```

`tmux` works equally well. Without a wake lock the process is throttled or
frozen once the screen goes off, and the polls stop.

**Always pass `-log_file` alongside `-log_url`.** The CSV row is written before
the upload is attempted, so a failed upload leaves a replayable copy:

```bash
python backfill_track_log.py ~/bus_track.log \
    -log_url https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest
```

Ingest upserts on `(session_id, eta_iso)` and widens the observation bracket
rather than overwriting it, so replaying is always safe — including rows that
already made it through.

A run stopped with Ctrl-C or `kill` still flushes its final record, so the "last
sighting" is preserved. Only `kill -9` loses it; the ETA history is already
durable either way.

## Step 4 — Schedule it to run daily

One run tracks one bus, and exits on its own once that bus leaves the feed. So a
daily job is simply "start it ahead of the bus you care about".

### Choose the lead time

Start about **60 minutes ahead**. Measured against a feed that begins publishing
the bus 50 minutes out and revises it six times:

| Lead | API calls | Ran for | Estimates captured | First estimate |
|---|---|---|---|---|
| 90m | 115 | 99m | 6 | 13:42:00 |
| **60m** | **85** | **69m** | **6** | **13:42:00** |
| 30m | 55 | 39m | 5 | 13:43:30 |
| 10m | 35 | 19m | 3 | 13:45:30 |

More than 60m buys nothing — you cannot observe estimates the operator has not
published yet, so the extra polls all land in the acquire phase. Less than 60m
starts losing the *earliest* estimate, and since that is the lower bound of the
bracket, a short lead quietly shortens the interval rather than measuring it. The
final estimate and the last sighting are identical at every lead time; only the
lower bound is at risk.

### Cap the runtime

The 180-minute default is a safety net for interactive use, not a good value for
a cron job. On a day the route does not run — Sunday, a holiday timetable, a
typhoon — the tracker never acquires and polls uselessly until the cap:

| `-max_runtime_minutes` | Wasted calls on a no-show day |
|---|---|
| 180 (default) | 180 |
| 90 | 90 |

With a 60m lead and a bus that clears the feed ~10 minutes past its ETA, a real
run finishes in about 70 minutes. **`-max_runtime_minutes 90`** leaves ~20
minutes of headroom and halves the cost of a no-show. It exits `not_acquired`,
writes nothing, and leaves no partial session on the chart.

### The same-day constraint

The search window is built from `date.today()` — the **system-local date at the
moment the process starts**. Two consequences for scheduling:

- **The run must start on the same calendar day as the target bus.** Scheduling
  at 23:30 to catch a 00:20 bus builds the window on the wrong day, and it will
  never acquire. There is no way to get ahead of an after-midnight bus; track it
  with a window on its own date instead.
- **The device timezone must match `-search_schedule_tz`** (both `+08:00` by
  default), or the same mismatch appears for runs near midnight.

An early-morning bus is fine: a 06:30 departure needs a 05:30 start, which is
still the same day.

### Cron on Termux

```bash
pkg install termux-services cronie
sv-enable crond
crontab -e
```

```cron
40 12 * * 1-5 termux-wake-lock; cd ~/hk-bus-alarm && \
  python track_bus_arrival.py -seq 8 \
    -search_schedule_from 13:40 -search_schedule_to 13:50 \
    -max_runtime_minutes 90 \
    -log_file ~/bus_track.log \
    -log_url https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest \
    >> ~/track.out 2>&1; termux-wake-unlock
```

- `40 12` is 60 minutes before the 13:40 window opens.
- `1-5` skips weekends, which removes most no-show days outright.
- **The wake lock is not optional.** Without it Android freezes the process once
  the screen goes off and the polls stop silently, mid-track — you get a session
  that ends with no red marker and looks like the bus vanished early.
- Runs are 24 hours apart and capped at 90 minutes, so they can never overlap.

`$BUS_LOG_TOKEN` will not be set inside a cron environment. Either pass
`-log_token` explicitly, or source a file that exports it:

```cron
40 12 * * 1-5 . ~/.bus_env; termux-wake-lock; cd ~/hk-bus-alarm && python track_bus_arrival.py ...
```

Uploads that fail for a missing token still leave every row in `-log_file`;
replay them with `backfill_track_log.py`.

### Confirm it ran

```bash
tail -20 ~/track.out          # the run summary, including the deduced bracket
tail -5  ~/bus_track.log      # the rows it wrote
```

`~/track.out` holds one line per poll, flushed as it goes, so you can also watch
a run live with `tail -f ~/track.out`. Most lines will read `unchanged, no
write` — that is the normal steady state confirming the poll happened and the
ETA had not moved, not a problem. Add `-quiet` to the cron line if you would
rather log only the changes; a 90-minute run writes roughly 80 lines otherwise.

A healthy run ends with `Bus arrived between HH:MM:SS and HH:MM:SS`. Anything
else — `not_acquired`, `timeout`, `interrupted` — is reported explicitly and
means the figures above it are the last observation, not an arrival.

## Verification

```bash
# 1. Unauthenticated ingest is rejected
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest        # expect 401

# 2. Public read works with no auth
curl -s https://hk-bus-alarm-chart.iteneti.top/api/arrivals/data.json | head -c 200

# 3. Authenticated ingest works
curl -s -X POST https://hk-bus-alarm-chart.iteneti.top/api/arrivals/ingest \
  -H "Authorization: Bearer $BUS_LOG_TOKEN" \
  -d '{"session_id":"test|8|2026-01-01T06:00:00+08:00","route_id":"test","seq":8,
       "eta_iso":"2026-01-01T07:00:00+08:00","first_seen":"2026-01-01T06:00:00+08:00",
       "last_seen":"2026-01-01T07:02:00+08:00","polls":40}'
```

Open <https://hk-bus-alarm-chart.iteneti.top/arrivals/> in a private window — it
must load with no login and show the smoke-test point. Remove it afterwards from
the D1 console:

```sql
DELETE FROM arrival_track WHERE route_id = 'test';
```

## Reading the chart

Each tracking session is a vertical column of markers at its ETA's date:

| Marker | Meaning |
|---|---|
| green ✕ | every distinct ETA published as the bus approached; the earliest is the **lower bound** |
| blue ✕ | the last of those — the final estimate before the bus vanished |
| red ✕ | the last poll that still listed the bus, once overdue — the **upper bound** |

The bracket runs from the first green mark to the red one, drawn as a dotted
connector, with every later estimate inside it. A session with **no red marker**
vanished from the feed before its ETA elapsed, so it has no overdue sighting to
bound it — recorded honestly rather than guessed at.

## Purging data

For clearing out POC or test data. **There is no undo.** Deleting from
`arrival_track` does not touch `schedule_log` — the two tables are independent,
so purging tracks never disturbs the alarm history.

Run these in the **dashboard D1 console** (*Storage & Databases → D1 →
`hk-bus-alarm` → Console*), or with
`cd web && npx wrangler d1 execute hk-bus-alarm --remote --command "<SQL>"`.

### Take a backup first

```bash
cd web
npx wrangler d1 export hk-bus-alarm --remote --output ./arrivals-backup.sql
```

The device CSV (`-log_file`) is the other copy — if it is intact,
`backfill_track_log.py` rebuilds the table exactly, because ingest is idempotent.

### Look before you delete

A session is one tracked bus, so counting sessions is usually more meaningful
than counting rows:

```sql
SELECT session_id, route_id, seq,
       COUNT(*)      AS estimates,
       MIN(eta_iso)  AS first_estimate,
       MAX(eta_iso)  AS final_estimate,
       MAX(last_seen) AS last_sighting
FROM arrival_track
GROUP BY session_id
ORDER BY MIN(first_seen_epoch) DESC;
```

### Purge everything

```sql
SELECT COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS rows FROM arrival_track;
DELETE FROM arrival_track;
```

### Purge one tracking session

The unit you almost always want — one session is one bus, one column on the
chart. Copy the id from the query above:

```sql
DELETE FROM arrival_track
WHERE session_id = '81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE|8|2026-08-19T13:00:00+08:00';
```

Deleting only *part* of a session leaves a misleading chart column: drop the
final row and the next-latest estimate silently becomes the blue marker, with a
last sighting that never belonged to it. Delete whole sessions.

### Purge one route, or the smoke test

```sql
DELETE FROM arrival_track WHERE route_id = 'test';
DELETE FROM arrival_track WHERE route_id = '81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE';
```

### Purge one day

`session_id` embeds the run start, but filtering on `eta_epoch` is clearer and
matches the date the chart plots:

```sql
SELECT COUNT(*) FROM arrival_track WHERE substr(eta_iso, 1, 10) = '2026-08-19';
DELETE FROM arrival_track WHERE substr(eta_iso, 1, 10) = '2026-08-19';
```

### Purge older than N days

```sql
-- older than 30 days; eta_epoch is milliseconds, strftime('%s') is seconds
DELETE FROM arrival_track
WHERE eta_epoch < (strftime('%s', 'now') - 30 * 86400) * 1000;
```

### Purge sessions that never resolved

A run killed with `kill -9`, or one that hit `-max_runtime_minutes`, leaves a
session whose last sighting is not past its final ETA — no red marker, no upper
bound. To drop those and keep only completed tracks:

```sql
DELETE FROM arrival_track
WHERE session_id IN (
  SELECT a.session_id FROM arrival_track a
  WHERE a.first_seen_epoch = (
    SELECT MAX(b.first_seen_epoch) FROM arrival_track b WHERE b.session_id = a.session_id
  )
  AND a.last_seen_epoch <= a.eta_epoch
);
```

Run the inner `SELECT a.session_id …` on its own first to see which sessions
that catches.

### Afterwards

- `GET /api/arrivals/data.json` is cached for 60 seconds, so the chart can keep
  showing deleted sessions for up to a minute. Hard-refresh if it looks stale.
- No compaction step is needed — D1 manages its own storage. (Do not reach
  for `VACUUM`: D1 restricts some SQLite statements, and nothing here depends
  on it.)
- **Replaying puts them back.** `backfill_track_log.py` over a CSV that still
  contains those sessions restores them, and `session_id` is deterministic so
  they return under the same ids. Delete or move the local CSV too if you want
  them gone for good.

## Troubleshooting

Everything in [DEPLOYMENT.md § Troubleshooting](DEPLOYMENT.md#troubleshooting)
applies unchanged, in particular:

- **403 with `error code: 1010`** — Cloudflare's Browser Integrity Check
  rejecting a default urllib user agent. Uploads go through `bus_log_lib.py`,
  which sends the project's own `User-Agent`, so this only appears if that is
  bypassed.
- **401** — `$BUS_LOG_TOKEN` on the device and `INGEST_TOKEN` on the Worker
  differ. The rows stay in the CSV; replay with `backfill_track_log.py`.
- **`wrangler dev` / `d1 --local` die with a tcmalloc error** — some containers
  cannot run `workerd`. Use the dashboard D1 console and Workers Builds.

Specific to this feature:

**"No arrival tracks recorded yet" after a run.** The tracker only writes once it
has matched a bus. If it printed *"No bus was ever matched in the search
window"*, the window never contained an entry — check `-seq` and that the run
started while the bus was still ahead.

**Tracking ended immediately.** `find_schedule()` returning nothing *after* the
first match means the bus left the feed. If that happened seconds after start,
the window probably matched a bus that had already gone.

**The final estimate looks like a different bus.** Look for the forward-jump
warning on stderr. Narrow `-search_schedule_from`/`-to` so only one bus falls
inside it.
