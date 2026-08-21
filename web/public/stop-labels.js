/**
 * Turns a (route_id, seq) pair into something a human recognises.
 *
 * The database stores `seq` and nothing else, so the join to a stop name happens
 * here, at render time, against /api/stops.json. That path is absolute on
 * purpose: /arrivals/ sits one level down, where the main page's relative
 * "./api/…" idiom would resolve to /arrivals/api/… .
 *
 * Every failure degrades to "stop 5" rather than throwing. A chart that cannot
 * reach the stop metadata should still plot its data.
 */

//: route_id -> Map(seq -> stop). Holds the in-flight Promise while loading, and
//: is overwritten with the resolved Map so the synchronous label helpers below
//: can read it without awaiting.
const cache = new Map();

/** Short form of a stop name: the "(YT134)" pole code is noise in a legend. */
function shorten(name) {
  return name.replace(/\s*\([^)]*\)\s*$/, "").trim();
}

/** The resolved stop, or undefined if the route was never loaded or failed. */
function lookup(routeId, seq) {
  const stops = cache.get(routeId);
  return stops instanceof Map ? stops.get(Number(seq)) : undefined;
}

/**
 * Fetch and memoise one route's stops. Resolves to a Map, empty if unavailable —
 * an empty Map is cached too, so a dead endpoint is not re-fetched per point.
 */
function loadRoute(routeId) {
  const existing = cache.get(routeId);
  if (existing) {
    return Promise.resolve(existing);
  }
  const pending = (async () => {
    const stops = new Map();
    try {
      const res = await fetch(`/api/stops.json?route=${encodeURIComponent(routeId)}`);
      if (res.ok) {
        const body = await res.json();
        for (const stop of body.stops || []) {
          stops.set(Number(stop.seq), stop);
        }
      }
    } catch {
      // Leave the map empty; callers fall back to the bare seq.
    }
    // Replace the Promise with the value, so the sync helpers can use it.
    cache.set(routeId, stops);
    return stops;
  })();
  cache.set(routeId, pending);
  return pending;
}

/** Warm the cache for every route about to be rendered, in parallel. */
export async function preloadStops(routeIds) {
  await Promise.all([...new Set(routeIds)].map((id) => loadRoute(id)));
}

/**
 * Label for one stop. Call `preloadStops()` first — this reads the warmed cache
 * synchronously so it can be used inside Plotly trace builders.
 */
export function stopLabel(routeId, seq) {
  const stop = lookup(routeId, seq);
  const name = stop ? shorten(stop.name_en) : "";
  return name ? `stop ${seq} · ${name}` : `stop ${seq}`;
}

/** "81+1+A+B · stop 5 · KOWLOON CENTRAL POST OFFICE" — the full series name. */
export function seriesLabel(routeId, seq) {
  return `${routeId} · ${stopLabel(routeId, seq)}`;
}

/** The stop name alone, for a table cell. Falls back to the bare seq. */
export function stopName(routeId, seq) {
  const stop = lookup(routeId, seq);
  return stop && stop.name_en ? shorten(stop.name_en) : `stop ${seq}`;
}

/**
 * Stable key identifying one series. The separator is a NUL, which cannot occur
 * in a route id, so no route/seq pair can collide with another.
 */
export function seriesKey(routeId, seq) {
  return `${routeId}\u0000${seq}`;
}

/** Split a `seriesKey()` back into its parts. A plain space would not do:
 *  route ids contain them ("HIGH SPEED RAIL WEST KOWLOON STATION"). */
export function splitSeriesKey(key) {
  const [routeId, seq] = key.split("\u0000");
  return { routeId, seq: Number(seq) };
}
