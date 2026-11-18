/* Fleet reliability UI.
   Charts are plain SVG: 2px lines, hairline grid, 10% area bands, 2px surface
   ring on focus dots, crosshair + tooltip.  Every value in a tooltip is also in
   the table view.  All data-derived strings are inserted with textContent. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const SERIES = ["overall", "safe", "watch", "danger"];
const SERIES_VAR = ["--series-1", "--series-2", "--series-3", "--series-4"];
const SERIES_LABEL = {
  overall: "All devices",
  safe: "Safe band",
  watch: "Watch band",
  danger: "Danger band",
};
const STATUS_LABEL = {
  faulted: "faulted",
  not_yet_failed: "not yet failed",
  exited: "exited observation",
  pre_entry_fault: "pre-entry fault",
  not_yet_observed: "not yet observed",
  void: "void window",
};
const STATUS_VAR = {
  faulted: "--critical",
  exited: "--warning",
  not_yet_failed: "--good",
  pre_entry_fault: "--serious",
  not_yet_observed: "--text-muted",
  void: "--text-muted",
};

const state = {
  status: null,
  reports: [],
  report: null,
  compareReport: null,
  compare: null,
  buildTimer: null,
  selectedDevice: null,
  devices: [],
  intervals: null,
};

/* ---------------- helpers ---------------- */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "style") node.setAttribute("style", v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}
function svgEl(tag, attrs = {}) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    n.setAttribute(k, v);
  }
  return n;
}
const fmtInt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US"));
const fmt1 = (n) => (n === null || n === undefined ? "—" : Number(n).toFixed(1));
const fmtDays = (n) => {
  if (n === null || n === undefined) return "—";
  const d = Math.round(n);
  if (Math.abs(d) >= 365) return `${(d / 365).toFixed(1)} yr`;
  return `${d} d`;
};
const pct = (n) => (n === null || n === undefined ? "—" : `${(n * 100).toFixed(1)}%`);
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}
function seriesColor(cohort) {
  const i = SERIES.indexOf(cohort);
  return cssVar(SERIES_VAR[i < 0 ? 0 : i]);
}

/* ---------------- tabs ---------------- */
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("is-active", t === tab));
    $$(".view").forEach((v) => v.classList.toggle("is-active",
      v.id === `view-${tab.dataset.view}`));
    if (tab.dataset.view === "builds") loadBuilds();
    if (tab.dataset.view === "reports") loadReports();
    if (tab.dataset.view === "data") loadBatches();
    if (tab.dataset.view === "devices" && !state.devices.length) loadDevices();
    if (tab.dataset.view === "compare" && !state.compare) runCompare();
  });
});

/* ---------------- status / header ---------------- */
async function loadStatus() {
  state.status = await api("/api/status");
  const d = state.status.dataset;
  const meta = $("#topmeta");
  meta.replaceChildren(
    el("div", {}, `data revision ${d.revision}`),
    el("div", {}, `${fmtInt(d.devices)} devices · ${fmtInt(d.reports)} reports`),
    el("div", {}, state.status.algorithm_version),
  );
  $("#build-strategy").replaceChildren(
    ...Object.entries(state.status.strategies).map(([k, v]) =>
      el("option", { value: k, selected: k === state.status.default_strategy }, v.label)),
  );
  renderCaveats([]);
}

/* ---------------- report selection ---------------- */
async function loadReportIndex() {
  const data = await api("/api/reports");
  state.reports = data.reports;
  const sel = $("#report-select");
  const cmp = $("#report-compare");
  sel.replaceChildren();
  cmp.replaceChildren(el("option", { value: "" }, "none"));
  if (!data.reports.length) {
    sel.append(el("option", { value: "" }, "no reports yet"));
    state.report = null;
    renderEmptyCurves("No report yet. Open Builds, start a build, and a report will appear here.");
    return;
  }
  for (const r of data.reports) {
    const label = `#${r.id} · ${r.as_of} · ${r.strategy} · rev ${r.data_revision}${r.stale ? " · stale" : ""}`;
    sel.append(el("option", { value: String(r.id) }, label));
    cmp.append(el("option", { value: String(r.id) }, label));
  }
  const current = state.report?._report?.id;
  const pick = current && data.reports.some((r) => r.id === current)
    ? current : data.reports[0].id;
  sel.value = String(pick);
  await selectReport(pick);
}
async function selectReport(id) {
  state.report = await api(`/api/reports/${id}`);
  if (state.compareReport && state.compareReport._report.id === id) {
    state.compareReport = null;
    $("#report-compare").value = "";
  }
  $("#report-select").value = String(id);
  renderCurves();
  renderStaleNote();
  renderReportMeta();
}
$("#report-select").addEventListener("change", async (e) => {
  if (e.target.value) await selectReport(Number(e.target.value));
});
$("#report-compare").addEventListener("change", async (e) => {
  const id = Number(e.target.value);
  if (!id) {
    state.compareReport = null;
    renderCurves();
    return;
  }
  try {
    state.compareReport = await api(`/api/reports/${id}`);
  } catch (err) {
    state.compareReport = null;
    feedback("#curve-hint", `Could not load report #${id}: ${err.message}`, "warn");
  }
  renderCurves();
});
$("#cohort-chips").addEventListener("change", () => renderCurves());
$("#band-select").addEventListener("change", () => renderCurves());

function renderStaleNote() {
  const host = $("#stale-note");
  const r = state.report?._report;
  host.replaceChildren();
  if (!r) return;
  if (r.stale) {
    host.append(el("div", { class: "warnbox" },
      `This report used data revision ${r.data_revision}; data is now at revision ` +
      `${r.current_revision}. It is still shown exactly as built. Start a build to ` +
      `see the revised cohorts — the old report stays in Reports.`));
  }
}

function renderReportMeta() {
  const r = state.report?._report;
  if (!r) return;
  const meta = $("#topmeta");
  meta.replaceChildren(
    el("div", {}, `report #${r.id} · as of ${r.as_of}`),
    el("div", {}, `data revision ${r.data_revision}${r.stale ? " (stale)" : ""}`),
    el("div", {}, `${r.strategy} · ${state.report.meta.algorithm_version}`),
  );
}

function renderEmptyCurves(msg) {
  $("#curve-chart").replaceChildren(el("p", { class: "empty" }, msg));
  $("#atrisk-table").replaceChildren();
  $("#curve-legend").replaceChildren();
}

/* ---------------- curve chart ---------------- */
function visibleCohorts() {
  return [...$("#cohort-chips").querySelectorAll("input:checked")].map((i) => i.value)
    .filter((c) => SERIES.includes(c));
}
function bandMode() { return $("#band-select").value; }

function survivalAt(points, age) {
  let s = 1;
  for (const p of points) {
    if (p.age <= age + 1e-9) s = p.survival; else break;
  }
  return s;
}
function stepPath(points, x, y) {
  if (!points.length) return "";
  let d = `M${x(points[0].age)},${y(points[0].survival)}`;
  for (let i = 1; i < points.length; i++) {
    const p = points[i];
    d += `H${x(p.age)}V${y(p.survival)}`;
  }
  return d;
}

function renderCurves() {
  const report = state.report;
  if (!report) return;
  const host = $("#curve-chart");
  host.replaceChildren();
  const cohorts = visibleCohorts();
  const mode = bandMode();
  const primary = report.cohorts["overall"];
  const comparison = state.compareReport;
  const lastAge = Math.max(
    ...cohorts.map((c) => report.cohorts[c]?.last_observed_age || 0),
    ...(comparison
      ? cohorts.map((c) => comparison.cohorts[c]?.last_observed_age || 0)
      : []),
    1);
  const xMax = Math.max(lastAge, 30);

  $("#curve-title").textContent = "Fault-free survival by risk set";
  $("#curve-caption").textContent =
    `Cutoff ${report._report.as_of} · ` +
    `Kaplan–Meier with delayed entry · age measured from the service-clock origin. ` +
    `Bands: ${mode === "both" ? "Greenwood (shaded) and bootstrap (dashed)" :
      mode === "none" ? "none" : mode}.` +
    (comparison
      ? ` Comparing with report #${comparison._report.id}` +
        ` (${comparison._report.as_of}, rev ${comparison._report.data_revision}),` +
        ` drawn as thin dashed lines in the same colours.` +
        (comparison._report.as_of !== report._report.as_of
          ? " Note: the two reports use different cutoffs, so their curves are" +
            " not directly comparable; align the cutoff to compare."
          : "")
      : "");

  if (!cohorts.length) {
    host.append(el("p", { class: "empty" }, "Select at least one risk set."));
    $("#curve-legend").replaceChildren();
    return;
  }

  const W = Math.max(host.clientWidth || 720, 460);
  const H = 380;
  const M = { top: 14, right: 74, bottom: 40, left: 52 };
  const iw = W - M.left - M.right;
  const ih = H - M.top - M.bottom;
  const x = (v) => M.left + (Math.min(v, xMax) / xMax) * iw;
  const y = (v) => M.top + (1 - Math.max(0, Math.min(1, v))) * ih;

  const svg = svgEl("svg", {
    viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Fault-free survival curves for the selected risk sets",
  });

  // grid + y axis
  for (let t = 0; t <= 1.0001; t += 0.25) {
    svg.append(svgEl("line", {
      class: "gridline", x1: M.left, x2: M.left + iw, y1: y(t), y2: y(t),
    }));
    const tx = svgEl("text", {
      class: "tick", x: M.left - 8, y: y(t) + 4, "text-anchor": "end",
    });
    tx.textContent = `${Math.round(t * 100)}%`;
    svg.append(tx);
  }
  svg.append(svgEl("line", {
    class: "axisline", x1: M.left, x2: M.left + iw,
    y1: M.top + ih, y2: M.top + ih,
  }));
  const xTicks = 5;
  for (let i = 0; i <= xTicks; i++) {
    const v = (xMax / xTicks) * i;
    const tx = svgEl("text", {
      class: "tick", x: x(v), y: M.top + ih + 18, "text-anchor": "middle",
    });
    tx.textContent = v >= 365 ? `${(v / 365).toFixed(1)} yr` : `${Math.round(v)} d`;
    svg.append(tx);
  }
  const xTitle = svgEl("text", {
    class: "axis-title", x: M.left + iw / 2, y: H - 4, "text-anchor": "middle",
  });
  xTitle.textContent = "Age on service clock";
  svg.append(xTitle);
  const yTitle = svgEl("text", {
    class: "axis-title", x: 12, y: M.top + ih / 2, "text-anchor": "middle",
    transform: `rotate(-90 12 ${M.top + ih / 2})`,
  });
  yTitle.textContent = "Fault-free proportion";
  svg.append(yTitle);

  // bands behind lines
  for (const cohort of cohorts) {
    const c = report.cohorts[cohort];
    if (!c) continue;
    const color = seriesColor(cohort);
    if (mode === "both" || mode === "greenwood") {
      const g = c.greenwood.filter((p) => p.lower !== null && p.upper !== null);
      if (g.length > 1) {
        let d = `M${x(g[0].age)},${y(g[0].upper)}`;
        for (const p of g) d += `H${x(p.age)}V${y(p.upper)}`;
        for (let i = g.length - 1; i >= 0; i--) {
          const p = g[i];
          d += `H${x(p.age)}V${y(p.lower)}`;
        }
        d += "Z";
        svg.append(svgEl("path", { d, fill: color, class: "band-area", stroke: "none" }));
      }
    }
    if (mode === "both" || mode === "bootstrap") {
      const b = c.bootstrap;
      if (b && b.ages && b.ages.length > 1) {
        let up = `M${x(b.ages[0])},${y(b.upper[0])}`;
        b.ages.forEach((a, i) => { up += `H${x(a)}V${y(b.upper[i])}`; });
        let lo = `M${x(b.ages[0])},${y(b.lower[0])}`;
        b.ages.forEach((a, i) => { lo += `H${x(a)}V${y(b.lower[i])}`; });
        svg.append(svgEl("path", {
          d: up, fill: "none", stroke: color, "stroke-width": 1,
          "stroke-dasharray": "5 4", opacity: 0.55,
        }));
        svg.append(svgEl("path", {
          d: lo, fill: "none", stroke: color, "stroke-width": 1,
          "stroke-dasharray": "5 4", opacity: 0.55,
        }));
      }
    }
  }

  // lines
  const endLabels = [];
  for (const cohort of cohorts) {
    const c = report.cohorts[cohort];
    if (!c || !c.points.length) continue;
    const color = seriesColor(cohort);
    svg.append(svgEl("path", {
      d: stepPath(c.points, x, y), class: "series-line", stroke: color,
      "data-cohort": cohort,
    }));
    const last = c.points[c.points.length - 1];
    endLabels.push({ cohort, color, y: y(last.survival), s: last.survival });
  }
  // selective direct labels at line ends; skip if they would collide.
  // The overall curve is the headline and gets first claim on a label.
  endLabels.sort((a, b) => {
    if (a.cohort === "overall" && b.cohort !== "overall") return -1;
    if (b.cohort === "overall" && a.cohort !== "overall") return 1;
    return a.y - b.y;
  });
  const placed = [];
  for (const L of endLabels) {
    if (placed.some((p) => Math.abs(L.y - p) < 13)) continue;
    placed.push(L.y);
    const t = svgEl("text", {
      class: "direct-label", x: M.left + iw + 8, y: L.y + 4,
    });
    t.textContent = `${SERIES_LABEL[L.cohort]} ${(L.s * 100).toFixed(0)}%`;
    svg.append(t);
  }

  // comparison report: the same cohorts as thin dashed lines, so the two
  // reports are compared shape-for-shape on one axis.
  if (comparison) {
    for (const cohort of cohorts) {
      const cc = comparison.cohorts[cohort];
      if (!cc || !cc.points.length) continue;
      svg.append(svgEl("path", {
        d: stepPath(cc.points, x, y), class: "compare-line",
        stroke: seriesColor(cohort), "data-cohort": cohort,
        "data-report": String(comparison._report.id),
        "stroke-dasharray": "6 4", opacity: 0.75,
      }));
    }
  }

  // crosshair layer
  const cross = svgEl("line", {
    class: "crosshair", y1: M.top, y2: M.top + ih, x1: 0, x2: 0, opacity: 0,
  });
  svg.append(cross);
  const dots = cohorts.map((cohort) => {
    const dot = svgEl("circle", {
      class: "focus-dot", r: 4.5, fill: seriesColor(cohort), opacity: 0,
    });
    svg.append(dot);
    return { cohort, dot };
  });
  const hit = svgEl("rect", {
    x: M.left, y: M.top, width: iw, height: ih,
    fill: "transparent", style: "cursor:crosshair",
  });
  svg.append(hit);

  const tooltip = $("#tooltip");
  function move(evt) {
    const rect = svg.getBoundingClientRect();
    const px = ((evt.clientX - rect.left) / rect.width) * W;
    const age = Math.max(0, Math.min(xMax, ((px - M.left) / iw) * xMax));
    cross.setAttribute("x1", x(age));
    cross.setAttribute("x2", x(age));
    cross.setAttribute("opacity", 1);
    const rows = [];
    for (const { cohort, dot } of dots) {
      const c = report.cohorts[cohort];
      if (!c || !c.points.length) { dot.setAttribute("opacity", 0); continue; }
      const s = survivalAt(c.points, age);
      dot.setAttribute("cx", x(age));
      dot.setAttribute("cy", y(s));
      dot.setAttribute("opacity", 1);
      rows.push({ cohort, s });
    }
    if (comparison) {
      for (const cohort of cohorts) {
        const cc = comparison.cohorts[cohort];
        if (!cc || !cc.points.length) continue;
        rows.push({
          cohort, s: survivalAt(cc.points, age),
          report: `#${comparison._report.id}`,
        });
      }
    }
    tooltip.replaceChildren(
      el("div", { class: "tt-head" }, `age ${Math.round(age)} days`),
      ...rows.map(({ cohort, s, report: rid }) => el("div", { class: "tt-row" },
        el("span", { class: "tt-key" },
          el("span", {
            class: `tt-line${rid ? " dash" : ""}`,
            style: `--dot:${seriesColor(cohort)}`,
          }),
          `${SERIES_LABEL[cohort]}${rid ? ` · report ${rid}` : ""}`),
        el("span", { class: "tt-val" }, pct(s)))),
    );
    tooltip.hidden = false;
    const tw = tooltip.offsetWidth || 180;
    let left = evt.clientX + 16;
    if (left + tw > window.innerWidth - 8) left = evt.clientX - tw - 16;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.min(evt.clientY + 14, window.innerHeight - 120)}px`;
  }
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", () => {
    tooltip.hidden = true;
    cross.setAttribute("opacity", 0);
    dots.forEach(({ dot }) => dot.setAttribute("opacity", 0));
  });

  host.append(svg);
  renderLegend(cohorts);
  renderCurveTable(cohorts);
  renderAtRisk(cohorts);
  renderSidePanels();
  const hint = $("#curve-hint");
  hint.className = "hint";
  hint.textContent =
    `${cohorts.length} series` +
    (state.compareReport ? ` compared with report #${state.compareReport._report.id}` : "") +
    ` · hover to read all series at one age · values also in the table view.`;
}

function renderLegend(cohorts) {
  const host = $("#curve-legend");
  const keys = cohorts.map((cohort) =>
    el("span", { class: "key" },
      el("span", { class: "swatch", style: `background:${seriesColor(cohort)}` }),
      SERIES_LABEL[cohort]));
  if (bandMode() !== "none") {
    keys.push(el("span", { class: "key" },
      el("span", { class: "swatch dash" }), "bootstrap 95%"));
  }
  if (state.compareReport) {
    keys.push(el("span", { class: "key" },
      el("span", { class: "swatch dashed" }),
      `report #${state.compareReport._report.id} (thin dashed)`));
  }
  host.replaceChildren(...keys);
}

function renderCurveTable(cohorts) {
  const host = $("#curve-table");
  const report = state.report;
  // A fixed 11-point grid rather than every knot: readable, and each value is
  // the exact step value of the curve at that age (the at-risk table carries
  // the knot counts).
  const base = report.cohorts[cohorts[0]];
  const grid = (base?.at_risk_table || []).map((r) => r.age);
  const ages = grid.length ? grid : [...new Set(cohorts.flatMap((c) =>
    (report.cohorts[c]?.points || []).map((p) => p.age)))].sort((a, b) => a - b);
  const head = el("tr", {},
    el("th", { class: "num" }, "Age (d)"),
    ...cohorts.flatMap((c) => [
      el("th", { class: "num" }, `${SERIES_LABEL[c]} survival`),
      el("th", { class: "num" }, "95% band"),
    ]));
  const rows = ages.map((age) => el("tr", {},
    el("td", { class: "num" }, fmtInt(Math.round(age))),
    ...cohorts.flatMap((c) => {
      const cdata = report.cohorts[c];
      if (!cdata || !cdata.points.length) {
        return [el("td", { class: "num" }, "—"), el("td", { class: "num" }, "—")];
      }
      const s = survivalAt(cdata.points, age);
      let band = "—";
      for (const p of cdata.greenwood) {
        if (p.age <= age + 1e-9 && p.lower !== null) {
          band = `${pct(p.lower)}–${pct(p.upper)}`;
        }
      }
      return [el("td", { class: "num" }, pct(s)), el("td", { class: "num" }, band)];
    })));
  host.replaceChildren(
    el("p", { class: "caption", style: "margin-bottom:8px" },
      "Values at a fixed 11-point age grid; each is the exact step value of its " +
      "curve at that age. The at-risk table below carries counts per age."),
    el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows)));
}
$("#toggle-table").addEventListener("click", (e) => {
  const t = $("#curve-table");
  t.hidden = !t.hidden;
  e.currentTarget.setAttribute("aria-expanded", String(!t.hidden));
  e.currentTarget.textContent = t.hidden ? "Show table view" : "Hide table view";
});

function renderAtRisk(cohorts) {
  const host = $("#atrisk-table");
  const report = state.report;
  const base = report.cohorts[cohorts[0]];
  if (!base) { host.replaceChildren(); return; }
  const table = base.at_risk_table || [];
  const head = el("tr", {},
    el("th", { class: "num" }, "Age (d)"),
    ...cohorts.flatMap((c) => [
      el("th", { class: "num" }, `${SERIES_LABEL[c]} at risk`),
      el("th", { class: "num" }, "faults"),
    ]));
  const rows = table.map((row, i) => el("tr", {},
    el("td", { class: "num" }, fmtInt(Math.round(row.age))),
    ...cohorts.flatMap((c) => {
      const cdata = report.cohorts[c];
      if (!cdata) return [el("td", {}, "—"), el("td", {}, "—")];
      const t = (cdata.at_risk_table || [])[i] || {};
      return [
        el("td", { class: "num" }, fmtInt(t.at_risk ?? 0)),
        el("td", { class: "num" }, fmtInt(t.events_cum ?? 0)),
      ];
    })));
  host.replaceChildren(el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows)));
}

function renderSidePanels() {
  const report = state.report;
  const overall = report.cohorts.overall;
  // median hero
  const median = overall.median_age;
  const hero = $("#stat-median");
  hero.replaceChildren();
  if (median === null || median === undefined) {
    hero.className = "hero muted";
    hero.textContent = "Not reached";
    $("#stat-median-note").textContent = overall.median_note ||
      "Survival never fell to 50% within observed ages.";
  } else {
    hero.className = "hero";
    hero.append(document.createTextNode(fmtDays(median)),
      el("span", { class: "unit" }, `${Math.round(median)} days`));
    $("#stat-median-note").textContent =
      "First observed crossing of 50% on the age scale, interpolated between " +
      "the bracketing knots. Not a prediction beyond observed ages.";
  }

  // censoring breakdown, three codes kept apart
  const cg = $("#censor-breakdown");
  cg.replaceChildren(
    censorCell("Faulted (event)", overall.n_events, STATUS_VAR.faulted),
    censorCell("Not yet failed (open)", overall.n_censored_open, STATUS_VAR.not_yet_failed),
    censorCell("Exited observation", overall.n_censored_exited, STATUS_VAR.exited),
    censorCell("Band transitions", overall.n_censored_transition, "--text-muted"),
  );

  // truncation
  const tr = report.truncation || {};
  $("#truncation-block").replaceChildren(
    kv("Devices analysed", fmtInt(tr.count)),
    kv("Entered late (pre-entry > 0)", fmtInt(tr.with_pre_entry)),
    kv("Median pre-entry", fmtDays(tr.median)),
    kv("Max pre-entry", fmtDays(tr.max)),
    el("p", { class: "caption", style: "margin-top:8px" }, tr.note || ""),
  );

  // status composition
  const counts = report.status_counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
  const bar = el("div", { class: "stackbar", role: "img",
    "aria-label": "Status composition of devices in this report" });
  const list = el("div", { class: "legendlist" });
  for (const [key, n] of Object.entries(counts)) {
    bar.append(el("span", {
      style: `width:${(n / total) * 100}%;background:var(${STATUS_VAR[key] || "--text-muted"})`,
      title: `${STATUS_LABEL[key] || key}: ${n}`,
    }));
    list.append(el("div", { class: "item" },
      el("span", { class: "sw", style: `background:var(${STATUS_VAR[key] || "--text-muted"})` }),
      STATUS_LABEL[key] || key,
      el("span", { class: "val" }, fmtInt(n))));
  }
  const pre = counts.pre_entry_fault || 0;
  $("#status-block").replaceChildren(bar, list,
    el("p", { class: "caption", style: "margin-top:8px" },
      pre
        ? `${pre} device(s) had a fault before observation with no repair; their ` +
          `entry state is unknown, so they are excluded from risk sets rather than assumed healthy.`
        : "All observed devices had a known entry state."));

  // naive vs KM
  const nv = report.naive_comparison;
  const host = $("#naive-block");
  host.replaceChildren(
    el("div", { class: "box" },
      el("div", { class: "n" }, nv.naive_mean_age_at_fault === null
        ? "—" : fmtDays(nv.naive_mean_age_at_fault)),
      el("div", { class: "l" }, "Naive mean age at first fault"),
      el("div", { class: "d" },
        `faulted devices only (n=${nv.naive_n_faulted}); omits ` +
        `${nv.censored_devices_excluded_from_naive} censored device(s)`)),
    el("div", { class: "box" },
      el("div", { class: "n" }, nv.km_median_age === null
        ? "not reached" : fmtDays(nv.km_median_age)),
      el("div", { class: "l" }, "Kaplan–Meier median"),
      el("div", { class: "d" }, "uses all observed exposure; delayed entry")),
    el("div", { class: "box" },
      el("div", { class: "n" }, fmtDays(nv.km_restricted_mean_age)),
      el("div", { class: "l" }, "Restricted mean age"),
      el("div", { class: "d" }, "area under the curve to the last observed age")),
    el("div", { class: "warnbox", style: "grid-column:1 / -1" }, nv.warning),
  );
  renderCaveats(report.caveats || []);
}

function censorCell(label, n, varName) {
  return el("div", { class: "censor-cell" },
    el("div", { class: "lbl" },
      el("span", { class: "dot", style: `background:var(${varName})` }), label),
    el("div", { class: "n" }, fmtInt(n)));
}
function kv(k, v) {
  return el("div", { class: "kv" }, el("span", { class: "k" }, k),
    el("span", { class: "v" }, v));
}
function renderCaveats(items) {
  const host = $("#caveats");
  host.replaceChildren(...(items.length ? items : [
    "Fictional, synthetic data. No medical or individual diagnostic conclusion may be drawn.",
    "Descriptive estimates only; no extrapolation beyond observed ages.",
  ]).map((c) => el("li", {}, c)));
}

/* ---------------- devices ---------------- */
async function loadDevices() {
  const q = encodeURIComponent($("#device-search").value.trim());
  const data = await api(`/api/devices?q=${q}&limit=400`);
  state.devices = data.devices;
  renderDeviceList();
}
async function renderDeviceList() {
  const host = $("#device-list");
  const report = state.report;
  let entries = state.devices.map((d) => ({ ...d }));
  if (report) {
    const rd = await api(`/api/reports/${report._report.id}/devices?limit=2000`);
    const map = new Map(rd.devices.map((x) => [x.device_id, x]));
    entries = entries
      .filter((d) => map.has(d.id) || !report)
      .map((d) => ({ ...d, rep: map.get(d.id) }));
  }
  const statusFilter = $("#device-status").value;
  const cohortFilter = $("#device-cohort").value;
  entries = entries.filter((d) => {
    if (statusFilter && d.rep?.status !== statusFilter) return false;
    if (cohortFilter && !(d.rep?.entered_sets || []).includes(cohortFilter)) return false;
    return true;
  });
  $("#device-count").textContent = `${entries.length} shown`;
  if (!entries.length) {
    host.replaceChildren(el("p", { class: "empty" }, "No devices match."));
    return;
  }
  host.replaceChildren(...entries.map((d) => {
    const item = el("div", {
      class: "devitem" + (d.id === state.selectedDevice ? " is-selected" : ""),
      role: "option", tabindex: "0", "aria-selected": String(d.id === state.selectedDevice),
      onclick: () => openDevice(d.id),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDevice(d.id); } },
    },
      el("span", { class: "id" }, d.id),
      el("span", { class: "meta" }, d.model),
      el("span", { class: "meta" },
        `${d.site} · obs ${d.observation_start_date}${d.observation_end_date ? ` → ${d.observation_end_date}` : " → open"}`),
      el("span", { class: "sets" },
        ...(d.rep ? [el("span", { class: "setpill" },
          STATUS_LABEL[d.rep.status] || d.rep.status)] : []),
        ...(d.rep?.entered_sets || []).map((s) =>
          el("span", { class: "setpill", style: "border-color:currentColor" }, s))),
    );
    return item;
  }));
}
$("#device-search").addEventListener("input", debounce(loadDevices, 250));
$("#device-status").addEventListener("change", renderDeviceList);
$("#device-cohort").addEventListener("change", renderDeviceList);

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function openDevice(id) {
  state.selectedDevice = id;
  renderDeviceList();
  const host = $("#device-detail");
  host.replaceChildren(el("p", { class: "empty" }, "Loading…"));
  const [detail, intervals] = await Promise.all([
    api(`/api/devices/${encodeURIComponent(id)}`),
    api(`/api/devices/${encodeURIComponent(id)}/intervals` +
        (state.report ? `?as_of=${state.report._report.as_of}` : "")),
  ]);
  state.intervals = intervals;
  host.replaceChildren(renderDeviceDetail(detail, intervals));
}

function renderDeviceDetail(d, iv) {
  const statusVar = STATUS_VAR[d.risk_explanations?.[0]?.status] || "--text-muted";
  const head = el("div", { class: "detail-head" },
    el("div", {},
      el("h2", {}, d.device_id),
      el("p", { class: "caption" },
        `${d.model} · ${d.site} · commissioned ${d.commissioned_date}`)),
    el("span", { class: "badge" },
      el("span", { class: "dot", style: `background:var(${statusVar})` }),
      STATUS_LABEL[iv.status] || iv.status));

  // timeline
  const tl = el("div", { class: "timeline" },
    timelineRow("Service clock", iv, "cohort"));
  const lastAge = Math.max(iv.followup_age, iv.first_fault_age || 0, 1);
  const axis = el("div", { class: "tlaxis" },
    el("span", {}, `0 d`),
    el("span", {}, `entry ${fmtDays(iv.entry_age)}`),
    el("span", {}, `${fmtDays(lastAge)}`));

  // observation editor
  const editForm = el("form", { class: "card", style: "margin-top:14px" },
    el("h3", {}, "Revise observation window"),
    el("p", { class: "caption" },
      "Edits are recorded as events with the previous window; they bump the data " +
      "revision and mark reports stale. Faults are corrected through ingestion, not here."),
    el("div", { class: "row", style: "margin-top:8px" },
      el("label", { class: "field" }, el("span", {}, "Start"),
        el("input", { type: "date", name: "start", value: d.observation_start_date })),
      el("label", { class: "field" }, el("span", {}, "End (blank = still observed)"),
        el("input", { type: "date", name: "end", value: d.observation_end_date || "" })),
    ),
    el("label", { class: "field block", style: "margin-top:8px" },
      el("span", {}, "Note"),
      el("input", { type: "text", name: "note", placeholder: "why the window changed" })),
    el("div", { class: "row", style: "margin-top:10px" },
      el("button", { class: "btn primary", type: "submit" }, "Save revision")),
    el("div", { class: "feedback", "aria-live": "polite" }),
  );
  editForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(editForm);
    const body = {
      observation_start_date: f.get("start") || null,
      observation_end_date: f.get("end") || null,
      note: f.get("note") || "edited in web UI",
      batch_id: `ui-${d.device_id}-${Date.now()}`,
    };
    const fb = editForm.querySelector(".feedback");
    try {
      const res = await api(`/api/devices/${encodeURIComponent(d.device_id)}/observation`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body) });
      fb.className = "feedback ok";
      fb.textContent = `Accepted at data revision ${res.data_revision}. ` +
        `Reports built earlier are now stale; rebuild to see the change.`;
      await loadStatus();
      await loadReportIndex();
    } catch (err) {
      fb.className = "feedback err";
      fb.textContent = err.message;
    }
  });

  const faults = d.faults.length
    ? el("div", { class: "tablewrap" }, el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Fault date"), el("th", {}, "Component"),
          el("th", {}, "Severity"), el("th", { class: "num" }, "Reported"),
          el("th", { class: "num" }, "Late by"), el("th", { class: "num" }, "Rev"))),
        el("tbody", {}, ...d.faults.map((f) => el("tr", {},
          el("td", {}, f.fault_date), el("td", {}, f.component),
          el("td", {}, f.severity), el("td", { class: "num" }, f.reported_date),
          el("td", { class: "num" }, `${fmtInt(f.late_report_days)} d`),
          el("td", { class: "num" }, String(f.revision)))))))
    : el("p", { class: "empty" }, "No effective fault records.");

  const history = d.fault_history.length
    ? el("div", { class: "tablewrap" }, el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Event key"), el("th", { class: "num" }, "Rev"),
          el("th", {}, "Fault date"), el("th", {}, "Component"),
          el("th", {}, "Status"), el("th", { class: "num" }, "Revision"))),
        el("tbody", {}, ...d.fault_history.map((f) => el("tr", {},
          el("td", { class: "report-link" }, f.event_key),
          el("td", { class: "num" }, String(f.revision)),
          el("td", {}, f.fault_date), el("td", {}, f.component),
          el("td", {}, el("span", { class: "badge" + (f.retracted ? " stale" : " ok") },
            f.retracted ? "retracted" : "effective")),
          el("td", { class: "num" }, String(f.data_revision)))))))
    : el("p", { class: "empty" }, "No fault revisions.");

  const obsHist = (d.observation_history || []).length
    ? el("div", { class: "tablewrap" }, el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Previous"), el("th", {}, "New"),
          el("th", {}, "Note"), el("th", { class: "num" }, "Revision"))),
        el("tbody", {}, ...d.observation_history.map((o) => el("tr", {},
          el("td", {}, `${o.prev_start} → ${o.prev_end || "open"}`),
          el("td", {}, `${o.new_start} → ${o.new_end || "open"}`),
          el("td", {}, o.note || ""),
          el("td", { class: "num" }, String(o.data_revision)))))))
    : el("p", { class: "empty" }, "Observation window never revised.");

  const explanations = (d.risk_explanations || []).slice(0, 3).map((x) => {
    const b = x.basis || {};
    return el("div", { class: "basis" },
      el("div", { class: "head" },
        el("strong", {}, `Report #${x.report_id}`),
        el("span", { class: "tag" }, `as of ${x.as_of}`),
        el("span", { class: "tag" }, `rev ${x.data_revision}`),
        el("span", { class: "tag" }, STATUS_LABEL[x.status] || x.status)),
      el("div", {}, b.status_reason || ""),
      el("dl", {},
        el("dt", {}, "entered sets"),
        el("dd", {}, x.entered_sets.length ? x.entered_sets.join(", ") : "none"),
        el("dt", {}, "pre-entry"),
        el("dd", {}, `${fmtInt(x.pre_entry_days)} d`),
        el("dt", {}, "entry age"),
        el("dd", {}, fmtDays(x.entry_age)),
        el("dt", {}, "clock origin"),
        el("dd", {}, b.clock_origin || "—"),
        el("dt", {}, "service interval"),
        el("dd", {}, b.service_interval_days ? `${b.service_interval_days} d` : "—"),
        el("dt", {}, "bands entered"),
        el("dd", {}, (b.bands_entered || []).join(", ") || "—"),
      ),
    );
  });

  const intervalTable = el("div", { class: "tablewrap" }, el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "Cohort"), el("th", {}, "From"), el("th", {}, "To"),
      el("th", { class: "num" }, "Entry age"), el("th", { class: "num" }, "End age"),
      el("th", {}, "Ends with"), el("th", {}, "Why this set"))),
    el("tbody", {}, ...iv.intervals.map((v) => el("tr", {},
      el("td", {}, v.cohort),
      el("td", {}, v.start_date), el("td", {}, v.end_date),
      el("td", { class: "num" }, fmtInt(Math.round(v.start_age))),
      el("td", { class: "num" }, fmtInt(Math.round(v.end_age))),
      el("td", {}, v.end_reason),
      el("td", {}, v.basis.band_rule || "delayed-entry overall row"))))));

  return el("div", {},
    head,
    el("p", { class: "caption", style: "margin-top:6px" }, iv.basis.status_reason || ""),
    el("div", { class: "sectiontitle" }, "Observed life on the service clock"),
    el("p", { class: "caption" },
      `Pre-entry (unobserved): ${fmtInt(d.pre_entry_days)} d · ` +
      `entry age ${fmtDays(iv.entry_age)} · observed to ${fmtDays(iv.followup_age)}` +
      (iv.first_fault_age !== null ? ` · first fault at ${fmtDays(iv.first_fault_age)}` : "")),
    tl, axis,
    el("div", { class: "sectiontitle" }, "Risk intervals at "
      + (state.report ? state.report._report.as_of : iv.as_of)),
    intervalTable,
    editForm,
    el("div", { class: "sectiontitle" }, "Why each risk set"),
    el("div", { class: "basislist" }, ...(explanations.length ? explanations
      : [el("p", { class: "empty" }, "No report has included this device yet.")])),
    el("div", { class: "sectiontitle" }, "Effective faults"),
    faults,
    el("div", { class: "sectiontitle" }, "Fault revision history"),
    history,
    el("div", { class: "sectiontitle" }, "Observation window history"),
    obsHist,
  );
}

function timelineRow(label, iv, key) {
  const total = Math.max(iv.followup_age, iv.first_fault_age || 0, 1);
  const segs = iv.intervals.filter((v) => v.cohort !== "overall");
  const row = el("div", { class: "tlrow" },
    el("span", { class: "meta" }, label),
    el("div", { class: "tlbar", role: "img",
      "aria-label": "risk-set bands over observed life" }));
  for (const s of segs) {
    row.lastChild.append(el("span", {
      class: "tlseg",
      style: `left:${(s.start_age / total) * 100}%;width:${((s.end_age - s.start_age) / total) * 100}%;` +
        `background:${seriesColor(s.cohort)}`,
      title: `${s.cohort}: ${s.start_date} → ${s.end_date}`,
    }));
  }
  // mark entry and fault
  if (iv.entry_age > 0) {
    row.lastChild.append(el("span", {
      style: `position:absolute;left:${(iv.entry_age / total) * 100}%;top:-2px;bottom:-2px;` +
        `width:2px;background:var(--text-muted)`,
      title: `observation entry at age ${Math.round(iv.entry_age)} d`,
    }));
  }
  if (iv.first_fault_age !== null) {
    row.lastChild.append(el("span", {
      style: `position:absolute;left:calc(${(iv.first_fault_age / total) * 100}% - 3px);` +
        `top:-3px;width:8px;height:8px;border-radius:50%;background:var(--critical);` +
        `box-shadow:0 0 0 2px var(--surface-2)`,
      title: `first fault at age ${Math.round(iv.first_fault_age)} d`,
    }));
  }
  return row;
}

/* ---------------- builds ---------------- */
async function loadBuilds() {
  const builds = await api("/api/builds");
  const host = $("#build-list");
  const parent = $("#build-parent");
  const done = builds.filter((b) => b.status === "done");
  const prev = parent.value;
  parent.replaceChildren(el("option", { value: "" }, "full rebuild"));
  for (const b of done) {
    parent.append(el("option", { value: String(b.id) },
      `#${b.id} · ${b.as_of} · rev ${b.data_revision}`));
  }
  if (prev) parent.value = prev;

  if (!builds.length) {
    host.replaceChildren(el("p", { class: "empty" }, "No builds yet."));
    return;
  }
  const head = el("tr", {},
    el("th", { class: "num" }, "Build"), el("th", {}, "Kind"), el("th", {}, "Strategy"),
    el("th", {}, "As of"), el("th", { class: "num" }, "Revision"),
    el("th", {}, "Progress"), el("th", {}, "Status"), el("th", {}, "Report"), el("th", {}));
  const rows = builds.map((b) => {
    const progress = el("div", { class: "stackbar", style: "width:120px;margin:0" },
      el("span", { style: `width:${Math.round(b.progress * 100)}%;background:var(--series-1)` }));
    const active = ["queued", "running", "paused"].includes(b.status);
    const actions = el("td", {},
      ...(active ? [
        b.status === "paused" || b.status === "queued"
          ? el("button", { class: "btn small", onclick: () => buildAction(b.id, "resume") }, "Resume")
          : el("button", { class: "btn small", onclick: () => buildAction(b.id, "pause") }, "Pause"),
        el("button", { class: "btn small danger", onclick: () => buildAction(b.id, "cancel") }, "Cancel"),
      ] : b.report_id ? [
        el("button", { class: "btn small", onclick: async () => {
          await selectReport(b.report_id); activateTab("curves");
        } }, "Open curves"),
      ] : []),
    );
    return el("tr", {},
      el("td", { class: "num" }, String(b.id)),
      el("td", {}, b.kind + (b.parent_build_id ? ` ← #${b.parent_build_id}` : "")),
      el("td", {}, b.strategy),
      el("td", {}, b.as_of),
      el("td", { class: "num" }, String(b.data_revision)),
      el("td", {}, progress),
      el("td", {}, el("span", { class: "badge " + (b.status === "done" ? "ok" :
        b.status === "failed" ? "stale" : "run") }, b.status)),
      el("td", {}, b.report_id ? `#${b.report_id}` : "—"),
      actions);
  });
  host.replaceChildren(el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows)));

  const anyActive = builds.some((b) => ["queued", "running"].includes(b.status));
  clearTimeout(state.buildTimer);
  if (anyActive) state.buildTimer = setTimeout(loadBuilds, 1200);
}

async function buildAction(id, action) {
  try {
    await api(`/api/builds/${id}/${action}`, { method: "POST" });
    await loadBuilds();
  } catch (e) {
    feedback("#build-feedback", e.message, "err");
  }
}
function feedback(sel, msg, kind = "") {
  const host = $(sel);
  host.className = `feedback ${kind}`;
  host.textContent = msg;
}

$("#build-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    as_of: $("#build-asof").value || null,
    strategy: $("#build-strategy").value,
    parent_build_id: $("#build-parent").value ? Number($("#build-parent").value) : null,
    bootstrap_reps: Number($("#build-reps").value),
  };
  if (!body.as_of && body.parent_build_id) {
    const b = (await api("/api/builds")).find((x) => x.id === body.parent_build_id);
    body.as_of = b?.as_of;
  }
  try {
    const res = await api("/api/builds", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    feedback("#build-feedback", `Build #${res.build_id} ${res.status}.`, "ok");
    await loadBuilds();
  } catch (err) {
    feedback("#build-feedback", err.message, "err");
  }
});

/* ---------------- compare ---------------- */
async function runCompare() {
  const host = $("#compare-out");
  host.replaceChildren(el("p", { class: "empty" }, "Measuring both arms on the current data…"));
  const asOf = $("#compare-asof").value;
  try {
    state.compare = await api("/api/compare" + (asOf ? `?as_of=${asOf}` : ""));
    renderCompare();
  } catch (e) {
    host.replaceChildren(el("p", { class: "err" }, e.message));
  }
}
$("#compare-run").addEventListener("click", runCompare);

function renderCompare() {
  const c = state.compare;
  const host = $("#compare-out");
  if (!c) return;
  const arms = Object.entries(c.arms);
  const children = [
    el("p", { class: "caption" }, c.headline),
    el("div", { class: "metric" },
      el("div", { class: "m" }, el("div", { class: "n" }, fmtInt(c.devices)),
        el("div", { class: "l" }, "devices compared")),
      el("div", { class: "m" }, el("div", { class: "n" }, fmtInt(c.interval_cache?.rows || 0)),
        el("div", { class: "l" }, "cached interval rows present")),
      el("div", { class: "m" }, el("div", { class: "n" }, `${(c.db_bytes / 1048576).toFixed(1)} MB`),
        el("div", { class: "l" }, "database size")),
      el("div", { class: "m" }, el("div", { class: "n" }, c.as_of),
        el("div", { class: "l" }, "cutoff"))),
  ];
  for (const [key, arm] of arms) {
    children.push(el("div", { class: "arm" },
      el("div", {},
        el("h3", {}, arm.label),
        el("p", { class: "caption" }, arm.description),
        el("p", { class: "caption" }, arm.cost)),
      el("div", { class: "metric", style: "margin:0;grid-template-columns:1fr 1fr" },
        el("div", { class: "m" }, el("div", { class: "n" }, `${arm.elapsed_ms} ms`),
          el("div", { class: "l" }, "measured derive time")),
        el("div", { class: "m" }, el("div", { class: "n" }, fmtInt(arm.intervals_derived)),
          el("div", { class: "l" }, "intervals derived")),
        el("div", { class: "m" }, el("div", { class: "n" }, fmtInt(arm.devices_reusing_cache)),
          el("div", { class: "l" }, "devices reusing cache")),
        el("div", { class: "m" }, el("div", { class: "n" },
          fmtInt(arm.interval_rows_persisted_if_materialized)),
          el("div", { class: "l" }, "interval rows written")),
      )));
  }
  children.push(el("div", { class: "warnbox" }, c.cost_note));
  host.replaceChildren(...children);
}

/* ---------------- reports ---------------- */
async function loadReports() {
  const data = await api("/api/reports");
  const host = $("#report-list");
  if (!data.reports.length) {
    host.replaceChildren(el("p", { class: "empty" }, "No reports yet."));
    return;
  }
  const head = el("tr", {},
    el("th", { class: "num" }, "Report"), el("th", {}, "As of"),
    el("th", {}, "Strategy"), el("th", { class: "num" }, "Revision"),
    el("th", {}, "Created"), el("th", {}, "Supersedes"), el("th", {}, "State"), el("th", {}));
  const rows = data.reports.map((r) => el("tr", {},
    el("td", { class: "num" }, String(r.id)),
    el("td", {}, r.as_of), el("td", {}, r.strategy),
    el("td", { class: "num" }, String(r.data_revision)),
    el("td", {}, r.created_at),
    el("td", {}, r.supersedes ? `#${r.supersedes}` : "—"),
    el("td", {}, el("span", { class: "badge " + (r.stale ? "stale" : "ok") },
      r.stale ? "stale" : "current")),
    el("td", {}, el("button", { class: "btn small", onclick: async () => {
      await openReportDetail(r.id);
    } }, "View"))));
  host.replaceChildren(el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows)));
}

async function openReportDetail(id) {
  const host = $("#report-detail");
  host.replaceChildren(el("p", { class: "empty" }, "Loading…"));
  const r = await api(`/api/reports/${id}`);
  const m = r.meta;
  host.replaceChildren(
    el("h2", {}, `Report #${r._report.id}: ${r._report.title}`),
    el("p", { class: "caption" },
      `As of ${r._report.as_of} · data revision ${r._report.data_revision}` +
      `${r._report.stale ? ` (current ${r._report.current_revision})` : ""} · ` +
      `${m.strategy_label} · ${m.algorithm_version} · created ${r._report.created_at}`),
    el("div", { class: "metric" },
      el("div", { class: "m" }, el("div", { class: "n" }, fmtInt(r.cohorts.overall.n_devices)),
        el("div", { class: "l" }, "devices in overall cohort")),
      el("div", { class: "m" }, el("div", { class: "n" }, fmtInt(r.cohorts.overall.n_events)),
        el("div", { class: "l" }, "first faults observed")),
      el("div", { class: "m" }, el("div", { class: "n" }, fmtDays(r.cohorts.overall.median_age)),
        el("div", { class: "l" }, "median age")),
      el("div", { class: "m" }, el("div", { class: "n" },
        fmtInt(r.truncation.with_pre_entry)),
        el("div", { class: "l" }, "devices with pre-entry time"))),
    el("div", { class: "row", style: "margin:12px 0" },
      el("button", { class: "btn primary", onclick: async () => {
        await selectReport(r._report.id); activateTab("curves");
      } }, "Compare curves"),
      el("button", { class: "btn", onclick: () => startBuildFrom(r) }, "Rebuild from this cutoff")),
    el("div", { class: "sectiontitle" }, "Cohorts"),
    el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Cohort"), el("th", { class: "num" }, "Devices"),
        el("th", { class: "num" }, "Rows"), el("th", { class: "num" }, "Faults"),
        el("th", { class: "num" }, "Open"), el("th", { class: "num" }, "Exited"),
        el("th", { class: "num" }, "Median"), el("th", { class: "num" }, "Restricted mean"))),
      el("tbody", {}, ...Object.entries(r.cohorts).map(([k, c]) => el("tr", {},
        el("td", {}, c.label),
        el("td", { class: "num" }, fmtInt(c.n_devices)),
        el("td", { class: "num" }, fmtInt(c.n_intervals)),
        el("td", { class: "num" }, fmtInt(c.n_events)),
        el("td", { class: "num" }, fmtInt(c.n_censored_open)),
        el("td", { class: "num" }, fmtInt(c.n_censored_exited)),
        el("td", { class: "num" }, c.median_age === null ? "not reached" : fmtDays(c.median_age)),
        el("td", { class: "num" }, fmtDays(c.restricted_mean_age))))))),
    el("div", { class: "sectiontitle" }, "Lineage"),
    el("div", { class: "basis" },
      el("dl", {},
        el("dt", {}, "build"),
        el("dd", {}, `#${m.build_id}`),
        el("dt", {}, "kind"),
        el("dd", {}, r.lineage?.kind || "full"),
        el("dt", {}, "parent build"),
        el("dd", {}, r.lineage?.parent_build ? `#${r.lineage.parent_build.id} (rev ${r.lineage.parent_build.data_revision})` : "—"),
        el("dt", {}, "changed devices"),
        el("dd", {}, r.lineage?.kind === "incremental"
          ? `${r.lineage.changed_device_count} rebuilt, rest copied from parent`
          : "—"),
        el("dt", {}, "bands"),
        el("dd", {}, m.bands_key),
        el("dt", {}, "bootstrap"),
        el("dd", {}, `${r.cohorts.overall.bootstrap.reps} reps, seed ${r.cohorts.overall.bootstrap.seed}`),
      )),
    el("div", { class: "sectiontitle" }, "Caveats"),
    el("ul", { class: "caveats" }, ...(r.caveats || []).map((c) => el("li", {}, c))),
  );
}

function startBuildFrom(r) {
  activateTab("builds");
  $("#build-asof").value = r._report.as_of;
  feedback("#build-feedback", `Cutoff pre-filled from report #${r._report.id}.`, "");
}

/* ---------------- ingest ---------------- */
async function loadBatches() {
  const batches = await api("/api/batches");
  const host = $("#batch-list");
  if (!batches.length) {
    host.replaceChildren(el("p", { class: "empty" }, "No batches yet."));
    return;
  }
  const head = el("tr", {},
    el("th", { class: "num" }, "Batch"), el("th", {}, "Received"),
    el("th", {}, "Source"), el("th", { class: "num" }, "Rev"),
    el("th", { class: "num" }, "Accepted"), el("th", { class: "num" }, "Skipped"),
    el("th", { class: "num" }, "Rejected"), el("th", { class: "num" }, "Devices"));
  const rows = batches.map((b) => el("tr", {},
    el("td", { class: "num" }, String(b.id)),
    el("td", {}, b.received_at), el("td", {}, b.source),
    el("td", { class: "num" }, String(b.data_revision)),
    el("td", { class: "num" }, fmtInt(b.accepted)),
    el("td", { class: "num" }, fmtInt(b.skipped)),
    el("td", { class: "num" }, fmtInt(b.rejected)),
    el("td", { class: "num" }, fmtInt(b.changed_devices.length))));
  host.replaceChildren(el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows)));
}

const EXAMPLE_BATCH = {
  batch_id: "ui-example-001",
  source: "web",
  faults: [
    {
      device_id: "DEV-0001",
      event_key: "DEV-0001-F1",
      fault_date: "2026-08-01",
      component: "drive-motor",
      severity: "major",
      reported_date: "2026-09-05",
      note: "late report from site log",
    },
  ],
};

$("#ingest-json").value = JSON.stringify(EXAMPLE_BATCH, null, 2);
$("#ingest-send").addEventListener("click", () => sendIngest(false));
$("#ingest-validate").addEventListener("click", () => sendIngest(true));

async function sendIngest(dry) {
  let payload;
  try {
    payload = JSON.parse($("#ingest-json").value);
  } catch (e) {
    feedback("#ingest-feedback", `JSON error: ${e.message}`, "err");
    return;
  }
  try {
    const res = await api(`/api/ingest${dry ? "?dry_run=1" : ""}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const rejected = res.rows.filter((r) => r.status === "rejected");
    feedback("#ingest-feedback",
      (dry ? "Dry run: " : "Committed: ") +
      `${res.accepted} accepted, ${res.skipped} skipped, ${res.rejected} rejected. ` +
      `Revision ${res.data_revision}. Changed devices: ${res.changed_devices.length}.` +
      (rejected.length ? ` First rejection: ${rejected[0].key} — ${rejected[0].reason}` : ""),
      res.rejected ? "err" : "ok");
    await loadStatus();
    await loadBatches();
    await loadReportIndex();
  } catch (e) {
    feedback("#ingest-feedback", e.message, "err");
  }
}

/* ---------------- boot ---------------- */
function activateTab(name) {
  const tab = $$(".tab").find((t) => t.dataset.view === name);
  if (tab) tab.click();
}

async function boot() {
  const today = new Date().toISOString().slice(0, 10);
  $("#build-asof").value = today;
  $("#compare-asof").value = today;
  await loadStatus();
  await loadReportIndex();
  await loadBuilds();
  window.addEventListener("resize", debounce(() => {
    if (state.report) renderCurves();
  }, 250));
}
boot().catch((e) => {
  document.body.prepend(el("div", { class: "banner" },
    `Could not load the API: ${e.message}`));
});
