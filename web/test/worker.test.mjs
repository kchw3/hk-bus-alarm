/**
 * Node harness for the Worker. workerd cannot run in this container, so the
 * bindings it needs (D1, caches, crypto.subtle.timingSafeEqual) are stubbed and
 * the SQL is executed against a real better-sqlite3-free sqlite via node:sqlite.
 */
import { DatabaseSync } from "node:sqlite";
import assert from "node:assert";
import { readFileSync } from "node:fs";

import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// repo root, two levels up from web/test/
const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");

// --- stubs -----------------------------------------------------------------
crypto.subtle.timingSafeEqual = (a, b) => {
  const x = new Uint8Array(a);
  const y = new Uint8Array(b);
  if (x.length !== y.length) return false;
  return x.every((v, i) => v === y[i]);
};

const db = new DatabaseSync(":memory:");
db.exec(readFileSync(`${REPO}/web/schema.sql`, "utf8"));

const DB = {
  prepare(sql) {
    // bind() must return an object whose all()/run() see ITS OWN args, not the
    // prepared statement's — mirroring D1, where bind() yields a new statement.
    const make = (args) => ({
      _sql: sql,
      _args: args,
      bind: (...next) => make(next),
      async all() {
        return { results: db.prepare(sql).all(...args) };
      },
      async run() {
        db.prepare(sql).run(...args);
      },
    });
    return make([]);
  },
  async batch(stmts) {
    for (const s of stmts) db.prepare(s._sql).run(...s._args);
  },
};

const cacheStore = new Map();
globalThis.caches = {
  default: {
    async match(req) {
      return cacheStore.get(req.url)?.clone();
    },
    async put(req, res) {
      cacheStore.set(req.url, res);
    },
  },
};

const ctx = { waitUntil: (p) => p };
const env = { DB, INGEST_TOKEN: "secret" };

const worker = (await import(`${REPO}/web/src/index.js`)).default;

const post = (path, body, token = "secret") =>
  worker.fetch(
    new Request(`https://x.test${path}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
    env,
    ctx,
  );
const get = (path) => worker.fetch(new Request(`https://x.test${path}`), env, ctx);

const RID = "81+1+HIGH SPEED RAIL WEST KOWLOON STATION+WO CHE";
let failures = 0;
async function check(name, fn) {
  try {
    await fn();
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failures++;
    console.log(`  FAIL  ${name}\n        ${e.message}`);
  }
}

// --- schedule ingest -------------------------------------------------------
await check("ingest rejects a record with no seq", async () => {
  const r = await post("/api/ingest", {
    timestamp: "2026-08-21T07:00:00+08:00",
    route_id: RID,
    eta_iso: "2026-08-21T07:26:00+08:00",
  });
  assert.strictEqual(r.status, 400);
  assert.match((await r.json()).error, /seq must be a positive integer/);
});

await check("ingest rejects seq 0 and non-integers", async () => {
  for (const seq of [0, -1, "abc", 1.5]) {
    const r = await post("/api/ingest", {
      timestamp: "2026-08-21T07:00:00+08:00",
      route_id: RID,
      seq,
      eta_iso: "2026-08-21T07:26:00+08:00",
    });
    assert.strictEqual(r.status, 400, `seq=${seq} should be rejected`);
  }
});

await check("two stops at the same timestamp both persist", async () => {
  const ts = "2026-08-21T07:00:00+08:00";
  for (const [seq, stop_id, eta] of [
    [5, "D7C7D86E923EEAE1", "2026-08-21T07:26:00+08:00"],
    [7, "SEVEN", "2026-08-21T07:31:00+08:00"],
  ]) {
    const r = await post("/api/ingest", {
      timestamp: ts, route_id: RID, seq, stop_id, eta_iso: eta,
      bus_schedule: `${eta.slice(0, 16)}+08:00 (26m)`, alarm_time: "06:56", reason: "x",
    });
    assert.strictEqual(r.status, 200);
  }
  const rows = db.prepare("SELECT seq, stop_id FROM schedule_log WHERE ts=?").all(ts);
  assert.strictEqual(rows.length, 2, "old (ts,route_id) PK would have kept one");
  assert.deepStrictEqual(rows.map((r) => r.seq).sort(), [5, 7]);
});

await check("replay without stop_id preserves the captured one", async () => {
  const r = await post("/api/ingest", {
    timestamp: "2026-08-21T07:00:00+08:00", route_id: RID, seq: 5,
    eta_iso: "2026-08-21T07:27:00+08:00", bus_schedule: "replayed", reason: "r2",
  });
  assert.strictEqual(r.status, 200);
  const row = db.prepare("SELECT stop_id, reason FROM schedule_log WHERE ts=? AND seq=5")
    .get("2026-08-21T07:00:00+08:00");
  assert.strictEqual(row.stop_id, "D7C7D86E923EEAE1", "stop_id was erased");
  assert.strictEqual(row.reason, "r2", "other columns should still update");
});

await check("data.json returns seq and filters on it", async () => {
  const all = await (await get("/api/data.json")).json();
  assert.ok(all.records.every((r) => Number.isInteger(r.seq)), "every record needs a seq");
  const only5 = await (await get("/api/data.json?seq=5")).json();
  assert.ok(only5.records.length > 0 && only5.records.every((r) => r.seq === 5));
  assert.strictEqual((await get("/api/data.json?seq=0")).status, 400);
});

// --- arrivals ingest -------------------------------------------------------
await check("arrivals ingest stores stop_id and widens the bracket", async () => {
  const base = {
    session_id: `${RID}|5|2026-08-21T07:00:00+08:00`, route_id: RID, seq: 5,
    eta_iso: "2026-08-21T07:28:00+08:00", window_from: "a", window_to: "b",
  };
  let r = await post("/api/arrivals/ingest", {
    ...base, stop_id: "D7C7D86E923EEAE1",
    first_seen: "2026-08-21T07:10:00+08:00", last_seen: "2026-08-21T07:20:00+08:00", polls: 5,
  });
  assert.strictEqual(r.status, 200);
  // A replay from CSV: no stop_id, wider bracket.
  r = await post("/api/arrivals/ingest", {
    ...base,
    first_seen: "2026-08-21T07:05:00+08:00", last_seen: "2026-08-21T07:30:00+08:00", polls: 9,
  });
  assert.strictEqual(r.status, 200);
  const row = db.prepare("SELECT stop_id, first_seen, last_seen FROM arrival_track").get();
  assert.strictEqual(row.stop_id, "D7C7D86E923EEAE1", "stop_id erased by replay");
  assert.strictEqual(row.first_seen, "2026-08-21T07:05:00+08:00");
  assert.strictEqual(row.last_seen, "2026-08-21T07:30:00+08:00");
});

await check("arrivals data.json returns stop_id", async () => {
  const body = await (await get("/api/arrivals/data.json")).json();
  assert.ok(body.records.length > 0);
  assert.ok("stop_id" in body.records[0]);
});

// --- /api/stops.json -------------------------------------------------------
const ROUTE_FIXTURE = {
  routeList: { [RID]: { stops: { kmb: ["S1", "S2", "S3", "S4", "D7C7D86E923EEAE1"] } } },
  stopList: {
    S1: { name: { en: "A", zh: "甲" } },
    D7C7D86E923EEAE1: { name: { en: "KOWLOON CENTRAL POST OFFICE (YT134)", zh: "九龍中央郵政局 (YT134)" } },
  },
};

await check("stops.json maps seq 5 to the right stop, 1-based", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return new Response(JSON.stringify(ROUTE_FIXTURE), { status: 200 });
  };
  const body = await (await get(`/api/stops.json?route=${encodeURIComponent(RID)}`)).json();
  assert.strictEqual(body.count, 5);
  assert.strictEqual(body.stops[0].seq, 1, "seq must be 1-based to match the CLI");
  const five = body.stops.find((s) => s.seq === 5);
  assert.strictEqual(five.stop_id, "D7C7D86E923EEAE1");
  assert.strictEqual(five.name_en, "KOWLOON CENTRAL POST OFFICE (YT134)");
  // second request must be served from cache, not upstream
  await get(`/api/stops.json?route=${encodeURIComponent(RID)}`);
  assert.strictEqual(calls, 1, "8MB upstream should be fetched once, then cached");
});

await check("stops.json 404s an unknown route, 503s an upstream failure", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify(ROUTE_FIXTURE), { status: 200 });
  assert.strictEqual((await get("/api/stops.json?route=nope")).status, 404);
  globalThis.fetch = async () => { throw new Error("network down"); };
  assert.strictEqual((await get("/api/stops.json?route=other")).status, 503);
  assert.strictEqual((await get("/api/stops.json")).status, 400);
});

console.log(failures === 0 ? "\nAll worker checks passed." : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
