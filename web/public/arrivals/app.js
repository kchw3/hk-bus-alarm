/**
 * Renders arrival tracking as date (x) vs time of day (y) — same axes as the
 * schedule chart, so the two pages can be read side by side.
 *
 * One tracking session becomes a vertical column of markers at its ETA's date:
 *   - green  every distinct ETA the operator published while the bus approached.
 *            The earliest of these is the lower bound of the bracket.
 *   - blue   the last of those, the final estimate before the bus vanished.
 *   - red    the last poll that still listed the bus, once it was overdue —
 *            the upper bound.
 *
 * The bracket therefore runs from the first green mark to the red one, with the
 * blue final estimate inside it. Red is omitted when the bus vanished before its
 * ETA elapsed: there is no overdue sighting to draw, and inventing one would
 * misrepresent the track.
 */

import { preloadStops, seriesLabel, stopName } from "../stop-labels.js";

// Absolute: this page lives one level down, so the schedule chart's relative
// "./api/…" idiom would resolve to /arrivals/api/… here.
const DATA_URL = "/api/arrivals/data.json";

const ISO_PARTS = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/;

const state = {
  records: [],
  days: 0,           // 0 = all
  showEstimates: true,
};

const el = {
  chart: document.getElementById("chart"),
  message: document.getElementById("message"),
  subtitle: document.getElementById("subtitle"),
  controls: document.getElementById("controls"),
  showEstimates: document.getElementById("showEstimates"),
  tableView: document.getElementById("tableView"),
  tableBody: document.getElementById("tableBody"),
};

/** Read a CSS custom property off :root, so the palette lives in one place. */
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/**
 * Split an ISO timestamp into its *local* calendar date and clock time, using the
 * offset carried in the string itself. Deliberately avoids `new Date()`, which
 * would re-express a +08:00 timestamp in the viewer's own timezone and could
 * shift a late-evening bus onto the next day.
 */
function splitIso(iso) {
  const match = ISO_PARTS.exec(iso || "");
  if (!match) {
    return null;
  }
  const [, date, hh, mm, ss] = match;
  return {
    date,
    clock: `${hh}:${mm}:${ss || "00"}`,
    // Plotly plots time-of-day on a date axis pinned to one dummy day.
    dummy: `1970-01-01 ${hh}:${mm}:${ss || "00"}`,
  };
}

function showMessage(text, isError = false) {
  el.message.textContent = text;
  el.message.hidden = false;
  el.message.className = isError ? "msg error" : "msg";
  el.chart.style.display = "none";
}

function hideMessage() {
  el.message.hidden = true;
  el.chart.style.display = "";
}

function minutesBetween(fromEpoch, toEpoch) {
  return (toEpoch - fromEpoch) / 60000;
}

function signedMinutes(value) {
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}m`;
}

/**
 * Group rows into sessions. Rows arrive ordered by (session_id, first_seen_epoch),
 * so the last row of each group is that session's final observation.
 */
function groupBySession(records) {
  const sessions = new Map();
  for (const record of records) {
    const eta = splitIso(record.eta_iso);
    if (!eta) {
      continue;
    }
    if (!sessions.has(record.session_id)) {
      sessions.set(record.session_id, []);
    }
    sessions.get(record.session_id).push({ ...record, eta });
  }

  const out = [];
  for (const [sessionId, rows] of sessions) {
    // Defensive: do not rely on the server's ORDER BY for correctness of
    // "which estimate was last".
    rows.sort((a, b) => Date.parse(a.first_seen) - Date.parse(b.first_seen));
    const final = rows[rows.length - 1];
    const first = rows[0];
    const lastSeen = splitIso(final.last_seen);
    out.push({
      sessionId,
      route: final.route_id,
      seq: final.seq,
      // Resolved once per session; every trace below reuses it, so the route
      // and stop name stay identical across the three marker series.
      label: seriesLabel(final.route_id, final.seq),
      stop: stopName(final.route_id, final.seq),
      rows,
      first,
      final,
      lastSeen,
      // Only a bus still listed after its own ETA gives an upper bound.
      overdue: lastSeen !== null && final.last_seen_epoch > final.eta_epoch,
      polls: rows.reduce((sum, r) => sum + (r.polls || 1), 0),
      drift: minutesBetween(first.eta_epoch, final.eta_epoch),
    });
  }
  out.sort((a, b) => a.final.eta_epoch - b.final.eta_epoch);
  return out;
}

function withinRange(records, days) {
  if (!days) {
    return records;
  }
  const cutoff = Date.now() - days * 86400000;
  return records.filter((r) => r.eta_epoch >= cutoff);
}

const HOVER =
  "<b>%{customdata[0]}</b><br>" +
  "%{customdata[1]}<br>" +
  "%{customdata[2]}<extra></extra>";

function buildTraces(sessions) {
  const traces = [];

  if (state.showEstimates) {
    const x = [];
    const y = [];
    const customdata = [];
    for (const session of sessions) {
      for (const row of session.rows) {
        x.push(row.eta.date);
        y.push(row.eta.dummy);
        customdata.push([
          session.label,
          `estimate ${row.eta.clock} (${signedMinutes(
            minutesBetween(session.final.eta_epoch, row.eta_epoch),
          )} vs final)`,
          `published ${splitIso(row.first_seen)?.clock || row.first_seen}, ${row.polls} poll(s)`,
        ]);
      }
    }
    traces.push({
      type: "scatter",
      mode: "markers",
      name: "every estimate",
      x, y, customdata,
      hovertemplate: HOVER,
      marker: {
        symbol: "x-thin",
        size: 8,
        opacity: 0.6,
        color: cssVar("--series-3"),
        line: { width: 1.5, color: cssVar("--series-3") },
      },
    });
  }

  traces.push({
    type: "scatter",
    mode: "markers",
    name: "final estimate",
    x: sessions.map((s) => s.final.eta.date),
    y: sessions.map((s) => s.final.eta.dummy),
    customdata: sessions.map((s) => [
      s.label,
      `final estimate ${s.final.eta.clock}`,
      `drifted ${signedMinutes(s.drift)} across ${s.rows.length} estimate(s)`,
    ]),
    hovertemplate: HOVER,
    marker: {
      symbol: "x-thin",
      size: 13,
      color: cssVar("--series-1"),
      line: { width: 2.5, color: cssVar("--series-1") },
    },
  });

  const overdue = sessions.filter((s) => s.overdue);
  if (overdue.length) {
    traces.push({
      type: "scatter",
      mode: "markers",
      name: "last sighting",
      x: overdue.map((s) => s.final.eta.date),
      y: overdue.map((s) => s.lastSeen.dummy),
      customdata: overdue.map((s) => [
        s.label,
        `last sighting ${s.lastSeen.clock}`,
        `still listed ${signedMinutes(
          minutesBetween(s.final.eta_epoch, s.final.last_seen_epoch),
        )} past its own ETA`,
      ]),
      hovertemplate: HOVER,
      marker: {
        symbol: "x-thin",
        size: 13,
        color: cssVar("--series-8"),
        line: { width: 2.5, color: cssVar("--series-8") },
      },
    });
  }

  // Vertical connector spanning the whole bracket — the FIRST published ETA
  // (lower bound) up to the last sighting (upper bound) — so the column reads as
  // one interval rather than a scatter of loose marks. The green estimates and
  // the blue final sit inside it.
  const bracketX = [];
  const bracketY = [];
  for (const session of overdue) {
    bracketX.push(session.first.eta.date, session.first.eta.date, null);
    bracketY.push(session.first.eta.dummy, session.lastSeen.dummy, null);
  }
  if (bracketX.length) {
    traces.push({
      type: "scatter",
      mode: "lines",
      name: "arrival window",
      x: bracketX,
      y: bracketY,
      hoverinfo: "skip",
      showlegend: false,
      line: { width: 1.5, color: cssVar("--text-muted"), dash: "dot" },
    });
  }

  return traces;
}

const WEEKEND_DAYS = new Set([0, 6]); // Sunday, Saturday
const MAX_WEEKEND_BANDS = 400;
const HALF_DAY_MS = 43200000;

/** 'YYYY-MM-DD HH:MM:SS' for a Plotly date axis, from an epoch in UTC. */
function axisStamp(epoch) {
  return new Date(epoch).toISOString().slice(0, 19).replace("T", " ");
}

/**
 * A background band behind every Saturday and Sunday in the plotted range.
 * Weekday is read in UTC from the plain `YYYY-MM-DD` string so the viewer's own
 * timezone cannot shift a band onto the neighbouring day.
 */
function weekendBands(dates) {
  if (dates.length === 0) {
    return [];
  }
  const first = Date.parse(`${dates[0]}T00:00:00Z`);
  const last = Date.parse(`${dates[dates.length - 1]}T00:00:00Z`);
  if (Number.isNaN(first) || Number.isNaN(last)) {
    return [];
  }

  const color = cssVar("--weekend-band");
  const shapes = [];
  for (let t = first; t <= last && shapes.length < MAX_WEEKEND_BANDS; t += 2 * HALF_DAY_MS) {
    if (!WEEKEND_DAYS.has(new Date(t).getUTCDay())) {
      continue;
    }
    shapes.push({
      type: "rect",
      xref: "x",
      yref: "paper",
      layer: "below",
      x0: axisStamp(t - HALF_DAY_MS),
      x1: axisStamp(t + HALF_DAY_MS),
      y0: 0,
      y1: 1,
      fillcolor: color,
      line: { width: 0 },
    });
  }
  return shapes;
}

function buildLayout(dates) {
  return {
    shapes: weekendBands(dates),
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: cssVar("--text-secondary"), size: 12 },
    margin: { l: 62, r: 16, t: 12, b: 44 },
    hovermode: "closest",
    hoverlabel: {
      bgcolor: cssVar("--surface-1"),
      bordercolor: cssVar("--border"),
      font: { color: cssVar("--text-primary") },
      align: "left",
    },
    showlegend: true,
    legend: {
      orientation: "h",
      yanchor: "bottom",
      y: 1.02,
      x: 0,
      font: { color: cssVar("--text-secondary") },
    },
    xaxis: {
      type: "date",
      tickformat: "%b %-d",
      gridcolor: cssVar("--grid"),
      linecolor: cssVar("--border"),
      zeroline: false,
      rangeslider: { visible: true, thickness: 0.08, bgcolor: cssVar("--surface-0") },
    },
    yaxis: {
      type: "date",
      tickformat: "%H:%M",
      title: { text: "Time of day", font: { color: cssVar("--text-muted") } },
      gridcolor: cssVar("--grid"),
      linecolor: cssVar("--border"),
      zeroline: false,
    },
  };
}

const PLOT_CONFIG = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"],
  toImageButtonOptions: { filename: "bus-arrival-tracking", scale: 2 },
};

function renderTable(sessions) {
  const rows = [...sessions].sort((a, b) => b.final.eta_epoch - a.final.eta_epoch);

  el.tableBody.innerHTML = "";
  for (const session of rows) {
    const tr = document.createElement("tr");
    const cells = [
      [session.final.eta.date, false],
      [session.route, false],
      [`${session.seq} · ${session.stop}`, false],
      [session.first.eta.clock, false],
      [session.final.eta.clock, false],
      [session.overdue ? session.lastSeen.clock : "—", false],
      [signedMinutes(session.drift), true],
      [String(session.rows.length), true],
      [String(session.polls), true],
    ];
    for (const [value, dim] of cells) {
      const td = document.createElement("td");
      td.textContent = value;
      if (dim) {
        td.className = "dim";
      }
      tr.appendChild(td);
    }
    el.tableBody.appendChild(tr);
  }
  el.tableView.hidden = rows.length === 0;
}

function render() {
  const sessions = groupBySession(withinRange(state.records, state.days));

  if (sessions.length === 0) {
    showMessage(
      state.days
        ? `No arrival tracks in the last ${state.days} days.`
        : "No arrival tracks recorded yet.",
    );
    el.tableView.hidden = true;
    Plotly.purge(el.chart);
    return;
  }

  hideMessage();
  const dates = [...new Set(sessions.map((s) => s.final.eta.date))].sort();
  Plotly.react(el.chart, buildTraces(sessions), buildLayout(dates), PLOT_CONFIG);
  renderTable(sessions);
}

function updateSubtitle() {
  const sessions = new Set(state.records.map((r) => r.session_id));
  const keys = new Set(state.records.map((r) => `${r.route_id}|${r.seq}`));
  const first = state.records[0];
  const routeText =
    keys.size === 1 && first
      ? seriesLabel(first.route_id, first.seq)
      : `${keys.size} route/stop series`;
  el.subtitle.textContent =
    `${routeText} · ${sessions.size} tracked bus${sessions.size === 1 ? "" : "es"} · ` +
    `${state.records.length} estimate${state.records.length === 1 ? "" : "s"} recorded`;
}

function wireControls() {
  el.controls.hidden = false;

  for (const button of el.controls.querySelectorAll("button[data-days]")) {
    button.addEventListener("click", () => {
      state.days = Number(button.dataset.days);
      for (const other of el.controls.querySelectorAll("button[data-days]")) {
        other.setAttribute("aria-pressed", String(other === button));
      }
      render();
    });
  }

  el.showEstimates.addEventListener("change", () => {
    state.showEstimates = el.showEstimates.checked;
    render();
  });

  // Re-render on theme change so the palette swaps to its dark steps.
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
  }
}

async function main() {
  let payload;
  try {
    const response = await fetch(DATA_URL, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    payload = await response.json();
  } catch (error) {
    el.subtitle.textContent = "";
    showMessage(`Could not load arrival data: ${error.message}`, true);
    return;
  }

  state.records = Array.isArray(payload.records) ? payload.records : [];
  if (state.records.length === 0) {
    el.subtitle.textContent =
      "No data yet — run track_bus_arrival.py with -log_url.";
    showMessage("No arrival tracks recorded yet.");
    return;
  }

  // Labels resolve synchronously inside the trace builders, so warm the stop
  // metadata first. preloadStops() never rejects; labels fall back to "stop N".
  await preloadStops(state.records.map((r) => r.route_id));

  updateSubtitle();
  wireControls();
  render();
}

main();
