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

const byId = (id) => document.getElementById(id);
const dialogReturnFocus = new Map();
let plansRequestSequence = 0;
let plansAbortController = null;
let savedPlansState = [];
let selectedPlanId = null;
let planDetailState = null;
let planDetailRequestSequence = 0;
let planWorkspaceRequestSequence = 0;
let planApprovalState = null;
let planApprovalRequestSequence = 0;
let planCancelState = null;
let planCancelRequestSequence = 0;
let screenRequestSequence = 0;
let screenAbortController = null;
let postureRequestSequence = 0;
let postureAbortController = null;
let providerCallAllowed = false;
let providerCallReason = "Provider call blocked before network I/O";

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

function exactNonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function rateMetadata(error) {
  const metadata = [];
  if (exactNonnegativeInteger(error && error.retryAfter)) {
    metadata.push(`Retry after ${error.retryAfter} seconds`);
  }
  if (exactNonnegativeInteger(error && error.rateLimitReset)) {
    const resetTimestamp = new Date(error.rateLimitReset * 1000);
    const resetLabel = Number.isFinite(resetTimestamp.getTime())
      ? resetTimestamp.toISOString()
      : "time unavailable";
    metadata.push(
      `Rate limit reset ${error.rateLimitReset} (${resetLabel})`,
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

function notify(message, kind = "") {
  const notice = node("div", message, `notice ${kind}`.trim());
  byId("status-region").appendChild(notice);
  window.setTimeout(() => notice.remove(), kind === "notice-error" ? 9000 : 5000);
}

function showDialog(dialog, trigger) {
  dialogReturnFocus.set(dialog, trigger || document.activeElement);
  if (!dialog.open) {
    dialog.showModal();
  }
}

function closeDialog(dialog) {
  const target = dialogReturnFocus.get(dialog);
  dialogReturnFocus.delete(dialog);
  if (dialog.id === "plan-approval-dialog") {
    poisonPlanApprovalState();
  }
  if (dialog.id === "plan-cancel-dialog") {
    poisonPlanCancelState();
  }
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
  dialog.addEventListener("close", () => {
    const target = dialogReturnFocus.get(dialog);
    dialogReturnFocus.delete(dialog);
    if (dialog.id === "plan-approval-dialog") {
      poisonPlanApprovalState();
    }
    if (dialog.id === "plan-cancel-dialog") {
      poisonPlanCancelState();
    }
    if (target && typeof target.focus === "function") {
      target.focus();
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

function setBudgetValue(id, value, className) {
  const element = byId(id);
  if (!element) {
    return;
  }
  element.textContent = value;
  element.className = className;
}

function setPaidAvailability(allowed, reason) {
  providerCallAllowed = allowed === true;
  providerCallReason = typeof reason === "string" && reason.trim()
    ? reason.trim()
    : providerCallAllowed
      ? "Provider budget evidence is available."
      : "Provider call blocked before network I/O";
  ["analysis-submit", "proposal-submit"].forEach((id) => {
    const button = byId(id);
    if (button) {
      button.disabled = !providerCallAllowed;
    }
  });
  const state = byId("plans-budget-state");
  if (state) {
    state.textContent = providerCallReason;
    state.className = providerCallAllowed
      ? "field-hint is-verified"
      : "field-hint has-error is-blocked";
  }
}

function clearPlanBudget(error = null) {
  [
    "plans-budget-calls",
    "plans-budget-input",
    "plans-budget-output",
    "plans-budget-reset",
  ].forEach((id) => setBudgetValue(id, "Unknown", "is-unknown"));
  const detail = error ? errorText(error) : "";
  setPaidAvailability(
    false,
    "Provider call blocked before network I/O"
      + (detail ? `. ${detail}` : ""),
  );
}

function completeProviderCheck(check) {
  return Boolean(
    check
    && typeof check.scope === "string"
    && check.scope.trim()
    && [
      "budget_remaining",
      "budget_limit",
      "input_tokens_remaining",
      "input_tokens_limit",
      "output_tokens_remaining",
      "output_tokens_limit",
    ].every((field) => exactNonnegativeInteger(check[field]))
    && typeof check.reset_at === "string"
    && Number.isFinite(Date.parse(check.reset_at))
  );
}

function providerBudgetSummary(checks, remainingField, limitField) {
  return checks.map((check) => (
    `${check.scope}: ${check[remainingField]} / ${check[limitField]}`
  )).join(" · ");
}

function renderPlanBudget(normalized) {
  const checks = normalized
    && normalized.provider
    && Array.isArray(normalized.provider.checks)
    ? normalized.provider.checks
    : [];
  if (
    !normalized
    || normalized.valid !== true
    || checks.length === 0
    || checks.some((check) => !completeProviderCheck(check))
  ) {
    clearPlanBudget();
    return false;
  }
  const className = normalized.provider.blocked
    ? "is-blocked"
    : "is-verified";
  setBudgetValue(
    "plans-budget-calls",
    providerBudgetSummary(checks, "budget_remaining", "budget_limit"),
    className,
  );
  setBudgetValue(
    "plans-budget-input",
    providerBudgetSummary(
      checks,
      "input_tokens_remaining",
      "input_tokens_limit",
    ),
    className,
  );
  setBudgetValue(
    "plans-budget-output",
    providerBudgetSummary(
      checks,
      "output_tokens_remaining",
      "output_tokens_limit",
    ),
    className,
  );
  setBudgetValue(
    "plans-budget-reset",
    [...new Set(checks.map((check) => check.reset_at))].join(" · "),
    className,
  );
  setPaidAvailability(
    !normalized.provider.blocked,
    normalized.provider.reason,
  );
  return !normalized.provider.blocked;
}

async function refreshPlanPosture() {
  if (postureAbortController) {
    postureAbortController.abort();
  }
  const controller = new AbortController();
  postureAbortController = controller;
  const requestToken = ++postureRequestSequence;
  clearPlanBudget();
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
    const normalized = normalizePosture(payload);
    renderPlanBudget(normalized);
    return normalized.valid;
  } catch (error) {
    if (
      requestToken !== postureRequestSequence
      || postureAbortController !== controller
    ) {
      return false;
    }
    postureAbortController = null;
    clearPlanBudget(error);
    return false;
  }
}

function blockPaidCallBeforeNetwork() {
  if (providerCallAllowed) {
    return false;
  }
  notify(providerCallReason, "notice-error");
  return true;
}

function handlePaidCallFailure(error) {
  if (
    exactNonnegativeInteger(error && error.retryAfter)
    || exactNonnegativeInteger(error && error.rateLimitReset)
  ) {
    clearPlanBudget(error);
  }
}

function freshnessLabel(value, now = Date.now()) {
  const observedAt = Date.parse(value);
  if (!Number.isFinite(observedAt)) {
    return "Freshness unknown";
  }
  const ageMs = now - observedAt;
  if (ageMs < 0) {
    return "Freshness: future timestamp";
  }
  const minutes = Math.floor(ageMs / 60000);
  if (minutes < 1) {
    return "Freshness: under 1 minute old";
  }
  if (minutes < 60) {
    return `Freshness: ${minutes}m old`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 48) {
    return `Freshness: ${hours}h old`;
  }
  return `Freshness: ${Math.floor(hours / 24)}d old`;
}

function planStatusClass(status) {
  if (status === "proposed") {
    return "plan-status-proposed";
  }
  if (status === "approved") {
    return "plan-status-approved";
  }
  if (status === "canceled") {
    return "plan-status-canceled";
  }
  return "plan-status-neutral";
}

function planMatchesFilter(plan, query) {
  if (!query) {
    return true;
  }
  return [
    plan.plan_id,
    plan.symbol,
    plan.action,
    plan.status,
  ].some((value) => readable(value, "").toLowerCase().includes(query));
}

function renderPlanQueue() {
  const target = byId("plans-list");
  if (!target) {
    return;
  }
  const filter = byId("plan-filter");
  const query = filter ? filter.value.trim().toLowerCase() : "";
  const plans = savedPlansState.filter(
    (plan) => planMatchesFilter(plan, query),
  );
  clear(target);
  if (!savedPlansState.length) {
    target.appendChild(node("p", "No saved plans.", "empty-state"));
    return;
  }
  if (!plans.length) {
    target.appendChild(node(
      "p",
      "No saved plans match this local filter.",
      "empty-state",
    ));
    return;
  }
  plans.forEach((plan) => {
    const planId = canonicalPlanId(plan.plan_id);
    const button = node(
      "button",
      null,
      "plan-item plan-queue-row"
        + (selectedPlanId === planId ? " is-selected" : ""),
    );
    button.type = "button";
    button.disabled = planId === null;
    if (typeof button.setAttribute === "function") {
      button.setAttribute(
        "aria-pressed",
        selectedPlanId === planId ? "true" : "false",
      );
    }

    const heading = node("span", null, "plan-queue-heading");
    heading.appendChild(node(
      "strong",
      `#${readable(plan.plan_id)} · ${readable(plan.symbol)}`,
    ));
    heading.appendChild(node(
      "span",
      readable(plan.status),
      `status-chip plan-status-chip ${planStatusClass(plan.status)}`,
    ));
    button.appendChild(heading);

    const facts = node("span", null, "plan-queue-facts");
    [
      `Action ${readable(plan.action)}`,
      `Confidence ${readable(plan.confidence)}`,
      `As of ${readable(plan.as_of)}`,
      freshnessLabel(plan.as_of),
    ].forEach((fact) => facts.appendChild(node("span", fact)));
    button.appendChild(facts);
    button.appendChild(node(
      "span",
      plan.paper_only === true ? "Paper-only" : "Paper status unknown",
      plan.paper_only === true
        ? "status-chip is-caution"
        : "status-chip is-unknown",
    ));
    button.appendChild(node(
      "span",
      "Regime context available in detail.",
      "plan-queue-context",
    ));
    button.addEventListener("click", () => {
      if (planId === null) {
        return;
      }
      selectedPlanId = planId;
      renderPlanQueue();
      showPlan(planId);
    });
    target.appendChild(button);
  });
}

function validPlanSummary(plan) {
  return Boolean(
    plan
    && typeof plan === "object"
    && !Array.isArray(plan)
    && canonicalPlanId(plan.plan_id) !== null
    && typeof plan.symbol === "string"
    && Boolean(plan.symbol.trim())
    && typeof plan.action === "string"
    && Boolean(plan.action.trim())
    && typeof plan.status === "string"
    && Boolean(plan.status.trim())
    && typeof plan.paper_only === "boolean"
    && Number.isFinite(Number(plan.confidence))
    && typeof plan.as_of === "string"
    && Number.isFinite(Date.parse(plan.as_of))
  );
}

function clearSelectedPlanAuthority(message) {
  savedPlansState = [];
  selectedPlanId = null;
  beginPlanWorkspace();
  const approvalDialog = byId("plan-approval-dialog");
  const cancelDialog = byId("plan-cancel-dialog");
  if (approvalDialog && approvalDialog.open) {
    closeDialog(approvalDialog);
  } else {
    poisonPlanApprovalState();
  }
  if (cancelDialog && cancelDialog.open) {
    closeDialog(cancelDialog);
  } else {
    poisonPlanCancelState();
  }
  const title = byId("plan-detail-title");
  if (title) {
    title.textContent = "Plan detail unavailable";
  }
  const detail = byId("plan-detail");
  if (detail) {
    clear(detail);
    detail.appendChild(node(
      "p",
      message,
      "banner-caution",
    ));
  }
}

async function refreshPlans() {
  const target = byId("plans-list");
  if (plansAbortController) {
    plansAbortController.abort();
  }
  const controller = new AbortController();
  plansAbortController = controller;
  const requestToken = ++plansRequestSequence;
  clear(target);
  target.appendChild(node(
    "p",
    "Refreshing saved plans…",
    "empty-state",
  ));
  let payload;
  try {
    payload = await api("/plans", {
      signal: controller.signal,
    });
    if (
      !payload
      || typeof payload !== "object"
      || Array.isArray(payload)
      || !Array.isArray(payload.plans)
      || payload.plans.some((plan) => !validPlanSummary(plan))
    ) {
      throw new Error("Saved plan response was invalid");
    }
  } catch (error) {
    if (
      requestToken !== plansRequestSequence
      || plansAbortController !== controller
    ) {
      return false;
    }
    plansAbortController = null;
    clearSelectedPlanAuthority(
      "Saved plan truth is unavailable; prior plan authority was cleared.",
    );
    clear(target);
    target.appendChild(node(
      "p",
      "Saved plan truth is unavailable.",
      "empty-state",
    ));
    throw error;
  }
  if (
    requestToken !== plansRequestSequence
    || plansAbortController !== controller
  ) {
    return false;
  }
  plansAbortController = null;
  savedPlansState = payload.plans.slice();
  renderPlanQueue();
  return true;
}

async function submitAnalysis(event) {
  event.preventDefault();
  if (blockPaidCallBeforeNetwork()) {
    return false;
  }
  const symbol = byId("analysis-symbol").value.trim().toUpperCase();
  const reason = byId("analysis-reason").value.trim();
  if (!symbol || !reason) {
    notify("A symbol and non-empty analysis reason are required.", "notice-error");
    return;
  }
  const workspaceToken = beginPlanWorkspace();
  const target = byId("plan-detail");
  byId("plan-detail-title").textContent = `Analyzing ${symbol}`;
  clear(target);
  target.appendChild(node("p", `Analyzing ${symbol}…`, "empty-state"));
  try {
    const result = await api("/analyze", jsonPost({symbol, reason}));
    await refreshPlans();
    if (workspaceToken !== planWorkspaceRequestSequence) {
      return false;
    }
    byId("analysis-reason").value = "";
    await showPlan(result.plan_id);
    return true;
  } catch (error) {
    if (workspaceToken !== planWorkspaceRequestSequence) {
      return false;
    }
    handlePaidCallFailure(error);
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
    return false;
  }
}

async function submitProposals(event) {
  event.preventDefault();
  if (blockPaidCallBeforeNetwork()) {
    return false;
  }
  const reason = byId("proposal-reason").value.trim();
  if (!reason) {
    notify("A non-empty proposal reason is required.", "notice-error");
    return;
  }
  const workspaceToken = beginPlanWorkspace();
  const target = byId("plan-detail");
  byId("plan-detail-title").textContent = "Generating proposals";
  clear(target);
  target.appendChild(node(
    "p",
    "Screening and analyzing candidates. Analyst output remains unproven.",
    "empty-state",
  ));
  try {
    const result = await api("/propose", jsonPost({n: 3, reason}));
    await refreshPlans();
    if (workspaceToken !== planWorkspaceRequestSequence) {
      return false;
    }
    byId("proposal-reason").value = "";
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
    return true;
  } catch (error) {
    if (workspaceToken !== planWorkspaceRequestSequence) {
      return false;
    }
    handlePaidCallFailure(error);
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
    return false;
  }
}

async function runScreen() {
  const target = byId("screen-results");
  if (screenAbortController) {
    screenAbortController.abort();
  }
  const controller = new AbortController();
  screenAbortController = controller;
  const requestToken = ++screenRequestSequence;
  clear(target);
  target.appendChild(node("p", "Screening…", "empty-state"));
  try {
    const result = await api("/screen", {
      method: "POST",
      signal: controller.signal,
    });
    if (
      requestToken !== screenRequestSequence
      || screenAbortController !== controller
    ) {
      return false;
    }
    screenAbortController = null;
    clear(target);
    const candidates = Array.isArray(result.candidates)
      ? result.candidates
      : [];
    if (!candidates.length) {
      target.appendChild(node("p", "No candidates reported.", "empty-state"));
      return true;
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
    return true;
  } catch (error) {
    if (
      requestToken !== screenRequestSequence
      || screenAbortController !== controller
    ) {
      return false;
    }
    screenAbortController = null;
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
    return false;
  }
}

function canonicalPlanId(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function beginPlanWorkspace() {
  planDetailState = null;
  const requestToken = ++planWorkspaceRequestSequence;
  updatePlanApprovalButton();
  return requestToken;
}

function planDetailTokenIsCurrent(requestToken, targetPlanId) {
  return Boolean(
    planDetailState
    && planDetailState.requestToken === requestToken
    && planDetailState.targetPlanId === targetPlanId
    && requestToken === planDetailRequestSequence
    && planDetailState.workspaceToken === planWorkspaceRequestSequence
  );
}

function currentDetailMatches(planId, detailRequestToken) {
  return Boolean(
    planDetailTokenIsCurrent(detailRequestToken, planId)
    && planDetailState.plan
    && planDetailState.plan.plan_id === planId
  );
}

function poisonPlanApprovalState() {
  planApprovalRequestSequence += 1;
  planApprovalState = null;
  const reason = byId("plan-approval-reason");
  const submit = byId("plan-approval-submit");
  if (reason) {
    reason.value = "";
  }
  if (submit) {
    submit.disabled = true;
  }
}

function poisonPlanCancelState() {
  planCancelRequestSequence += 1;
  planCancelState = null;
  const reason = byId("plan-cancel-reason");
  if (reason) {
    reason.value = "";
  }
}

function planApprovalIsActionable() {
  const state = planApprovalState;
  if (
    !state
    || state.submitting === true
    || state.requestToken !== planApprovalRequestSequence
    || !byId("plan-approval-dialog").open
    || !currentDetailMatches(
      state.targetPlanId,
      state.detailRequestToken,
    )
  ) {
    return false;
  }
  const plan = planDetailState.plan;
  return Boolean(
    plan.status === "proposed"
    && Number(plan.sized && plan.sized.total_shares) > 0
    && state.symbol === plan.symbol
    && state.action === (plan.plan && plan.plan.action)
    && typeof state.reviewToken === "string"
    && state.reviewToken.length > 0
    && state.reviewToken === plan.review_token
  );
}

function updatePlanApprovalButton() {
  const reasonElement = byId("plan-approval-reason");
  const submit = byId("plan-approval-submit");
  if (!reasonElement || !submit) {
    return;
  }
  const reason = reasonElement.value.trim();
  submit.disabled = !(
    planApprovalIsActionable() && reason
  );
}

function openPlanApproval(plan, detailRequestToken, invoker) {
  const targetPlanId = canonicalPlanId(plan.plan_id);
  if (
    targetPlanId === null
    || !currentDetailMatches(targetPlanId, detailRequestToken)
  ) {
    notify("Plan approval target is no longer current.", "notice-error");
    return;
  }
  const requestToken = ++planApprovalRequestSequence;
  planApprovalState = Object.freeze({
    targetPlanId,
    detailRequestToken,
    requestToken,
    symbol: plan.symbol,
    action: plan.plan && plan.plan.action,
    reviewToken: plan.review_token,
    invoker,
    submitting: false,
  });
  byId("plan-approval-target-id").textContent = String(targetPlanId);
  byId("plan-approval-target-symbol").textContent = readable(plan.symbol);
  byId("plan-approval-target-action").textContent = readable(
    plan.plan && plan.plan.action,
  );
  byId("plan-approval-reason").value = "";
  byId("plan-approval-submit").disabled = true;
  showDialog(byId("plan-approval-dialog"), invoker);
  byId("plan-approval-reason").focus();
}

function openPlanCancel(plan, detailRequestToken, invoker) {
  const targetPlanId = canonicalPlanId(plan.plan_id);
  if (
    targetPlanId === null
    || !currentDetailMatches(targetPlanId, detailRequestToken)
  ) {
    notify("Plan cancellation target is no longer current.", "notice-error");
    return;
  }
  planCancelState = Object.freeze({
    targetPlanId,
    detailRequestToken,
    requestToken: ++planCancelRequestSequence,
    symbol: plan.symbol,
    action: plan.plan && plan.plan.action,
    invoker,
    submitting: false,
  });
  byId("plan-cancel-target-id").textContent = String(targetPlanId);
  byId("plan-cancel-target-symbol").textContent = readable(plan.symbol);
  byId("plan-cancel-target-action").textContent = readable(
    plan.plan && plan.plan.action,
  );
  byId("plan-cancel-reason").value = "";
  showDialog(byId("plan-cancel-dialog"), invoker);
  byId("plan-cancel-reason").focus();
}

function appendPlanActions(target, plan, detailRequestToken) {
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
      openPlanApproval(plan, detailRequestToken, approve);
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
    openPlanCancel(plan, detailRequestToken, cancel);
  });
  actions.appendChild(cancel);
  target.appendChild(actions);
}

function definitionGrid(items, className = "plan-evidence-grid") {
  const list = node("dl", null, className);
  items.forEach(([label, value, valueClass = ""]) => {
    const item = node("div");
    item.appendChild(node("dt", label));
    item.appendChild(node("dd", value, valueClass));
    list.appendChild(item);
  });
  return list;
}

function evidenceSection(title) {
  const section = node("section", null, "plan-evidence-section");
  section.appendChild(node("h3", title));
  return section;
}

function percentage(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? `${numeric * 100}%`
    : "Unavailable";
}

function nullableEvidence(value) {
  return value === null || value === undefined || value === ""
    ? "Not recorded"
    : String(value);
}

function optionalSetting(value) {
  return value === null || value === undefined || value === ""
    ? "Not set"
    : String(value);
}

function availabilityText(value) {
  if (value === "not_recorded") {
    return "Not recorded";
  }
  if (value === "references_only") {
    return "References only";
  }
  return "Availability unknown";
}

function appendStringList(section, values, className, emptyLabel) {
  if (!Array.isArray(values) || values.length === 0) {
    section.appendChild(node("p", emptyLabel, "muted"));
    return;
  }
  const list = node("ul", null, className);
  values.forEach((value) => {
    list.appendChild(node("li", readable(value)));
  });
  section.appendChild(list);
}

function appendSourceReferences(section, thesis, availability) {
  const state = availability.source_evidence;
  section.appendChild(node(
    "p",
    availabilityText(state),
    state === "references_only" ? "banner-caution" : "muted",
  ));
  if (state !== "references_only") {
    return;
  }
  const references = Array.isArray(thesis.cited_source_refs)
    ? thesis.cited_source_refs
    : [];
  if (!references.length) {
    section.appendChild(node(
      "p",
      "No opaque reference identifiers were returned.",
      "muted",
    ));
    return;
  }
  const list = node("ul", null, "plan-reference-list");
  references.forEach((reference) => {
    const item = node("li");
    item.appendChild(node("code", readable(reference)));
    list.appendChild(item);
  });
  section.appendChild(list);
}

function renderPersistedPlan(target, plan, detailRequestToken) {
  const thesis = plan.plan || {};
  const sized = plan.sized || {};
  const availability = plan.evidence_availability || {};

  if (plan.paper_only === true) {
    target.appendChild(node(
      "p",
      "Research / paper-only · persisted analyst evidence is not proof of profitability.",
      "banner-caution",
    ));
  } else {
    target.appendChild(node(
      "p",
      "Paper-only authority is not verified for this record.",
      "banner-caution",
    ));
  }

  const identity = evidenceSection("Plan identity");
  const heading = node("div", null, "plan-detail-heading");
  heading.appendChild(node("strong", readable(plan.symbol)));
  heading.appendChild(node(
    "span",
    readable(plan.status),
    `status-chip plan-status-chip ${planStatusClass(plan.status)}`,
  ));
  heading.appendChild(node(
    "span",
    plan.paper_only === true ? "Paper-only" : "Paper status unknown",
    plan.paper_only === true
      ? "status-chip is-caution"
      : "status-chip is-unknown",
  ));
  identity.appendChild(heading);
  identity.appendChild(definitionGrid([
    ["Plan ID", readable(plan.plan_id)],
    ["Action", readable(thesis.action)],
    ["Confidence", readable(thesis.confidence)],
    ["Reference price", readable(thesis.reference_price)],
    ["As of", readable(thesis.as_of)],
    ["Status", readable(plan.status)],
  ]));
  target.appendChild(identity);

  const thesisSection = evidenceSection("Persisted thesis");
  thesisSection.appendChild(node("p", readable(thesis.thesis)));
  target.appendChild(thesisSection);

  const context = evidenceSection("Recorded context");
  context.appendChild(definitionGrid([
    ["Regime context", readable(thesis.regime_note)],
    ["Earnings note", nullableEvidence(thesis.earnings_note)],
    ["Correlation note", nullableEvidence(thesis.correlation_note)],
  ]));
  context.appendChild(node("h4", "Cited playbook concepts"));
  appendStringList(
    context,
    thesis.cited_concepts,
    "plan-concept-list",
    "Not recorded",
  );
  target.appendChild(context);

  const scenarios = evidenceSection("Scenarios");
  scenarios.appendChild(metricTable(
    ["Case", "Target", "Days", "Probability"],
    (Array.isArray(thesis.scenarios) ? thesis.scenarios : []).map((scenario) => [
      scenario.name,
      scenario.price_target,
      scenario.horizon_days,
      percentage(scenario.probability),
    ]),
  ));
  target.appendChild(scenarios);

  const entry = evidenceSection("Entry plan and deterministic sizing");
  const entryPlan = thesis.entry_plan || {};
  entry.appendChild(definitionGrid([
    ["Entry type", readable(entryPlan.type)],
    ["Direction", readable(sized.direction)],
    ["Total shares", readable(sized.total_shares)],
    ["Risk budget", readable(sized.risk_budget)],
    ["Zero-size reason", readable(sized.zero_reason, "None")],
  ]));
  entry.appendChild(node("h4", "Persisted entry levels"));
  entry.appendChild(metricTable(
    ["Level", "Fraction"],
    (Array.isArray(entryPlan.tranches) ? entryPlan.tranches : []).map(
      (tranche) => [
        tranche.price_level,
        percentage(tranche.fraction),
      ],
    ),
  ));
  entry.appendChild(node("h4", "Deterministic sized tranches"));
  entry.appendChild(metricTable(
    ["Level", "Fraction", "Shares", "Notional"],
    (Array.isArray(sized.tranches) ? sized.tranches : []).map((tranche) => [
      tranche.price_level,
      percentage(tranche.fraction),
      tranche.shares,
      tranche.notional,
    ]),
  ));
  target.appendChild(entry);

  const invalidation = thesis.invalidation || {};
  const invalidationSection = evidenceSection("Invalidation");
  invalidationSection.appendChild(definitionGrid([
    ["Price level", readable(invalidation.price_level)],
    ["Rationale", readable(invalidation.rationale)],
  ]));
  target.appendChild(invalidationSection);

  const exitPlan = thesis.exit_plan || {};
  const exits = evidenceSection("Exit plan");
  exits.appendChild(metricTable(
    ["Target", "Fraction to sell"],
    (Array.isArray(exitPlan.targets) ? exitPlan.targets : []).map((exit) => [
      exit.price_level,
      percentage(exit.fraction_to_sell),
    ]),
  ));
  exits.appendChild(definitionGrid([
    ["Stop", readable(exitPlan.stop)],
    ["Trailing stop %", optionalSetting(exitPlan.trailing_stop_pct)],
    ["Time stop days", optionalSetting(exitPlan.time_stop_days)],
  ]));
  target.appendChild(exits);

  const availabilitySection = evidenceSection("Evidence availability");
  availabilitySection.appendChild(definitionGrid([
    ["Injection flags", availabilityText(availability.injection_flags)],
    ["Uncertainties", availabilityText(availability.uncertainties)],
    ["Catalysts", availabilityText(availability.catalysts)],
    ["Risks", availabilityText(availability.risks)],
    ["Market context", availabilityText(availability.market_context)],
    [
      "Relative strength vs SPY",
      availabilityText(availability.relative_strength_vs_spy),
    ],
    [
      "Days to next earnings",
      availabilityText(availability.days_to_next_earnings),
    ],
  ], "plan-availability-grid"));
  target.appendChild(availabilitySection);

  const sources = evidenceSection("Source references");
  appendSourceReferences(sources, thesis, availability);
  target.appendChild(sources);

  appendPlanActions(target, plan, detailRequestToken);
}

async function showPlan(planId) {
  const workspaceToken = beginPlanWorkspace();
  const targetPlanId = canonicalPlanId(planId);
  const target = byId("plan-detail");
  clear(target);
  if (targetPlanId === null) {
    planDetailState = null;
    target.appendChild(node(
      "p",
      "Plan identity mismatch. Choose a current plan.",
      "banner-caution",
    ));
    updatePlanApprovalButton();
    return;
  }
  const requestToken = ++planDetailRequestSequence;
  planDetailState = Object.freeze({
    targetPlanId,
    requestToken,
    workspaceToken,
    plan: null,
  });
  updatePlanApprovalButton();
  byId("plan-detail-title").textContent = `Loading plan #${targetPlanId}`;
  target.appendChild(node("p", "Loading plan…", "empty-state"));
  try {
    const plan = await api(`/plans/${targetPlanId}`);
    if (!planDetailTokenIsCurrent(requestToken, targetPlanId)) {
      return;
    }
    if (canonicalPlanId(plan && plan.plan_id) !== targetPlanId) {
      planDetailState = Object.freeze({
        targetPlanId,
        requestToken,
        workspaceToken,
        plan: null,
      });
      clear(target);
      target.appendChild(node(
        "p",
        "Plan identity mismatch. Refresh and choose the plan again.",
        "banner-caution",
      ));
      byId("plan-detail-title").textContent = (
        `Plan #${targetPlanId} unavailable`
      );
      updatePlanApprovalButton();
      return;
    }
    planDetailState = Object.freeze({
      targetPlanId,
      requestToken,
      workspaceToken,
      plan: Object.freeze(plan),
    });
    byId("plan-detail-title").textContent = (
      `Plan #${readable(plan.plan_id)} · ${readable(plan.symbol)} · `
      + readable(plan.status)
    );
    clear(target);
    selectedPlanId = targetPlanId;
    renderPlanQueue();
    renderPersistedPlan(target, plan, requestToken);
    updatePlanApprovalButton();
  } catch (error) {
    if (!planDetailTokenIsCurrent(requestToken, targetPlanId)) {
      return;
    }
    planDetailState = Object.freeze({
      targetPlanId,
      requestToken,
      workspaceToken,
      plan: null,
    });
    clear(target);
    target.appendChild(node("p", errorText(error), "banner-caution"));
    updatePlanApprovalButton();
  }
}

function boundedReceiptCount(value) {
  return (
    Number.isSafeInteger(value)
    && value >= 0
    && value <= 10000
  );
}

function validPlanApprovalReceipt(result, planId) {
  const bracket = result && result.bracket;
  return Boolean(
    result
    && typeof result === "object"
    && !Array.isArray(result)
    && canonicalPlanId(result.plan_id) === planId
    && result.status === "approved"
    && result.paper_only === true
    && boundedReceiptCount(result.rules_created)
    && (
      result.rules_created > 0
      || (
        bracket
        && typeof bracket === "object"
        && !Array.isArray(bracket)
      )
    )
  );
}

function validPlanCancellationReceipt(result, planId) {
  return Boolean(
    result
    && typeof result === "object"
    && !Array.isArray(result)
    && canonicalPlanId(result.plan_id) === planId
    && result.status === "canceled"
    && boundedReceiptCount(result.rules_canceled)
  );
}

async function submitPlanApproval(event) {
  event.preventDefault();
  const state = planApprovalState;
  const reason = byId("plan-approval-reason").value.trim();
  if (!reason || !state || !planApprovalIsActionable()) {
    notify("A non-empty review reason is required.", "notice-error");
    updatePlanApprovalButton();
    return;
  }
  const planId = state.targetPlanId;
  if (
    state.requestToken !== planApprovalRequestSequence
    || !currentDetailMatches(planId, state.detailRequestToken)
    || planDetailState.plan.plan_id !== planId
    || state.reviewToken !== planDetailState.plan.review_token
  ) {
    updatePlanApprovalButton();
    return;
  }
  planApprovalState = Object.freeze({
    ...state,
    submitting: true,
  });
  poisonPlanApprovalState();
  try {
    const result = await api(
      `/plans/${planId}/approve`,
      jsonPost({reason, review_token: state.reviewToken}),
    );
    if (!validPlanApprovalReceipt(result, planId)) {
      throw new Error(
        "Server response did not prove exact paper plan approval",
      );
    }
    closeDialog(byId("plan-approval-dialog"));
    notify(
      `Plan ${planId} paper approval recorded · `
      + (
        result.bracket
          ? "paper broker bracket receipt recorded"
          : `${result.rules_created} paper-only rules recorded`
      )
      + ".",
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
  const state = planCancelState;
  const reason = byId("plan-cancel-reason").value.trim();
  if (
    !reason
    || !state
    || state.submitting === true
    || state.requestToken !== planCancelRequestSequence
    || !byId("plan-cancel-dialog").open
    || !currentDetailMatches(
      state.targetPlanId,
      state.detailRequestToken,
    )
    || state.symbol !== planDetailState.plan.symbol
    || state.action !== (
      planDetailState.plan.plan && planDetailState.plan.plan.action
    )
  ) {
    notify("A non-empty cancellation reason is required.", "notice-error");
    return;
  }
  const planId = state.targetPlanId;
  planCancelState = Object.freeze({
    ...state,
    submitting: true,
  });
  try {
    const result = await api(
      `/plans/${planId}/cancel`,
      jsonPost({reason}),
    );
    if (!validPlanCancellationReceipt(result, planId)) {
      throw new Error(
        "Server response did not prove exact plan cancellation",
      );
    }
    poisonPlanCancelState();
    closeDialog(byId("plan-cancel-dialog"));
    notify(
      `Plan ${planId} canceled · ${result.rules_canceled} paper-only `
        + "rules canceled.",
      "notice-success",
    );
    await refreshPlans();
    await showPlan(planId);
  } catch (error) {
    poisonPlanCancelState();
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
  byId("refresh-plan-budget").addEventListener(
    "click",
    refreshPlanPosture,
  );
  byId("plan-filter").addEventListener("input", renderPlanQueue);
  byId("plan-approval-form").addEventListener("submit", submitPlanApproval);
  byId("plan-approval-reason").addEventListener(
    "input",
    updatePlanApprovalButton,
  );
  byId("plan-approval-cancel").addEventListener(
    "click",
    () => closeDialog(byId("plan-approval-dialog")),
  );
  byId("plan-cancel-form").addEventListener("submit", submitPlanCancel);
  byId("plan-cancel-close").addEventListener(
    "click",
    () => closeDialog(byId("plan-cancel-dialog")),
  );
  clearPlanBudget();
  await Promise.allSettled([
    refreshPlanPosture(),
    refreshPlans(),
  ]);
}

initialize();
