/**
 * HK bus schedule chart — Worker.
 *
 * Routes:
 *   POST /api/ingest     Bearer-token protected. Accepts one record or an array.
 *   GET  /api/data.json  Public, no auth. Optional ?days=N and ?route=<route_id>.
 *   everything else      Served from ./public by Workers Static Assets.
 */

// A record is ~250 bytes, so this comfortably holds a full backfill batch.
const MAX_BODY_BYTES = 256 * 1024;
const MAX_RECORDS_PER_REQUEST = 500;
const MAX_ROWS_PER_QUERY = 20000;
const DATA_CACHE_SECONDS = 60;

const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8" };

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/api/ingest") {
      return handleIngest(request, env);
    }
    if (url.pathname === "/api/data.json") {
      return handleData(request, env, url);
    }
    // Static assets are matched before the Worker runs, so anything reaching
    // here is an unknown path.
    return jsonResponse({ error: "not found" }, 404);
  },
};

function jsonResponse(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...extraHeaders },
  });
}

/** Constant-time string comparison, so a bad token leaks no timing signal. */
function tokensMatch(provided, expected) {
  const encoder = new TextEncoder();
  const a = encoder.encode(provided);
  const b = encoder.encode(expected);
  if (a.byteLength !== b.byteLength) {
    return false;
  }
  return crypto.subtle.timingSafeEqual(a, b);
}

function isAuthorized(request, env) {
  const header = request.headers.get("Authorization") || "";
  const prefix = "Bearer ";
  if (!header.startsWith(prefix)) {
    return false;
  }
  return tokensMatch(header.slice(prefix.length), env.INGEST_TOKEN);
}

/**
 * Validate and normalise one incoming record.
 * Returns { record } or { error }.
 */
function normalizeRecord(raw) {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return { error: "record must be a JSON object" };
  }

  const text = (value) => (typeof value === "string" ? value.trim() : "");

  const ts = text(raw.timestamp);
  const routeId = text(raw.route_id);
  if (!ts) {
    return { error: "timestamp is required" };
  }
  if (!routeId) {
    return { error: "route_id is required" };
  }

  const tsEpoch = Date.parse(ts);
  if (Number.isNaN(tsEpoch)) {
    return { error: `timestamp is not a valid ISO 8601 datetime: ${ts}` };
  }

  const etaIso = text(raw.eta_iso);
  let etaEpoch = null;
  if (etaIso) {
    const parsed = Date.parse(etaIso);
    if (Number.isNaN(parsed)) {
      return { error: `eta_iso is not a valid ISO 8601 datetime: ${etaIso}` };
    }
    etaEpoch = parsed;
  }

  return {
    record: {
      ts,
      ts_epoch: tsEpoch,
      route_id: routeId,
      bus_schedule: text(raw.bus_schedule),
      eta_iso: etaIso,
      eta_epoch: etaEpoch,
      alarm_time: text(raw.alarm_time),
      reason: text(raw.reason),
    },
  };
}

async function handleIngest(request, env) {
  if (request.method !== "POST") {
    return jsonResponse({ error: "method not allowed" }, 405, { Allow: "POST" });
  }
  if (!env.INGEST_TOKEN) {
    return jsonResponse({ error: "ingest is not configured" }, 500);
  }
  if (!isAuthorized(request, env)) {
    return jsonResponse({ error: "unauthorized" }, 401, {
      "WWW-Authenticate": "Bearer",
    });
  }

  const declaredLength = Number(request.headers.get("Content-Length") || 0);
  if (declaredLength > MAX_BODY_BYTES) {
    return jsonResponse({ error: "payload too large" }, 413);
  }

  const body = await request.text();
  if (body.length > MAX_BODY_BYTES) {
    return jsonResponse({ error: "payload too large" }, 413);
  }

  let payload;
  try {
    payload = JSON.parse(body);
  } catch {
    return jsonResponse({ error: "body is not valid JSON" }, 400);
  }

  const rawRecords = Array.isArray(payload) ? payload : [payload];
  if (rawRecords.length === 0) {
    return jsonResponse({ ok: true, written: 0 });
  }
  if (rawRecords.length > MAX_RECORDS_PER_REQUEST) {
    return jsonResponse(
      { error: `too many records (max ${MAX_RECORDS_PER_REQUEST})` },
      400,
    );
  }

  const records = [];
  for (const [index, raw] of rawRecords.entries()) {
    const { record, error } = normalizeRecord(raw);
    if (error) {
      return jsonResponse({ error: `record ${index}: ${error}` }, 400);
    }
    records.push(record);
  }

  const statement = env.DB.prepare(
    `INSERT INTO schedule_log
       (ts, ts_epoch, route_id, bus_schedule, eta_iso, eta_epoch, alarm_time, reason)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT (ts, route_id) DO UPDATE SET
       ts_epoch     = excluded.ts_epoch,
       bus_schedule = excluded.bus_schedule,
       eta_iso      = excluded.eta_iso,
       eta_epoch    = excluded.eta_epoch,
       alarm_time   = excluded.alarm_time,
       reason       = excluded.reason`,
  );

  await env.DB.batch(
    records.map((r) =>
      statement.bind(
        r.ts,
        r.ts_epoch,
        r.route_id,
        r.bus_schedule,
        r.eta_iso,
        r.eta_epoch,
        r.alarm_time,
        r.reason,
      ),
    ),
  );

  return jsonResponse({ ok: true, written: records.length });
}

async function handleData(request, env, url) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return jsonResponse({ error: "method not allowed" }, 405, { Allow: "GET" });
  }

  const conditions = ["eta_iso <> ''"];
  const bindings = [];

  const daysParam = url.searchParams.get("days");
  if (daysParam !== null) {
    const days = Number(daysParam);
    if (!Number.isFinite(days) || days <= 0) {
      return jsonResponse({ error: "days must be a positive number" }, 400);
    }
    conditions.push("ts_epoch >= ?");
    bindings.push(Date.now() - days * 86400000);
  }

  const route = url.searchParams.get("route");
  if (route) {
    conditions.push("route_id = ?");
    bindings.push(route);
  }

  const query =
    // `ts` is aliased so a response record has the same field names as an
    // ingested one.
    `SELECT ts AS timestamp, route_id, bus_schedule, eta_iso, alarm_time, reason
     FROM schedule_log
     WHERE ${conditions.join(" AND ")}
     ORDER BY ts_epoch ASC
     LIMIT ${MAX_ROWS_PER_QUERY}`;

  const { results } = await env.DB.prepare(query)
    .bind(...bindings)
    .all();

  return jsonResponse(
    {
      generated_at: new Date().toISOString(),
      count: results.length,
      records: results,
    },
    200,
    { "Cache-Control": `public, max-age=${DATA_CACHE_SECONDS}` },
  );
}
