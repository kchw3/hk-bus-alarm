/**
 * HK bus schedule chart — Worker.
 *
 * Routes:
 *   POST /api/ingest              Bearer-token protected. One record or an array.
 *   GET  /api/data.json           Public. Optional ?days=N and ?route=<route_id>.
 *   POST /api/arrivals/ingest     Same token. Arrival-tracking observations.
 *   GET  /api/arrivals/data.json  Public. Optional ?days=N, ?route=, ?session=.
 *   GET  /api/stops.json          Public. ?route=<route_id>. Stop names for labels.
 *   everything else               Served from ./public by Workers Static Assets.
 *
 * The two ingest endpoints share the INGEST_TOKEN secret and the size guards but
 * write to different tables: schedule_log (one row per alarm run) and
 * arrival_track (one row per distinct ETA value within a tracking session).
 *
 * Both tables store `seq` — never a stop name. `/api/stops.json` is what turns a
 * seq into something a human recognises, so the names stay a display concern and
 * a re-survey upstream cannot rewrite history.
 */

//: Upstream route/stop metadata, the same source hk-bus-eta reads. ~8MB, so it is
//: fetched at most once a day per route and only the extracted stop list is kept.
const ROUTE_DATA_URL = "https://hkbus.github.io/hk-bus-crawling/routeFareList.min.json";
const STOPS_CACHE_SECONDS = 86400;
const ROUTE_DATA_TIMEOUT_MS = 20000;

// A record is ~250 bytes, so this comfortably holds a full backfill batch.
const MAX_BODY_BYTES = 256 * 1024;
const MAX_RECORDS_PER_REQUEST = 500;
const MAX_ROWS_PER_QUERY = 20000;
const DATA_CACHE_SECONDS = 60;

const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8" };

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/api/ingest") {
      return handleIngest(request, env);
    }
    if (url.pathname === "/api/data.json") {
      return handleData(request, env, url);
    }
    if (url.pathname === "/api/arrivals/ingest") {
      return handleArrivalsIngest(request, env);
    }
    if (url.pathname === "/api/arrivals/data.json") {
      return handleArrivalsData(request, env, url);
    }
    if (url.pathname === "/api/stops.json") {
      return handleStops(request, url, ctx);
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

  // Required, and deliberately not defaulted: `seq` is half the series identity,
  // so a record without one would silently merge two stops of the same route —
  // exactly the bug the column was added to fix.
  const seq = Number(raw.seq);
  if (!Number.isInteger(seq) || seq < 1) {
    return { error: `seq must be a positive integer, got ${raw.seq}` };
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
      seq,
      stop_id: text(raw.stop_id),
      bus_schedule: text(raw.bus_schedule),
      eta_iso: etaIso,
      eta_epoch: etaEpoch,
      alarm_time: text(raw.alarm_time),
      reason: text(raw.reason),
    },
  };
}

/**
 * The front half both ingest endpoints share: method, auth, size limits and JSON
 * parsing. Returns { rawRecords } to carry on with, or { response } to return
 * unchanged.
 */
async function readIngestPayload(request, env) {
  if (request.method !== "POST") {
    return {
      response: jsonResponse({ error: "method not allowed" }, 405, { Allow: "POST" }),
    };
  }
  if (!env.INGEST_TOKEN) {
    return { response: jsonResponse({ error: "ingest is not configured" }, 500) };
  }
  if (!isAuthorized(request, env)) {
    return {
      response: jsonResponse({ error: "unauthorized" }, 401, {
        "WWW-Authenticate": "Bearer",
      }),
    };
  }

  const declaredLength = Number(request.headers.get("Content-Length") || 0);
  if (declaredLength > MAX_BODY_BYTES) {
    return { response: jsonResponse({ error: "payload too large" }, 413) };
  }

  const body = await request.text();
  if (body.length > MAX_BODY_BYTES) {
    return { response: jsonResponse({ error: "payload too large" }, 413) };
  }

  let payload;
  try {
    payload = JSON.parse(body);
  } catch {
    return { response: jsonResponse({ error: "body is not valid JSON" }, 400) };
  }

  const rawRecords = Array.isArray(payload) ? payload : [payload];
  if (rawRecords.length > MAX_RECORDS_PER_REQUEST) {
    return {
      response: jsonResponse(
        { error: `too many records (max ${MAX_RECORDS_PER_REQUEST})` },
        400,
      ),
    };
  }

  return { rawRecords };
}

/** Normalise every raw record, or return the 400 describing the first failure. */
function normalizeAll(rawRecords, normalize) {
  const records = [];
  for (const [index, raw] of rawRecords.entries()) {
    const { record, error } = normalize(raw);
    if (error) {
      return { response: jsonResponse({ error: `record ${index}: ${error}` }, 400) };
    }
    records.push(record);
  }
  return { records };
}

async function handleIngest(request, env) {
  const { rawRecords, response } = await readIngestPayload(request, env);
  if (response) {
    return response;
  }
  if (rawRecords.length === 0) {
    return jsonResponse({ ok: true, written: 0 });
  }

  const { records, response: invalid } = normalizeAll(rawRecords, normalizeRecord);
  if (invalid) {
    return invalid;
  }

  const statement = env.DB.prepare(
    // stop_id is COALESCEd rather than overwritten: a CSV replay through
    // backfill_log.py carries no stop_id, and must not erase one a live run
    // already recorded.
    `INSERT INTO schedule_log
       (ts, ts_epoch, route_id, seq, stop_id,
        bus_schedule, eta_iso, eta_epoch, alarm_time, reason)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT (ts, route_id, seq) DO UPDATE SET
       ts_epoch     = excluded.ts_epoch,
       stop_id      = COALESCE(NULLIF(excluded.stop_id, ''), stop_id),
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
        r.seq,
        r.stop_id,
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

  const seqParam = url.searchParams.get("seq");
  if (seqParam !== null) {
    const seq = Number(seqParam);
    if (!Number.isInteger(seq) || seq < 1) {
      return jsonResponse({ error: "seq must be a positive integer" }, 400);
    }
    conditions.push("seq = ?");
    bindings.push(seq);
  }

  const query =
    // `ts` is aliased so a response record has the same field names as an
    // ingested one.
    `SELECT ts AS timestamp, route_id, seq, stop_id,
            bus_schedule, eta_iso, alarm_time, reason
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

/**
 * Validate and normalise one arrival-tracking observation.
 * Returns { record } or { error }.
 */
function normalizeArrivalRecord(raw) {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return { error: "record must be a JSON object" };
  }

  const text = (value) => (typeof value === "string" ? value.trim() : "");

  const sessionId = text(raw.session_id);
  const routeId = text(raw.route_id);
  if (!sessionId) {
    return { error: "session_id is required" };
  }
  if (!routeId) {
    return { error: "route_id is required" };
  }

  // Each of these three is load-bearing: eta_iso is the estimate itself, and the
  // first/last pair is what brackets the arrival.
  const stamps = {};
  for (const field of ["eta_iso", "first_seen", "last_seen"]) {
    const value = text(raw[field]);
    if (!value) {
      return { error: `${field} is required` };
    }
    const epoch = Date.parse(value);
    if (Number.isNaN(epoch)) {
      return { error: `${field} is not a valid ISO 8601 datetime: ${value}` };
    }
    stamps[field] = { value, epoch };
  }

  const seq = Number(raw.seq);
  if (!Number.isInteger(seq) || seq < 1) {
    return { error: `seq must be a positive integer, got ${raw.seq}` };
  }

  const polls = raw.polls === undefined ? 1 : Number(raw.polls);
  if (!Number.isInteger(polls) || polls < 1) {
    return { error: `polls must be a positive integer, got ${raw.polls}` };
  }

  return {
    record: {
      session_id: sessionId,
      route_id: routeId,
      seq,
      stop_id: text(raw.stop_id),
      eta_iso: stamps.eta_iso.value,
      eta_epoch: stamps.eta_iso.epoch,
      first_seen: stamps.first_seen.value,
      first_seen_epoch: stamps.first_seen.epoch,
      last_seen: stamps.last_seen.value,
      last_seen_epoch: stamps.last_seen.epoch,
      polls,
      window_from: text(raw.window_from),
      window_to: text(raw.window_to),
    },
  };
}

async function handleArrivalsIngest(request, env) {
  const { rawRecords, response } = await readIngestPayload(request, env);
  if (response) {
    return response;
  }
  if (rawRecords.length === 0) {
    return jsonResponse({ ok: true, written: 0 });
  }

  const { records, response: invalid } = normalizeAll(rawRecords, normalizeArrivalRecord);
  if (invalid) {
    return invalid;
  }

  // The conflict clause widens the bracket rather than overwriting it: the
  // earliest first_seen and the latest last_seen win. That keeps a replay
  // idempotent even if rows arrive out of order.
  const statement = env.DB.prepare(
    `INSERT INTO arrival_track
       (session_id, route_id, seq, stop_id, eta_iso, eta_epoch,
        first_seen, first_seen_epoch, last_seen, last_seen_epoch,
        polls, window_from, window_to)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT (session_id, eta_iso) DO UPDATE SET
       route_id   = excluded.route_id,
       seq        = excluded.seq,
       stop_id    = COALESCE(NULLIF(excluded.stop_id, ''), arrival_track.stop_id),
       eta_epoch  = excluded.eta_epoch,
       first_seen = CASE
                      WHEN excluded.first_seen_epoch < arrival_track.first_seen_epoch
                      THEN excluded.first_seen ELSE arrival_track.first_seen END,
       first_seen_epoch = MIN(arrival_track.first_seen_epoch, excluded.first_seen_epoch),
       last_seen  = CASE
                      WHEN excluded.last_seen_epoch > arrival_track.last_seen_epoch
                      THEN excluded.last_seen ELSE arrival_track.last_seen END,
       last_seen_epoch  = MAX(arrival_track.last_seen_epoch, excluded.last_seen_epoch),
       polls       = MAX(arrival_track.polls, excluded.polls),
       window_from = excluded.window_from,
       window_to   = excluded.window_to`,
  );

  await env.DB.batch(
    records.map((r) =>
      statement.bind(
        r.session_id,
        r.route_id,
        r.seq,
        r.stop_id,
        r.eta_iso,
        r.eta_epoch,
        r.first_seen,
        r.first_seen_epoch,
        r.last_seen,
        r.last_seen_epoch,
        r.polls,
        r.window_from,
        r.window_to,
      ),
    ),
  );

  return jsonResponse({ ok: true, written: records.length });
}

async function handleArrivalsData(request, env, url) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return jsonResponse({ error: "method not allowed" }, 405, { Allow: "GET" });
  }

  const conditions = [];
  const bindings = [];

  const daysParam = url.searchParams.get("days");
  if (daysParam !== null) {
    const days = Number(daysParam);
    if (!Number.isFinite(days) || days <= 0) {
      return jsonResponse({ error: "days must be a positive number" }, 400);
    }
    // Filtered on the ETA, which is what the chart plots on the date axis.
    conditions.push("eta_epoch >= ?");
    bindings.push(Date.now() - days * 86400000);
  }

  const route = url.searchParams.get("route");
  if (route) {
    conditions.push("route_id = ?");
    bindings.push(route);
  }

  const session = url.searchParams.get("session");
  if (session) {
    conditions.push("session_id = ?");
    bindings.push(session);
  }

  const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
  // Ordered so the last row of each session group is its final observation,
  // which is the one the chart marks blue and red.
  const query =
    `SELECT session_id, route_id, seq, stop_id, eta_iso, eta_epoch,
            first_seen, last_seen, last_seen_epoch, polls, window_from, window_to
     FROM arrival_track
     ${where}
     ORDER BY session_id ASC, first_seen_epoch ASC
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

/**
 * Stop names for one route, so the pages can label a `seq` with something a
 * human recognises.
 *
 * The database stores `seq`, never a name — this is the only place the two are
 * joined, and it happens at render time. Upstream is ~8MB, far too heavy to hand
 * to a browser, so the Worker parses it and returns just this route's stops.
 * The small response is cached for a day; the expensive fetch happens on a miss.
 *
 * A failure here is never fatal: the pages fall back to "stop N".
 */
async function handleStops(request, url, ctx) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return jsonResponse({ error: "method not allowed" }, 405, { Allow: "GET" });
  }

  const routeId = url.searchParams.get("route");
  if (!routeId) {
    return jsonResponse({ error: "route is required" }, 400);
  }

  // Key on a normalised URL so "?route=X&t=1" cannot fragment the cache.
  const cacheKey = new Request(
    `${url.origin}/api/stops.json?route=${encodeURIComponent(routeId)}`,
    { method: "GET" },
  );
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) {
    return cached;
  }

  let routeData;
  try {
    routeData = await fetchRouteData();
  } catch (err) {
    // 503, not 404: the route may well exist, we just could not look it up.
    return jsonResponse({ error: `stop metadata unavailable: ${err}` }, 503);
  }

  const stops = extractStops(routeData, routeId);
  if (stops === null) {
    return jsonResponse({ error: `unknown route: ${routeId}` }, 404);
  }

  const response = jsonResponse(
    { route_id: routeId, count: stops.length, stops },
    200,
    { "Cache-Control": `public, max-age=${STOPS_CACHE_SECONDS}` },
  );
  // Populate the cache without making this request wait on it.
  ctx.waitUntil(cache.put(cacheKey, response.clone()));
  return response;
}

/** Fetch and parse the upstream route/stop metadata. Throws on failure. */
async function fetchRouteData() {
  const upstream = await fetch(ROUTE_DATA_URL, {
    signal: AbortSignal.timeout(ROUTE_DATA_TIMEOUT_MS),
    // Let Cloudflare hold the big payload at the edge too, so a cold Worker
    // usually avoids the origin round trip as well as the parse.
    cf: { cacheTtl: STOPS_CACHE_SECONDS, cacheEverything: true },
  });
  if (!upstream.ok) {
    throw new Error(`upstream returned HTTP ${upstream.status}`);
  }
  return upstream.json();
}

/**
 * Flatten one route's stops into [{seq, co, stop_id, name_en, name_zh}].
 *
 * `seq` is 1-based and follows the iteration order of the route's stop map —
 * the same order `flatten_stops()` in hk_bus_common.py produces, which is what
 * makes the number in the database mean the same thing here.
 *
 * Returns null when the route is unknown.
 */
function extractStops(routeData, routeId) {
  const route = routeData?.routeList?.[routeId];
  if (!route) {
    return null;
  }
  const stopList = routeData.stopList || {};
  const stops = [];
  let seq = 1;
  for (const [co, ids] of Object.entries(route.stops || {})) {
    if (!Array.isArray(ids)) {
      continue;
    }
    for (const stopId of ids) {
      const name = stopList[stopId]?.name || {};
      stops.push({
        seq: seq++,
        co,
        stop_id: stopId,
        name_en: name.en || "",
        name_zh: name.zh || "",
      });
    }
  }
  return stops;
}
