"use strict";

import {
  api,
  jsonPost,
  loadSession,
  logout,
} from "/static/js/auth.js";
import {
  normalizePosture,
} from "/static/js/posture.js";

const SIMULATED_LABEL = (
  "Simulated — past performance does not predict future results."
);
const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_CHART_POINTS = 5000;
const byId = (id) => document.getElementById(id);

let runsRequestSequence = 0;
let runsAbortController = null;
let postureRequestSequence = 0;
let postureAbortController = null;
let reportSelection = null;
let reportRequestSequence = 0;
let reportAbortController = null;
let simulationPolicy = null;
let runBusy = false;
let runRequestSequence = 0;

function node(tag, value, className) {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (value !== undefined && value !== null) {
    element.textContent = String(value);
  }
  return element;
}

function clear(element) {
  while (element && element.firstChild) {
    element.removeChild(element.firstChild);
  }
}

function readable(value, fallback = "Unavailable") {
  return value === undefined || value === null || value === ""
    ? fallback
    : String(value);
}

function exactNonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function exactPositiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function rateMetadata(error) {
  const metadata = [];
  if (exactNonnegativeInteger(error && error.retryAfter)) {
    metadata.push(`Retry after ${error.retryAfter} seconds`);
  }
  if (exactNonnegativeInteger(error && error.rateLimitReset)) {
    const reset = new Date(error.rateLimitReset * 1000);
    metadata.push(
      Number.isFinite(reset.getTime())
        ? `Rate limit resets ${reset.toISOString()}`
        : `Rate limit reset ${error.rateLimitReset}`,
    );
  }
  return metadata.join(" · ");
}

function errorText(error) {
  const request = error && error.requestId ? ` Request ${error.requestId}.` : "";
  const rate = rateMetadata(error);
  return (
    `${readable(error && error.message, "Request failed")}.${request}`
    + (rate ? ` ${rate}.` : "")
  );
}

function backtestErrorText(error) {
  const request = error && error.requestId ? ` Request ${error.requestId}.` : "";
  const rate = rateMetadata(error);
  const suffix = `${request}${rate ? ` ${rate}.` : ""}`;
  const messages = {
    backtest_busy: (
      "Backtest busy: another bounded simulation owns the single-run lease."
    ),
    backtest_timed_out: (
      "Backtest timed out at the configured runtime ceiling; no result is claimed."
    ),
    backtest_bounds_exceeded: (
      "Backtest refused: requested symbols or dates exceed configured ceilings."
    ),
    provider_budget_exhausted: (
      "Provider budget denied the run before model network I/O."
    ),
    provider_reconciliation_required: (
      "Provider budget state requires reconciliation before any model call."
    ),
    holdout_access_forbidden: (
      "Holdout access refused: parameter tuning cannot use the final holdout."
    ),
    backtest_canceled: (
      "Backtest canceled; partial output is not presented as a completed report."
    ),
  };
  if (error && error.status === 429) {
    return `Backtest rate limit reached.${suffix}`;
  }
  const message = messages[error && error.code];
  return message ? `${message}${suffix}` : errorText(error);
}

function notify(message, kind = "") {
  const notice = node("div", message, `notice ${kind}`.trim());
  const region = byId("status-region");
  if (!region) {
    return;
  }
  region.appendChild(notice);
  window.setTimeout(
    () => notice.remove(),
    kind === "notice-error" ? 9000 : 5000,
  );
}

function setState(id, value, className) {
  const element = byId(id);
  if (!element) {
    return;
  }
  element.textContent = value;
  element.className = className;
}

function clearSimulationPolicy() {
  simulationPolicy = null;
  [
    "backtest-runtime-limit",
    "backtest-symbol-limit",
    "backtest-range-limit",
    "backtest-provider-mode",
  ].forEach((id) => setState(id, "Unknown", "is-unknown"));
}

function normalizeSimulationPolicy(value) {
  if (
    !value
    || typeof value !== "object"
    || Array.isArray(value)
    || !exactPositiveInteger(value.max_runtime_seconds)
    || !exactPositiveInteger(value.max_symbols)
    || !exactPositiveInteger(value.max_calendar_days)
    || !exactPositiveInteger(value.window_requests)
    || !exactPositiveInteger(value.global_window_requests)
    || !exactPositiveInteger(value.window_seconds)
    || !exactPositiveInteger(value.daily_requests)
    || !exactPositiveInteger(value.global_daily_requests)
    || !exactPositiveInteger(value.concurrency)
    || typeof value.llm_enabled !== "boolean"
  ) {
    return null;
  }
  return Object.freeze({...value});
}

function renderSimulationPolicy(value) {
  const normalized = normalizeSimulationPolicy(value);
  if (!normalized) {
    clearSimulationPolicy();
    return false;
  }
  simulationPolicy = normalized;
  setState(
    "backtest-runtime-limit",
    `${normalized.max_runtime_seconds} seconds`,
    "is-verified",
  );
  setState(
    "backtest-symbol-limit",
    `${normalized.max_symbols} symbols`,
    "is-verified",
  );
  setState(
    "backtest-range-limit",
    `${normalized.max_calendar_days} calendar days`,
    "is-verified",
  );
  setState(
    "backtest-provider-mode",
    normalized.llm_enabled
      ? "Configured; unavailable in this deterministic runner"
      : "Disabled · 0 model calls",
    normalized.llm_enabled ? "is-caution" : "is-verified",
  );
  return true;
}

function renderRunBudget(normalized) {
  const checks = normalized
    && normalized.valid === true
    && Array.isArray(normalized.checks)
    ? normalized.checks
    : [];
  const backtest = checks.find((check) => (
    check
    && check.name === "request_budget"
    && check.scope === "backtest"
  ));
  if (
    !backtest
    || !exactNonnegativeInteger(backtest.budget_remaining)
    || !exactPositiveInteger(backtest.budget_limit)
    || typeof backtest.reset_at !== "string"
    || !Number.isFinite(Date.parse(backtest.reset_at))
  ) {
    setState("backtest-run-budget", "Unknown", "is-unknown");
    return false;
  }
  setState(
    "backtest-run-budget",
    `${backtest.budget_remaining} / ${backtest.budget_limit} until `
      + backtest.reset_at,
    backtest.status === "pass" && backtest.budget_remaining > 0
      ? "is-verified"
      : "is-blocked",
  );
  return backtest.status === "pass" && backtest.budget_remaining > 0;
}

async function refreshBacktestPosture() {
  if (postureAbortController) {
    postureAbortController.abort();
  }
  const controller = new AbortController();
  postureAbortController = controller;
  const requestToken = ++postureRequestSequence;
  setState("backtest-run-budget", "Checking…", "is-loading");
  try {
    const payload = await api("/security/posture", {
      signal: controller.signal,
    });
    if (
      requestToken !== postureRequestSequence
      || postureAbortController !== controller
    ) {
      return false;
    }
    postureAbortController = null;
    return renderRunBudget(normalizePosture(payload));
  } catch (_error) {
    if (
      requestToken !== postureRequestSequence
      || postureAbortController !== controller
    ) {
      return false;
    }
    postureAbortController = null;
    setState("backtest-run-budget", "Unknown", "is-unknown");
    return false;
  }
}

function setRunBusy(busy, message = "") {
  runBusy = busy === true;
  const submit = byId("backtest-submit");
  if (submit) {
    submit.disabled = runBusy;
  }
  setState(
    "backtest-active-state",
    runBusy ? (message || "In progress") : "No local run in progress",
    runBusy ? "is-caution" : "is-verified",
  );
}

function invalidateSelectedReport(title, message) {
  beginReportTransition(title, message);
}

async function refreshRuns() {
  const target = byId("backtest-runs");
  if (runsAbortController) {
    runsAbortController.abort();
  }
  const controller = new AbortController();
  runsAbortController = controller;
  const requestToken = ++runsRequestSequence;
  clear(target);
  target.appendChild(node(
    "p",
    "Refreshing saved runs…",
    "empty-state",
  ));
  let payload;
  try {
    payload = await api("/backtests", {
      signal: controller.signal,
    });
  } catch (error) {
    if (
      requestToken !== runsRequestSequence
      || runsAbortController !== controller
    ) {
      return false;
    }
    runsAbortController = null;
    clearSimulationPolicy();
    clear(target);
    target.appendChild(node(
      "p",
      "Saved run truth is unavailable.",
      "empty-state",
    ));
    invalidateSelectedReport(
      "Backtest report unavailable",
      `${SIMULATED_LABEL} Saved-run refresh failed; prior report cleared.`,
    );
    throw error;
  }
  if (
    requestToken !== runsRequestSequence
    || runsAbortController !== controller
  ) {
    return false;
  }
  runsAbortController = null;
  renderSimulationPolicy(payload && payload.simulation_policy);
  clear(target);
  const runs = Array.isArray(payload && payload.backtests)
    ? payload.backtests
    : [];
  if (!runs.length) {
    target.appendChild(node("p", "No saved runs.", "empty-state"));
    return true;
  }
  runs.forEach((run) => {
    const button = node("button", null, "run-item");
    button.type = "button";
    button.appendChild(node(
      "strong",
      `#${readable(run.run_id)} · ${readable(run.label)}`,
    ));
    const created = typeof run.created_at === "string"
      ? run.created_at.replace("T", " ").slice(0, 16)
      : "time unavailable";
    button.appendChild(node(
      "div",
      `${readable(run.status, "status unavailable")} · ${created}`,
      "item-meta",
    ));
    button.addEventListener("click", () => showReport(run.run_id));
    target.appendChild(button);
  });
  return true;
}

function formatMetric(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "Unavailable";
}

function canonicalRunId(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function reportSelectionIsCurrent(requestToken, targetRunId, controller) {
  return Boolean(
    reportSelection
    && reportSelection.requestToken === requestToken
    && reportSelection.targetRunId === targetRunId
    && reportSelection.controller === controller
    && requestToken === reportRequestSequence
    && reportAbortController === controller
  );
}

function beginReportTransition(title, message) {
  if (reportAbortController) {
    reportAbortController.abort();
    reportAbortController = null;
  }
  reportSelection = null;
  const requestToken = ++reportRequestSequence;
  const titleElement = byId("report-title");
  if (titleElement) {
    titleElement.textContent = title;
  }
  const target = byId("backtest-report");
  clear(target);
  if (target) {
    target.appendChild(node("p", message, "empty-state"));
  }
  return requestToken;
}

function reportTransitionIsCurrent(requestToken) {
  return Boolean(
    requestToken === reportRequestSequence
    && reportSelection === null
  );
}

function definitionList(entries, className = "evidence-grid") {
  const list = node("dl", null, className);
  entries.forEach(([label, value, state = ""]) => {
    const item = node("div");
    item.appendChild(node("dt", label));
    item.appendChild(node("dd", readable(value), state));
    list.appendChild(item);
  });
  return list;
}

function renderMetrics(target, report) {
  const rows = Array.isArray(report.rows) ? report.rows : [];
  const heading = node("div", null, "report-section-heading");
  heading.appendChild(node("p", "Strategy comparison", "section-label"));
  heading.appendChild(node("h3", "Metrics versus buy-and-hold"));
  target.appendChild(heading);
  if (!rows.length) {
    target.appendChild(node(
      "p",
      "No metric rows were persisted for this run.",
      "empty-state",
    ));
    return;
  }

  const wrapper = node("div", null, "table-wrap table-scroll-cue");
  const table = node("table", null, "data-table backtest-metrics-table");
  const head = node("thead");
  const headRow = node("tr");
  [
    "Symbol",
    "Strategy",
    "Window",
    "Return %",
    "B&H return %",
    "CAGR %",
    "Sharpe",
    "Sortino",
    "Max DD %",
    "Win %",
    "Profit factor",
    "Avg win",
    "Avg loss",
    "Exposure %",
    "Turnover",
    "Trades",
    "Beat B&H",
  ].forEach((header) => headRow.appendChild(node("th", header)));
  head.appendChild(headRow);
  table.appendChild(head);
  const body = node("tbody");
  rows.forEach((rowData) => {
    const metrics = rowData && typeof rowData.metrics === "object"
      ? rowData.metrics
      : {};
    const benchmark = (
      rowData
      && typeof rowData.benchmark_buy_and_hold === "object"
    ) ? rowData.benchmark_buy_and_hold : {};
    const row = node("tr");
    [
      readable(rowData && rowData.symbol),
      readable(rowData && rowData.strategy),
      readable(rowData && rowData.window),
      formatMetric(metrics.total_return_pct),
      formatMetric(benchmark.total_return_pct),
      formatMetric(metrics.cagr_pct),
      formatMetric(metrics.sharpe),
      formatMetric(metrics.sortino),
      formatMetric(metrics.max_drawdown_pct),
      formatMetric(metrics.win_rate_pct),
      formatMetric(metrics.profit_factor),
      formatMetric(metrics.avg_win),
      formatMetric(metrics.avg_loss),
      formatMetric(metrics.exposure_pct),
      formatMetric(metrics.turnover),
      readable(metrics.num_trades),
    ].forEach((value) => row.appendChild(node("td", value)));
    row.appendChild(node(
      "td",
      rowData && rowData.beat_buy_and_hold === true ? "Yes" : "No",
      rowData && rowData.beat_buy_and_hold === true
        ? "is-caution"
        : "is-unknown",
    ));
    body.appendChild(row);
  });
  table.appendChild(body);
  wrapper.appendChild(table);
  target.appendChild(wrapper);
}

function renderRegimes(target, report) {
  const rows = Array.isArray(report.rows) ? report.rows : [];
  const section = node("section", null, "report-subsection");
  section.appendChild(node("h3", "P&L attribution by regime"));
  let rendered = 0;
  rows.forEach((rowData) => {
    const regimes = (
      rowData
      && rowData.metrics
      && rowData.metrics.pnl_by_regime
      && typeof rowData.metrics.pnl_by_regime === "object"
      && !Array.isArray(rowData.metrics.pnl_by_regime)
    ) ? rowData.metrics.pnl_by_regime : {};
    const entries = Object.entries(regimes).filter(
      ([name, value]) => (
        typeof name === "string"
        && name
        && Number.isFinite(Number(value))
      ),
    );
    if (!entries.length) {
      return;
    }
    rendered += 1;
    const card = node("article", null, "regime-card");
    card.appendChild(node(
      "h4",
      `${readable(rowData.symbol)} · ${readable(rowData.strategy)} · `
        + readable(rowData.window),
    ));
    card.appendChild(definitionList(entries.map(([name, value]) => [
      name,
      formatMetric(value),
    ])));
    section.appendChild(card);
  });
  if (!rendered) {
    section.appendChild(node(
      "p",
      "Regime attribution unavailable for this run.",
      "empty-state",
    ));
  }
  target.appendChild(section);
}

function chartPoints(points, valueKey) {
  if (
    !Array.isArray(points)
    || points.length < 2
    || points.length > MAX_CHART_POINTS
  ) {
    return null;
  }
  const normalized = [];
  for (const point of points) {
    if (
      !point
      || typeof point !== "object"
      || typeof point.at !== "string"
      || !Number.isFinite(Date.parse(point.at))
      || !Number.isFinite(Number(point[valueKey]))
    ) {
      return null;
    }
    normalized.push({
      at: point.at,
      value: Number(point[valueKey]),
    });
  }
  return normalized;
}

function setSvgAttribute(element, name, value) {
  element.setAttribute(name, String(value));
}

function svgNode(tag) {
  return document.createElementNS(SVG_NS, tag);
}

function renderSvgChart({
  title,
  description,
  primary,
  benchmark,
  primaryLabel,
  benchmarkLabel,
}) {
  const primaryPoints = chartPoints(primary.points, primary.valueKey);
  const benchmarkPoints = chartPoints(
    benchmark.points,
    benchmark.valueKey,
  );
  if (
    !primaryPoints
    || !benchmarkPoints
    || primaryPoints.length !== benchmarkPoints.length
  ) {
    return null;
  }
  const values = [
    ...primaryPoints.map((point) => point.value),
    ...benchmarkPoints.map((point) => point.value),
  ];
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    return null;
  }
  const width = 720;
  const height = 260;
  const padding = 24;
  const span = max === min ? 1 : max - min;
  const plot = (points) => points.map((point, index) => {
    const x = padding + (
      index / Math.max(1, points.length - 1)
    ) * (width - padding * 2);
    const y = height - padding - (
      (point.value - min) / span
    ) * (height - padding * 2);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");

  const figure = node("figure", null, "evidence-chart");
  figure.appendChild(node("p", SIMULATED_LABEL, "chart-warning"));
  const svg = svgNode("svg");
  setSvgAttribute(svg, "viewBox", `0 0 ${width} ${height}`);
  setSvgAttribute(svg, "role", "img");
  setSvgAttribute(svg, "tabindex", "0");
  setSvgAttribute(
    svg,
    "aria-label",
    `${title}. ${primaryLabel} compared with ${benchmarkLabel}.`,
  );
  const titleNode = svgNode("title");
  titleNode.textContent = title;
  svg.appendChild(titleNode);
  const descriptionNode = svgNode("desc");
  descriptionNode.textContent = description;
  svg.appendChild(descriptionNode);
  [
    [primaryPoints, "chart-line-primary"],
    [benchmarkPoints, "chart-line-benchmark"],
  ].forEach(([points, className]) => {
    const line = svgNode("polyline");
    setSvgAttribute(line, "points", plot(points));
    setSvgAttribute(line, "class", className);
    setSvgAttribute(line, "fill", "none");
    svg.appendChild(line);
  });
  figure.appendChild(svg);
  const legend = node("div", null, "chart-legend");
  legend.appendChild(node("span", primaryLabel, "legend-primary"));
  legend.appendChild(node("span", benchmarkLabel, "legend-benchmark"));
  figure.appendChild(legend);
  figure.appendChild(node(
    "p",
    `${primaryLabel}: ${formatMetric(primaryPoints[0].value)} to `
      + `${formatMetric(primaryPoints.at(-1).value)}. `
      + `${benchmarkLabel}: ${formatMetric(benchmarkPoints[0].value)} to `
      + `${formatMetric(benchmarkPoints.at(-1).value)}. `
      + `Range ${formatMetric(min)} to ${formatMetric(max)}.`,
    "chart-summary",
  ));
  return figure;
}

function renderSeriesCharts(target, series) {
  const section = node("section", null, "report-subsection chart-section");
  section.appendChild(node("h3", "Equity and drawdown curves"));
  let rendered = 0;
  series.forEach((row) => {
    if (!row || typeof row !== "object") {
      return;
    }
    const identity = `${readable(row.symbol)} · `
      + `${readable(row.strategy)} · ${readable(row.window)}`;
    const equity = renderSvgChart({
      title: `${identity} equity curve`,
      description: (
        "Strategy equity and buy-and-hold benchmark through simulated time."
      ),
      primary: {points: row.strategy_equity, valueKey: "equity"},
      benchmark: {points: row.benchmark_equity, valueKey: "equity"},
      primaryLabel: "Strategy",
      benchmarkLabel: "Buy-and-hold",
    });
    const drawdown = renderSvgChart({
      title: `${identity} drawdown curve`,
      description: (
        "Strategy and buy-and-hold percentage drawdowns through simulated time."
      ),
      primary: {
        points: row.strategy_drawdown,
        valueKey: "drawdown_pct",
      },
      benchmark: {
        points: row.benchmark_drawdown,
        valueKey: "drawdown_pct",
      },
      primaryLabel: "Strategy drawdown",
      benchmarkLabel: "Buy-and-hold drawdown",
    });
    if (!equity || !drawdown) {
      const unavailable = node("article", null, "chart-unavailable");
      unavailable.appendChild(node("h4", identity));
      unavailable.appendChild(node(
        "p",
        "Chart unavailable: curve data was missing, non-finite, oversized, or inconsistent.",
        "banner-caution",
      ));
      section.appendChild(unavailable);
      return;
    }
    rendered += 1;
    const group = node("article", null, "chart-pair");
    group.appendChild(node("h4", identity));
    group.appendChild(equity);
    group.appendChild(drawdown);
    section.appendChild(group);
  });
  if (!rendered && !series.length) {
    section.appendChild(node(
      "p",
      "No chart series were persisted.",
      "empty-state",
    ));
  }
  target.appendChild(section);
}

function renderArtifactEvidence(target, report) {
  const artifactStatus = report
    && report.artifact_status
    && typeof report.artifact_status === "object"
    ? report.artifact_status
    : {};
  if (artifactStatus.status !== "available") {
    const knownReasons = {
      not_persisted_for_legacy_run: (
        "Artifact evidence unavailable: this legacy run did not persist replay series."
      ),
      artifact_invalid: (
        "Artifact evidence unavailable: persisted artifacts failed validation."
      ),
    };
    target.appendChild(node(
      "p",
      knownReasons[artifactStatus.reason]
        || "Artifact evidence unavailable: no validated replay series.",
      "banner-caution",
    ));
    return;
  }
  const manifest = report.manifest;
  const series = report.series;
  if (
    !manifest
    || typeof manifest !== "object"
    || Array.isArray(manifest)
    || !Array.isArray(series)
  ) {
    target.appendChild(node(
      "p",
      "Artifact evidence unavailable: response structure was invalid.",
      "banner-caution",
    ));
    return;
  }
  const config = (
    manifest.backtest_config
    && typeof manifest.backtest_config === "object"
  ) ? manifest.backtest_config : {};
  const costs = (
    series[0]
    && series[0].cost_assumptions
    && typeof series[0].cost_assumptions === "object"
  ) ? series[0].cost_assumptions : {};
  const slippage = costs.slippage_bps || config.slippage_bps || {};
  const fees = costs.fees_bps || config.fees_bps || {};
  target.appendChild(node("h3", "Run metadata"));
  target.appendChild(definitionList([
    ["Status", readable(report.status)],
    ["Data source", readable(manifest.data_source)],
    [
      "Actual data range",
      `${readable(manifest.actual_range && manifest.actual_range.start)} → `
        + readable(manifest.actual_range && manifest.actual_range.end),
    ],
    ["Duration", `${formatMetric(manifest.duration_seconds)} seconds`],
    [
      "Symbols",
      Array.isArray(manifest.symbols)
        ? manifest.symbols.join(", ")
        : "Unavailable",
    ],
    [
      "Strategies",
      Array.isArray(manifest.strategies)
        ? manifest.strategies.join(", ")
        : "Unavailable",
    ],
    ["Final holdout starts", readable(manifest.holdout_start)],
    [
      "Slippage bps",
      `Equity ${formatMetric(slippage.equity)} · Crypto `
        + formatMetric(slippage.crypto),
    ],
    [
      "Fee bps",
      `Equity ${formatMetric(fees.equity)} · Crypto `
        + formatMetric(fees.crypto),
    ],
  ], "report-metadata-grid"));

  const accessLog = Array.isArray(manifest.holdout_access_log)
    ? manifest.holdout_access_log
    : [];
  const blocked = accessLog.filter(
    (entry) => entry && entry.blocked === true,
  ).length;
  const allowed = accessLog.filter(
    (entry) => entry && entry.blocked === false,
  ).length;
  const audit = node("section", null, "report-subsection");
  audit.appendChild(node("h3", "Holdout access"));
  audit.appendChild(node(
    "p",
    accessLog.length
      ? `${blocked} blocked access checks · ${allowed} permitted evaluation accesses.`
      : "Holdout access evidence unavailable.",
    accessLog.length ? "is-caution" : "is-unknown",
  ));
  audit.appendChild(node(
    "p",
    (
      manifest.validation
      && manifest.validation.status === "unavailable"
      && manifest.validation.reason === "not_run"
    ) ? "Validation not run." : "Validation status unavailable.",
    "is-unknown",
  ));
  audit.appendChild(node(
    "p",
    (
      manifest.episodes
      && manifest.episodes.status === "not_run"
    ) ? "Historical episodes not run." : "Historical episode status unavailable.",
    "is-unknown",
  ));
  target.appendChild(audit);

  const feeRows = series.map((row) => [
    `${readable(row.symbol)} · ${readable(row.strategy)} · `
      + readable(row.window),
    `Strategy ${formatMetric(row.actual_total_fees)} · Buy-and-hold `
      + formatMetric(row.benchmark_actual_total_fees),
  ]);
  const feeSection = node("section", null, "report-subsection");
  feeSection.appendChild(node("h3", "Actual simulated fees"));
  feeSection.appendChild(
    feeRows.length
      ? definitionList(feeRows)
      : node("p", "Fee evidence unavailable.", "empty-state"),
  );
  target.appendChild(feeSection);
  renderSeriesCharts(target, series);
}

function renderReport(target, report) {
  target.appendChild(node("p", SIMULATED_LABEL, "banner-caution"));
  if (report.disclaimer && report.disclaimer !== SIMULATED_LABEL) {
    target.appendChild(node(
      "p",
      `Persisted report label: ${readable(report.disclaimer)}`,
      "item-meta",
    ));
  }
  renderArtifactEvidence(target, report);
  renderMetrics(target, report);
  renderRegimes(target, report);
}

async function showReport(runId) {
  const targetRunId = canonicalRunId(runId);
  const target = byId("backtest-report");
  if (reportAbortController) {
    reportAbortController.abort();
  }
  const controller = new AbortController();
  reportAbortController = controller;
  const requestToken = ++reportRequestSequence;
  reportSelection = Object.freeze({
    targetRunId,
    requestToken,
    controller,
    report: null,
  });
  clear(target);
  if (targetRunId === null) {
    reportAbortController = null;
    byId("report-title").textContent = "Choose a run";
    target.appendChild(node(
      "p",
      `${SIMULATED_LABEL} Report identity mismatch. Choose a current saved run.`,
      "banner-caution",
    ));
    return false;
  }
  byId("report-title").textContent = (
    `Loading report #${targetRunId}`
  );
  target.appendChild(node(
    "p",
    `${SIMULATED_LABEL} Loading report…`,
    "empty-state",
  ));
  try {
    const report = await api(
      `/backtests/${targetRunId}/report`,
      {signal: controller.signal},
    );
    if (!reportSelectionIsCurrent(
      requestToken,
      targetRunId,
      controller,
    )) {
      return false;
    }
    if (canonicalRunId(report && report.run_id) !== targetRunId) {
      reportAbortController = null;
      reportSelection = Object.freeze({
        targetRunId,
        requestToken,
        controller,
        report: null,
      });
      byId("report-title").textContent = (
        `Report #${targetRunId} unavailable`
      );
      clear(target);
      target.appendChild(node(
        "p",
        `${SIMULATED_LABEL} Report identity mismatch. Refresh and choose the run again.`,
        "banner-caution",
      ));
      return false;
    }
    reportAbortController = null;
    reportSelection = Object.freeze({
      targetRunId,
      requestToken,
      controller,
      report: Object.freeze(report),
    });
    byId("report-title").textContent = (
      `Report #${targetRunId} · ${readable(report.label)}`
    );
    clear(target);
    renderReport(target, report);
    return true;
  } catch (error) {
    if (!reportSelectionIsCurrent(
      requestToken,
      targetRunId,
      controller,
    )) {
      return false;
    }
    reportAbortController = null;
    reportSelection = Object.freeze({
      targetRunId,
      requestToken,
      controller,
      report: null,
    });
    byId("report-title").textContent = (
      `Report #${targetRunId} unavailable`
    );
    clear(target);
    target.appendChild(node(
      "p",
      `${SIMULATED_LABEL} ${errorText(error)}`,
      "banner-caution",
    ));
    return false;
  }
}

function parseSymbols() {
  const input = byId("backtest-symbols");
  if (!input || !input.value.trim()) {
    return [];
  }
  const symbols = [...new Set(
    input.value
      .split(",")
      .map((value) => value.trim().toUpperCase())
      .filter(Boolean),
  )];
  if (
    symbols.some((symbol) => !/^[A-Z0-9./-]{1,20}$/.test(symbol))
  ) {
    throw new Error("Symbols must be comma-separated market symbols.");
  }
  if (
    simulationPolicy
    && symbols.length > simulationPolicy.max_symbols
  ) {
    throw new Error(
      `Symbol count exceeds the ${simulationPolicy.max_symbols} symbol ceiling.`,
    );
  }
  return symbols;
}

function optionalDateRange() {
  const start = byId("backtest-start-date");
  const end = byId("backtest-end-date");
  const startValue = start ? start.value.trim() : "";
  const endValue = end ? end.value.trim() : "";
  if (Boolean(startValue) !== Boolean(endValue)) {
    throw new Error("Start date and end date must be provided together.");
  }
  if (!startValue) {
    return {};
  }
  const startDate = new Date(`${startValue}T00:00:00Z`);
  const endDate = new Date(`${endValue}T00:00:00Z`);
  if (
    !Number.isFinite(startDate.getTime())
    || !Number.isFinite(endDate.getTime())
    || endDate < startDate
  ) {
    throw new Error("Backtest dates must form a valid ordered range.");
  }
  const inclusiveDays = Math.floor(
    (endDate.getTime() - startDate.getTime()) / 86400000,
  ) + 1;
  if (
    simulationPolicy
    && inclusiveDays > simulationPolicy.max_calendar_days
  ) {
    throw new Error(
      `Date range exceeds the ${simulationPolicy.max_calendar_days} day ceiling.`,
    );
  }
  return {start_date: startValue, end_date: endValue};
}

function clearRunInputs() {
  [
    "backtest-reason",
    "backtest-symbols",
    "backtest-start-date",
    "backtest-end-date",
  ].forEach((id) => {
    const element = byId(id);
    if (element) {
      element.value = "";
    }
  });
}

async function submitBacktest(event) {
  event.preventDefault();
  if (runBusy) {
    notify(
      "A local backtest request is already in progress.",
      "notice-error",
    );
    return;
  }
  const reasonElement = byId("backtest-reason");
  const reason = reasonElement ? reasonElement.value.trim() : "";
  if (!reason) {
    notify("A non-empty backtest reason is required.", "notice-error");
    return;
  }
  let body;
  try {
    body = {
      reason,
      symbols: parseSymbols(),
      ...optionalDateRange(),
    };
  } catch (error) {
    notify(readable(error && error.message), "notice-error");
    return;
  }
  const requestToken = ++runRequestSequence;
  setRunBusy(true, "In progress · single-run lease requested");
  const reportTransitionToken = beginReportTransition(
    "Running backtest",
    `${SIMULATED_LABEL} Running bounded walk-forward simulation…`,
  );
  try {
    const result = await api("/backtests/run", jsonPost(body));
    if (requestToken !== runRequestSequence) {
      return;
    }
    clearRunInputs();
    await refreshRuns();
    await refreshBacktestPosture();
    if (reportTransitionIsCurrent(reportTransitionToken)) {
      await showReport(result.run_id);
    }
    notify(
      `Backtest ${readable(result.run_id)} completed as simulated evidence.`,
      "notice-success",
    );
  } catch (error) {
    if (requestToken !== runRequestSequence) {
      return;
    }
    if (error && error.code === "backtest_busy") {
      setRunBusy(
        true,
        "Busy · another server-side run owns the lease",
      );
    }
    if (reportTransitionIsCurrent(reportTransitionToken)) {
      byId("report-title").textContent = "Backtest run unavailable";
      const target = byId("backtest-report");
      clear(target);
      target.appendChild(node(
        "p",
        `${SIMULATED_LABEL} ${backtestErrorText(error)}`,
        "banner-caution",
      ));
    }
    await refreshBacktestPosture();
  } finally {
    if (requestToken === runRequestSequence) {
      setRunBusy(false);
    }
  }
}

async function initialize() {
  try {
    const session = await loadSession();
    byId("session-actor").textContent = readable(session.actor);
  } catch (_error) {
    return;
  }
  byId("sign-out").addEventListener("click", async () => {
    try {
      await logout();
    } catch (error) {
      notify(errorText(error), "notice-error");
    }
  });
  byId("backtest-form").addEventListener("submit", submitBacktest);
  byId("refresh-runs").addEventListener("click", async () => {
    try {
      await Promise.all([
        refreshRuns(),
        refreshBacktestPosture(),
      ]);
    } catch (error) {
      notify(errorText(error), "notice-error");
    }
  });
  setRunBusy(false);
  clearSimulationPolicy();
  await Promise.allSettled([
    refreshRuns(),
    refreshBacktestPosture(),
  ]);
}

initialize();
