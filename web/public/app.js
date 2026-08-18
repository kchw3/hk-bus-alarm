/**
 * Renders the logged bus schedules as date (x) vs time-of-day (y).
 *
 * Two traces per route:
 *   - "final"     one entry per date: the last logged schedule of that day, with the
 *                 alarm and reason from its first poll. Markers joined by a line, so
 *                 day-to-day drift reads as a trend.
 *   - "all polls" every logged schedule, faint x markers. Off by default.
 */

const DATA_URL = "./api/data.json";

// Categorical slots, assigned in fixed order and never cycled. Slots 1-3 are the
// all-pairs validated set (scatter uses the all-pairs gate); routes beyond the
// third still get a distinct hue, and identity never rests on color alone —
// every point names its route in the tooltip and the legend.
const SERIES_VARS = [
  "--series-1", "--series-2", "--series-3", "--series-4",
  "--series-5", "--series-6", "--series-7", "--series-8",
];

const ISO_PARTS = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/;

const state = {
  records: [],
  days: 0,          // 0 = all
  showPolls: false,
};

const el = {
  chart: document.getElementById("chart"),
  message: document.getElementById("message"),
  subtitle: document.getElementById("subtitle"),
  controls: document.getElementById("controls"),
  showPolls: document.getElementById("showPolls"),
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
 * would re-express a +08:00 timestamp in the viewer's own timezone and could shift
 * an early-morning bus onto the previous day.
 */
function splitIso(iso) {
  const match = ISO_PARTS.exec(iso || "");
  if (!match) {
    return null;
  }
  const [, date, hh, mm, ss] = match;
  return {
    date,
    clock: `${hh}:${mm}`,
    // Plotly plots time-of-day on a date axis pinned to one dummy day.
    dummy: `1970-01-01 ${hh}:${mm}:${ss || "00"}`,
    minutes: Number(hh) * 60 + Number(mm),
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

/** Group parsed points by route_id, preserving first-seen order for colour assignment. */
function groupByRoute(records) {
  const routes = new Map();
  for (const record of records) {
    const eta = splitIso(record.eta_iso);
    if (!eta) {
      continue;
    }
    const logged = splitIso(record.timestamp);
    if (!routes.has(record.route_id)) {
      routes.set(record.route_id, []);
    }
    routes.get(record.route_id).push({
      route: record.route_id,
      date: eta.date,
      clock: eta.clock,
      dummy: eta.dummy,
      loggedAt: logged ? `${logged.date} ${logged.clock}` : record.timestamp,
      alarm: record.alarm_time || "—",
      reason: record.reason || "—",
    });
  }
  return routes;
}

/**
 * Collapse a route's polls into one entry per date.
 *
 * The two halves come from opposite ends of the day, deliberately:
 *   - schedule: the LAST poll, the final ETA the run acted on.
 *   - alarm and reason: the FIRST poll. Later runs are clamped to now+2m
 *     (see _MIN_ALARM_LEAD_MINUTES in set_alarm_with_bus_eta.py), so their
 *     alarm times just track the clock and say nothing about the bus; the
 *     first run of the morning holds the alarm actually planned for it.
 */
function perDateSummary(points) {
  const byDate = new Map();
  for (const point of points) {
    const entry = byDate.get(point.date);
    if (!entry) {
      // First poll of this date: its alarm and reason are kept as-is.
      byDate.set(point.date, { ...point, polls: 1, firstLoggedAt: point.loggedAt });
      continue;
    }
    // Records arrive ordered by timestamp, so each later poll refreshes the schedule.
    entry.clock = point.clock;
    entry.dummy = point.dummy;
    entry.loggedAt = point.loggedAt;
    entry.polls += 1;
  }
  return [...byDate.values()];
}

function withinRange(records, days) {
  if (!days) {
    return records;
  }
  const cutoff = Date.now() - days * 86400000;
  return records.filter((r) => {
    const parsed = Date.parse(r.timestamp);
    return Number.isNaN(parsed) || parsed >= cutoff;
  });
}

const HOVER_TEMPLATE =
  "<b>%{customdata[0]}</b><br>" +
  "%{customdata[1]} · schedule %{customdata[2]}<br>" +
  "alarm %{customdata[3]}<br>" +
  "logged %{customdata[4]}<br>" +
  "%{customdata[5]}<extra></extra>";

function toCustomData(points) {
  return points.map((p) => [p.route, p.date, p.clock, p.alarm, p.loggedAt, p.reason]);
}

const GAP_DAYS = 2;
const EMPTY_HOVER = ["", "", "", "", "", ""];

/**
 * Series arrays with a null break wherever more than GAP_DAYS separates two
 * points, so the line never implies a schedule across days that were never
 * logged. Markers are unaffected.
 */
function seriesWithGaps(points) {
  const x = [];
  const y = [];
  const customdata = [];

  points.forEach((point, index) => {
    const previous = points[index - 1];
    if (previous) {
      const gap = (Date.parse(point.date) - Date.parse(previous.date)) / 86400000;
      if (gap > GAP_DAYS) {
        x.push(previous.date);
        y.push(null);
        customdata.push(EMPTY_HOVER);
      }
    }
    x.push(point.date);
    y.push(point.dummy);
    customdata.push([point.route, point.date, point.clock, point.alarm, point.loggedAt, point.reason]);
  });

  return { x, y, customdata };
}

function buildTraces(routes) {
  const surface = cssVar("--surface-1");
  const traces = [];
  let slot = 0;

  for (const [route, points] of routes) {
    const color = cssVar(SERIES_VARS[slot % SERIES_VARS.length]);
    slot += 1;

    if (state.showPolls) {
      traces.push({
        type: "scatter",
        mode: "markers",
        name: "every poll",
        legendgroup: route,
        legendgrouptitle: { text: route },
        x: points.map((p) => p.date),
        y: points.map((p) => p.dummy),
        customdata: toCustomData(points),
        hovertemplate: HOVER_TEMPLATE,
        marker: { symbol: "x-thin", size: 8, opacity: 0.45, color, line: { width: 1.5, color } },
      });
    }

    const finals = seriesWithGaps(perDateSummary(points));
    traces.push({
      type: "scatter",
      mode: "lines+markers",
      name: "final schedule",
      legendgroup: route,
      legendgrouptitle: { text: route },
      x: finals.x,
      y: finals.y,
      customdata: finals.customdata,
      hovertemplate: HOVER_TEMPLATE,
      connectgaps: false,
      // Spline reads as a trend line between one point per day. Smoothing is
      // kept below Plotly's 1.3 maximum so the curve does not overshoot far
      // past a marker and imply a schedule that was never logged.
      line: { width: 2, color, shape: "spline", smoothing: 0.8 },
      // 2px surface ring keeps overlapping markers separable.
      marker: { size: 9, color, line: { width: 2, color: surface } },
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
 *
 * Weekday is read in UTC from the plain `YYYY-MM-DD` string so the viewer's own
 * timezone cannot shift a band onto the neighbouring day. Each band is centred
 * on its date, spanning midday to midday, so the marker sits in the middle.
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
      groupclick: "togglegroup",
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
  toImageButtonOptions: { filename: "bus-schedule-history", scale: 2 },
};

function renderTable(routes) {
  const rows = [];
  for (const [, points] of routes) {
    rows.push(...perDateSummary(points));
  }
  rows.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));

  el.tableBody.innerHTML = "";
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const [value, dim] of [
      [row.date, false], [row.route, false], [row.clock, false],
      [row.alarm, false], [String(row.polls), true], [row.loggedAt, true], [row.reason, true],
    ]) {
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
  const records = withinRange(state.records, state.days);
  const routes = groupByRoute(records);

  if (routes.size === 0) {
    showMessage(
      state.days
        ? `No schedules logged in the last ${state.days} days.`
        : "No schedules logged yet.",
    );
    el.tableView.hidden = true;
    Plotly.purge(el.chart);
    return;
  }

  hideMessage();
  const dates = [...new Set(records.map((r) => (splitIso(r.eta_iso) || {}).date).filter(Boolean))].sort();
  Plotly.react(el.chart, buildTraces(routes), buildLayout(dates), PLOT_CONFIG);
  renderTable(routes);
}

function updateSubtitle() {
  const routes = new Set(state.records.map((r) => r.route_id));
  const last = state.records[state.records.length - 1];
  const logged = last ? splitIso(last.timestamp) : null;
  const routeText = routes.size === 1 ? [...routes][0] : `${routes.size} routes`;
  el.subtitle.textContent = logged
    ? `${routeText} · ${state.records.length} logged schedules · last run ${logged.date} ${logged.clock}`
    : routeText;
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

  el.showPolls.addEventListener("change", () => {
    state.showPolls = el.showPolls.checked;
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
    showMessage(`Could not load schedule data: ${error.message}`, true);
    return;
  }

  state.records = Array.isArray(payload.records) ? payload.records : [];
  if (state.records.length === 0) {
    el.subtitle.textContent = "No data yet — run set_alarm_with_bus_eta.py with -log_url.";
    showMessage("No schedules logged yet.");
    return;
  }

  updateSubtitle();
  wireControls();
  render();
}

main();
