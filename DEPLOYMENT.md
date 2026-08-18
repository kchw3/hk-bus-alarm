# Deployment guide

Step-by-step setup for the schedule history chart in `web/`: a Cloudflare Worker that
stores uploaded records in D1, serves a public no-login chart, and redeploys itself
whenever `web/` changes on `main`.

Target for this repository:

| | Value |
|---|---|
| Worker name | `hk-bus-alarm-chart` |
| Chart | <https://hk-bus-alarm-chart.iteneti.top/> |
| Ingest endpoint | `https://hk-bus-alarm-chart.iteneti.top/api/ingest` |
| Data endpoint | `https://hk-bus-alarm-chart.iteneti.top/api/data.json` |
| D1 database | `hk-bus-alarm` |
| Repository | `kchw3/hk-bus-alarm`, Worker in the `web/` subdirectory |

Everything below can be done **in the browser** — no `wrangler login` required, which
matters if you work in a container where the OAuth callback on `localhost:8976`
cannot be reached (see [Troubleshooting](#troubleshooting)). CLI equivalents are
given for each step in case you prefer them.

---

## Prerequisites

- A Cloudflare account (free plan is enough).
- The `iteneti.top` zone active on that same account — Custom Domains only work for
  zones Cloudflare hosts. Check under **Websites** in the dashboard.
- No existing DNS record for `hk-bus-alarm-chart.iteneti.top`. Cloudflare creates it
  for you, and refuses if a CNAME is already there.
- Push access to `kchw3/hk-bus-alarm`.

---

## Step 1 — Create the D1 database

**Dashboard:** *Storage & Databases → D1 SQL Database → Create database*, name it
`hk-bus-alarm`. Copy the **Database ID** shown on its page.

**CLI:** `cd web && npx wrangler d1 create hk-bus-alarm`

Put the id into [`web/wrangler.jsonc`](web/wrangler.jsonc):

```jsonc
"d1_databases": [
  {
    "binding": "DB",
    "database_name": "hk-bus-alarm",
    "database_id": "c9770e52-2640-4e87-8939-93221bea7ae2"
  }
]
```

> Already filled in for this repository. The id is not a secret — it is safe in git.

## Step 2 — Create the table

**Dashboard:** open the database → **Console** tab → paste the contents of
[`web/schema.sql`](web/schema.sql) → **Execute**.

**CLI:** `npx wrangler d1 execute hk-bus-alarm --remote --file ./schema.sql`

Verify with `SELECT name FROM sqlite_master WHERE type='table';` — you should see
`schedule_log`.

## Step 3 — Connect the repository (auto-deploy)

This is Cloudflare **Workers Builds**: every push to `main` that touches the project
builds and deploys automatically.

1. Dashboard → **Workers & Pages** → **Create application** → **Import a repository**.
2. Authorise the Cloudflare GitHub App and pick `kchw3/hk-bus-alarm`.
3. Configure the build:

   | Field | Value |
   |---|---|
   | Worker name | `hk-bus-alarm-chart` |
   | Git branch | `main` |
   | Root directory | `web` |
   | Build command | *(leave empty — nothing to build)* |
   | Deploy command | `npx wrangler deploy` |
   | Non-production branch deploy command | `npx wrangler versions upload` *(default)* |

   The Worker name **must** match `"name"` in `web/wrangler.jsonc`, or builds fail.
   `Root directory: web` is what makes Wrangler find `wrangler.jsonc`, `src/`, and
   `public/`; dependencies install from `web/package-lock.json` automatically.

4. Save and deploy. The first build runs immediately.

If the Worker already exists from a manual `wrangler deploy`, connect it instead via
*Workers & Pages → hk-bus-alarm-chart → Settings → Builds → Connect*.

**Deploying by hand instead** (no Git integration): `cd web && npx wrangler deploy`.

## Step 4 — Set the ingest token

The token authorises uploads. Generate one:

```bash
openssl rand -base64 32
```

**Dashboard:** *Workers & Pages → hk-bus-alarm-chart → Settings → Variables and
Secrets → Add → type **Secret**, name `INGEST_TOKEN`*, paste the value.

**CLI:** `npx wrangler secret put INGEST_TOKEN` (prompts, so the value stays out of
your shell history).

Secrets live on the Worker, not in the repository — they survive redeploys, so this
is a one-time step. Build variables in Workers Builds are a different thing: those
are only visible during the build and are **not** what the Worker reads at runtime.

Keep the same value on the device as `$BUS_LOG_TOKEN` (Step 6).

## Step 5 — Custom domain

[`web/wrangler.jsonc`](web/wrangler.jsonc) already declares it:

```jsonc
"routes": [
  { "pattern": "hk-bus-alarm-chart.iteneti.top", "custom_domain": true }
]
```

On the next deploy Cloudflare creates the DNS record and issues the certificate. A
Custom Domain routes **every** path on that hostname to the Worker, so no code
changes are needed — the page fetches its data relatively.

Confirm under *Workers & Pages → hk-bus-alarm-chart → Settings → Domains & Routes*.
Certificate issuance can take a few minutes on first setup.

The `*.workers.dev` URL keeps working alongside it. Once the custom domain is
verified, you can retire it by adding `"workers_dev": false` next to `"routes"`.

## Step 6 — Point the device at it

On the device running the alarm (Termux):

```bash
export BUS_LOG_TOKEN='<the token from Step 4>'

python set_alarm_with_bus_eta.py -seq 3 \
    -search_schedule_from 06:00 -search_schedule_to 08:00 \
    -alarm_minutes_before_schedule 30 \
    -log_file ~/bus_alarm.log \
    -log_url https://hk-bus-alarm-chart.iteneti.top/api/ingest \
    -add_alarm
```

Always pass `-log_file` alongside `-log_url`: the CSV row is written first, so a
failed upload leaves a replayable copy. See *Upload failures and recovery* in
[README.md](README.md).

## Step 7 — Load existing history

```bash
python backfill_log.py ~/bus_alarm.log \
    -log_url https://hk-bus-alarm-chart.iteneti.top/api/ingest
```

Ingest upserts on `(timestamp, route_id)`, so re-running this is harmless — use it
any time an upload failed.

---

## Verification

```bash
# 1. Unauthenticated ingest is rejected
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST https://hk-bus-alarm-chart.iteneti.top/api/ingest        # expect 401

# 2. Public read works with no auth
curl -s https://hk-bus-alarm-chart.iteneti.top/api/data.json | head -c 200

# 3. Authenticated ingest works
curl -s -X POST https://hk-bus-alarm-chart.iteneti.top/api/ingest \
  -H "Authorization: Bearer $BUS_LOG_TOKEN" \
  -d '{"timestamp":"2026-01-01T06:00:00+08:00","route_id":"test","bus_schedule":"2026-01-01T07:00+08:00 (60m)","eta_iso":"2026-01-01T07:00:00+08:00","alarm_time":"06:30","reason":"smoke test"}'
```

Then open <https://hk-bus-alarm-chart.iteneti.top/> in a private window — it must
load with no login. Remove the smoke-test row afterwards from the D1 console:

```sql
DELETE FROM schedule_log WHERE route_id = 'test';
```

Live request logs: *Workers & Pages → hk-bus-alarm-chart → Logs*, or `npx wrangler tail`.

---

## Troubleshooting

**`wrangler login` fails with `ECONNREFUSED …:8976`.** Wrangler opens that callback
port only while `wrangler login` is running and waiting; once the command exits, the
port closes. It also cannot be reached through a proxy — the OAuth `redirect_uri` is
hardcoded to `http://localhost:8976/oauth/callback` and must resolve inside the same
machine. Options: do everything in the dashboard as above; or create an API token
(*My Profile → API Tokens → Edit Cloudflare Workers* template, plus **D1 Edit**, plus
**DNS Edit** on `iteneti.top` if you deploy the custom domain from the CLI) and
`export CLOUDFLARE_API_TOKEN=…`; or, with `wrangler login` still running, copy the
failed `localhost:8976/oauth/callback?code=…&state=…` URL from the browser and
`curl` it from inside the machine.

**Build fails: Worker name mismatch.** The dashboard Worker name and `"name"` in
`web/wrangler.jsonc` must be identical (`hk-bus-alarm-chart`).

**Build fails: cannot find `wrangler.jsonc`.** Root directory is not set to `web`.

**Deploy fails on the custom domain.** The zone must be active on the same account,
and `hk-bus-alarm-chart.iteneti.top` must not already have a DNS record.

**Ingest returns 403 with `error code: 1010`.** Cloudflare's Browser Integrity
Check blocked the request at the edge, before the Worker — it rejects urllib's
default `Python-urllib/3.x` user agent. `bus_log_lib.py` therefore sends its own
`User-Agent` (`hk-bus-alarm/1.0`), which passes. Zone security features like this
apply to the custom domain but not to `*.workers.dev`, so this only shows up after
Step 5. If a stricter WAF rule blocks uploads later, add a Configuration Rule
(*Rules → Configuration Rules*) that turns Browser Integrity Check off for
`hk-bus-alarm-chart.iteneti.top/api/*`, or a WAF custom rule with the **Skip**
action for that path.

**Ingest returns 401.** `$BUS_LOG_TOKEN` on the device and the `INGEST_TOKEN` secret
on the Worker differ. Re-set the secret and retry; the device keeps the row locally,
so replay it with `backfill_log.py` afterwards.

**Ingest returns 500 "ingest is not configured".** The `INGEST_TOKEN` secret is
missing on the Worker (Step 4).

**Chart says "No schedules logged yet".** Nothing with a non-empty `eta_iso` has been
ingested. Rows logged when no bus was found in the window are stored but not plotted.

**`wrangler dev` dies with a tcmalloc / mmap error.** Some containers cannot run
`workerd` at all, which also breaks `wrangler d1 execute --local`. Use the dashboard
D1 console and deploy through Workers Builds instead.
