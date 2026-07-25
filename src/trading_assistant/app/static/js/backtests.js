"use strict";

import {
  api,
  jsonPost,
  loadSession,
  logout,
} from "/static/js/auth.js";

const byId = (id) => document.getElementById(id);

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
  while (element.firstChild) {
    element.removeChild(element.firstChild);
  }
}

function readable(value, fallback = "Unavailable") {
  return value === undefined || value === null || value === ""
    ? fallback
    : String(value);
}

function errorText(error) {
  const request = error && error.requestId ? ` Request ${error.requestId}.` : "";
  return `${readable(error && error.message, "Request failed")}.${request}`;
}

function notify(message, kind = "") {
  const notice = node("div", message, `notice ${kind}`.trim());
  byId("status-region").appendChild(notice);
  window.setTimeout(() => notice.remove(), kind === "notice-error" ? 9000 : 5000);
}

async function refreshRuns() {
  const payload = await api("/backtests");
  const target = byId("backtest-runs");
  clear(target);
  const runs = Array.isArray(payload.backtests) ? payload.backtests : [];
  if (!runs.length) {
    target.appendChild(node("p", "No saved runs.", "empty-state"));
    return;
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
    button.appendChild(node("div", created, "item-meta"));
    button.addEventListener("click", () => showReport(run.run_id));
    target.appendChild(button);
  });
}

function formatMetric(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "Unavailable";
}

async function showReport(runId) {
  const target = byId("backtest-report");
  clear(target);
  target.appendChild(node("p", "Loading report…", "empty-state"));
  try {
    const report = await api(`/backtests/${runId}/report`);
    byId("report-title").textContent = (
      `Report #${readable(report.run_id)} · ${readable(report.label)}`
    );
    clear(target);
    target.appendChild(node("p", readable(report.disclaimer), "banner-caution"));
    const wrapper = node("div", null, "table-wrap");
    const table = node("table");
    const head = node("thead");
    const headRow = node("tr");
    [
      "Symbol",
      "Strategy",
      "Window",
      "Return %",
      "Buy & hold %",
      "Sharpe",
      "Max drawdown %",
      "Trades",
      "Exposure %",
      "Beat benchmark",
    ].forEach((header) => headRow.appendChild(node("th", header)));
    head.appendChild(headRow);
    table.appendChild(head);
    const body = node("tbody");
    (Array.isArray(report.rows) ? report.rows : []).forEach((rowData) => {
      const metrics = rowData.metrics || {};
      const benchmark = rowData.benchmark_buy_and_hold || {};
      const row = node("tr");
      [
        rowData.symbol,
        rowData.strategy,
        rowData.window,
        formatMetric(metrics.total_return_pct),
        formatMetric(benchmark.total_return_pct),
        formatMetric(metrics.sharpe),
        formatMetric(metrics.max_drawdown_pct),
        readable(metrics.num_trades),
        formatMetric(metrics.exposure_pct, 0),
      ].forEach((value) => row.appendChild(node("td", readable(value))));
      row.appendChild(node(
        "td",
        rowData.beat_buy_and_hold === true ? "Yes" : "No",
        rowData.beat_buy_and_hold === true ? "positive" : "negative",
      ));
      body.appendChild(row);

      const regimes = metrics.pnl_by_regime || {};
      const regimeEntries = Object.entries(regimes);
      if (regimeEntries.length) {
        const regimeRow = node("tr");
        const cell = node(
          "td",
          `P&L by regime · ${regimeEntries.map(
            ([name, value]) => `${name}: ${formatMetric(value, 0)}`,
          ).join(" · ")}`,
          "muted",
        );
        cell.colSpan = 10;
        regimeRow.appendChild(cell);
        body.appendChild(regimeRow);
      }
    });
    table.appendChild(body);
    wrapper.appendChild(table);
    target.appendChild(wrapper);
  } catch (error) {
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
  }
}

async function submitBacktest(event) {
  event.preventDefault();
  const reason = byId("backtest-reason").value.trim();
  if (!reason) {
    notify("A non-empty backtest reason is required.", "notice-error");
    return;
  }
  const target = byId("backtest-report");
  clear(target);
  target.appendChild(node("p", "Running walk-forward simulation…", "empty-state"));
  try {
    const result = await api("/backtests/run", jsonPost({reason}));
    byId("backtest-reason").value = "";
    await refreshRuns();
    await showReport(result.run_id);
    notify(`Backtest ${readable(result.run_id)} completed.`, "notice-success");
  } catch (error) {
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
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
  byId("refresh-runs").addEventListener("click", refreshRuns);
  await refreshRuns();
}

initialize();
