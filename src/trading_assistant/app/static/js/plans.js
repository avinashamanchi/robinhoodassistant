"use strict";

import {
  api,
  jsonPost,
  loadSession,
  logout,
} from "/static/js/auth.js";

const byId = (id) => document.getElementById(id);
const dialogReturnFocus = new Map();
let activePlanId = null;

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

function showDialog(dialog, trigger) {
  dialogReturnFocus.set(dialog, trigger || document.activeElement);
  dialog.showModal();
}

function closeDialog(dialog) {
  const target = dialogReturnFocus.get(dialog);
  dialogReturnFocus.delete(dialog);
  if (dialog.open) {
    dialog.close();
  }
  if (target && typeof target.focus === "function") {
    target.focus();
  }
}

function bindDialogReturnFocus(dialog) {
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDialog(dialog);
    }
  });
}

function metricTable(headers, rows) {
  const wrapper = node("div", null, "table-wrap");
  const table = node("table");
  const head = node("thead");
  const headRow = node("tr");
  headers.forEach((header) => headRow.appendChild(node("th", header)));
  head.appendChild(headRow);
  table.appendChild(head);
  const body = node("tbody");
  rows.forEach((values) => {
    const row = node("tr");
    values.forEach((value) => row.appendChild(node("td", readable(value))));
    body.appendChild(row);
  });
  table.appendChild(body);
  wrapper.appendChild(table);
  return wrapper;
}

async function refreshPlans() {
  const payload = await api("/plans");
  const target = byId("plans-list");
  clear(target);
  const plans = Array.isArray(payload.plans) ? payload.plans : [];
  if (!plans.length) {
    target.appendChild(node("p", "No saved plans.", "empty-state"));
    return;
  }
  plans.forEach((plan) => {
    const button = node("button", null, "plan-item");
    button.type = "button";
    button.appendChild(node(
      "strong",
      `#${readable(plan.plan_id)} · ${readable(plan.symbol)}`,
    ));
    button.appendChild(node(
      "div",
      `${readable(plan.action)} · ${readable(plan.status)}`,
      "item-meta",
    ));
    button.addEventListener("click", () => showPlan(plan.plan_id));
    target.appendChild(button);
  });
}

async function submitAnalysis(event) {
  event.preventDefault();
  const symbol = byId("analysis-symbol").value.trim().toUpperCase();
  const reason = byId("analysis-reason").value.trim();
  if (!symbol || !reason) {
    notify("A symbol and non-empty analysis reason are required.", "notice-error");
    return;
  }
  const target = byId("plan-detail");
  clear(target);
  target.appendChild(node("p", `Analyzing ${symbol}…`, "empty-state"));
  try {
    const result = await api("/analyze", jsonPost({symbol, reason}));
    byId("analysis-reason").value = "";
    await refreshPlans();
    await showPlan(result.plan_id);
  } catch (error) {
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
  }
}

async function submitProposals(event) {
  event.preventDefault();
  const reason = byId("proposal-reason").value.trim();
  if (!reason) {
    notify("A non-empty proposal reason is required.", "notice-error");
    return;
  }
  const target = byId("plan-detail");
  clear(target);
  target.appendChild(node(
    "p",
    "Screening and analyzing candidates. Analyst output remains unproven.",
    "empty-state",
  ));
  try {
    const result = await api("/propose", jsonPost({n: 3, reason}));
    byId("proposal-reason").value = "";
    await refreshPlans();
    clear(target);
    target.appendChild(node("p", readable(result.note), "banner-caution"));
    (Array.isArray(result.proposed) ? result.proposed : []).forEach((plan) => {
      const label = plan.error
        ? `${readable(plan.symbol)} · skipped (${readable(plan.error)})`
        : (
          `#${readable(plan.plan_id)} · ${readable(plan.symbol)} · `
          + `${readable(plan.action)} · ${readable(plan.sized_shares)} shares`
        );
      target.appendChild(node("p", label, "muted"));
    });
  } catch (error) {
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
  }
}

async function runScreen() {
  const target = byId("screen-results");
  clear(target);
  target.appendChild(node("p", "Screening…", "empty-state"));
  try {
    const result = await api("/screen", {method: "POST"});
    clear(target);
    const candidates = Array.isArray(result.candidates)
      ? result.candidates
      : [];
    if (!candidates.length) {
      target.appendChild(node("p", "No candidates reported.", "empty-state"));
      return;
    }
    candidates.forEach((candidate) => {
      const button = node("button", null, "plan-item");
      button.type = "button";
      button.appendChild(node("strong", readable(candidate.symbol)));
      button.appendChild(node(
        "div",
        `${readable(candidate.score)} · ${readable(candidate.regime)}`,
        "item-meta",
      ));
      button.addEventListener("click", () => {
        byId("analysis-symbol").value = readable(candidate.symbol, "");
        byId("analysis-reason").focus();
      });
      target.appendChild(button);
    });
  } catch (error) {
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
  }
}

function appendPlanActions(target, plan) {
  if (plan.status !== "proposed") {
    return;
  }
  const sized = plan.sized || {};
  const actionable = Number(sized.total_shares) > 0;
  const actions = node("div", null, "action-row");
  if (actionable) {
    const approve = node("button", "Review plan approval", "button-primary");
    approve.type = "button";
    approve.addEventListener("click", () => {
      activePlanId = plan.plan_id;
      byId("plan-approval-reason").value = "";
      showDialog(byId("plan-approval-dialog"), approve);
      byId("plan-approval-reason").focus();
    });
    actions.appendChild(approve);
  } else {
    actions.appendChild(node(
      "p",
      `Advisory only. ${readable(sized.zero_reason, "No sized entry is available.")}`,
      "banner-caution",
    ));
  }
  const cancel = node("button", "Cancel plan", "button-danger");
  cancel.type = "button";
  cancel.addEventListener("click", () => {
    activePlanId = plan.plan_id;
    byId("plan-cancel-reason").value = "";
    showDialog(byId("plan-cancel-dialog"), cancel);
    byId("plan-cancel-reason").focus();
  });
  actions.appendChild(cancel);
  target.appendChild(actions);
}

async function showPlan(planId) {
  const target = byId("plan-detail");
  clear(target);
  target.appendChild(node("p", "Loading plan…", "empty-state"));
  try {
    const plan = await api(`/plans/${planId}`);
    activePlanId = plan.plan_id;
    byId("plan-detail-title").textContent = (
      `Plan #${readable(plan.plan_id)} · ${readable(plan.symbol)} · `
      + readable(plan.status)
    );
    clear(target);
    if (plan.paper_only === true) {
      target.appendChild(node(
        "p",
        "Unproven analyst gate — paper mode only.",
        "banner-caution",
      ));
    }
    const thesis = plan.plan || {};
    const sized = plan.sized || {};
    target.appendChild(node(
      "p",
      `${readable(thesis.action).toUpperCase()} · confidence `
      + `${readable(thesis.confidence)} · ${readable(thesis.regime_note)}`,
    ));
    target.appendChild(node("p", readable(thesis.thesis)));

    target.appendChild(node("h2", "Scenarios"));
    target.appendChild(metricTable(
      ["Case", "Target", "Days", "Probability"],
      (Array.isArray(thesis.scenarios) ? thesis.scenarios : []).map((scenario) => [
        scenario.name,
        scenario.price_target,
        scenario.horizon_days,
        `${Number(scenario.probability || 0) * 100}%`,
      ]),
    ));

    target.appendChild(node("h2", "Entry ladder"));
    target.appendChild(metricTable(
      ["Level", "Fraction", "Shares", "Notional"],
      (Array.isArray(sized.tranches) ? sized.tranches : []).map((tranche) => [
        tranche.price_level,
        `${Number(tranche.fraction || 0) * 100}%`,
        tranche.shares,
        tranche.notional,
      ]),
    ));
    target.appendChild(node(
      "p",
      `Total ${readable(sized.total_shares)} shares · risk budget `
      + `${readable(sized.risk_budget)}`,
      "muted",
    ));

    const exitPlan = thesis.exit_plan || {};
    target.appendChild(node("h2", "Exits"));
    target.appendChild(metricTable(
      ["Target", "Fraction to sell"],
      (Array.isArray(exitPlan.targets) ? exitPlan.targets : []).map((exit) => [
        exit.price_level,
        `${Number(exit.fraction_to_sell || 0) * 100}%`,
      ]),
    ));
    target.appendChild(node(
      "p",
      `Stop ${readable(exitPlan.stop)} · trailing `
      + `${readable(exitPlan.trailing_stop_pct, "not set")} · time stop `
      + `${readable(exitPlan.time_stop_days, "not set")}`,
      "muted",
    ));
    appendPlanActions(target, plan);
  } catch (error) {
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
  }
}

async function submitPlanApproval(event) {
  event.preventDefault();
  const reason = byId("plan-approval-reason").value.trim();
  if (!reason || activePlanId === null) {
    notify("A non-empty review reason is required.", "notice-error");
    return;
  }
  const planId = activePlanId;
  try {
    const result = await api(
      `/plans/${planId}/approve`,
      jsonPost({reason}),
    );
    closeDialog(byId("plan-approval-dialog"));
    notify(
      `Plan ${planId} approval returned ${result.bracket ? "broker bracket" : `${readable(result.rules_created, 0)} rules`}.`,
      "notice-success",
    );
    await refreshPlans();
    await showPlan(planId);
  } catch (error) {
    notify(errorText(error), "notice-error");
  }
}

async function submitPlanCancel(event) {
  event.preventDefault();
  const reason = byId("plan-cancel-reason").value.trim();
  if (!reason || activePlanId === null) {
    notify("A non-empty cancellation reason is required.", "notice-error");
    return;
  }
  const planId = activePlanId;
  try {
    await api(`/plans/${planId}/cancel`, jsonPost({reason}));
    closeDialog(byId("plan-cancel-dialog"));
    notify(`Plan ${planId} canceled.`, "notice-success");
    await refreshPlans();
    await showPlan(planId);
  } catch (error) {
    notify(errorText(error), "notice-error");
  }
}

async function initialize() {
  try {
    const session = await loadSession();
    byId("session-actor").textContent = readable(session.actor);
  } catch (_error) {
    return;
  }
  [
    byId("plan-approval-dialog"),
    byId("plan-cancel-dialog"),
  ].forEach(bindDialogReturnFocus);
  byId("sign-out").addEventListener("click", async () => {
    try {
      await logout();
    } catch (error) {
      notify(errorText(error), "notice-error");
    }
  });
  byId("analysis-form").addEventListener("submit", submitAnalysis);
  byId("proposal-form").addEventListener("submit", submitProposals);
  byId("screen-button").addEventListener("click", runScreen);
  byId("refresh-plans").addEventListener("click", refreshPlans);
  byId("plan-approval-form").addEventListener("submit", submitPlanApproval);
  byId("plan-approval-cancel").addEventListener(
    "click",
    () => closeDialog(byId("plan-approval-dialog")),
  );
  byId("plan-cancel-form").addEventListener("submit", submitPlanCancel);
  byId("plan-cancel-close").addEventListener(
    "click",
    () => closeDialog(byId("plan-cancel-dialog")),
  );
  await refreshPlans();
}

initialize();
