"use strict";

import {
  api,
  jsonPost,
  loadSession,
  logout,
} from "/static/js/auth.js";

const byId = (id) => document.getElementById(id);
const breakerScopes = ["equity", "crypto"];
const unsafeLocalIdFields = [
  "live_or_unknown_order_ids",
  "latched_order_ids",
  "unsafe_fill_ids",
  "active_rule_ids",
  "unsafe_rule_group_ids",
];
const dialogReturnFocus = new Map();
const HEALTH_OBSERVATION_MAX_AGE_MS = 30000;

let latestHealth = null;
let healthRequestSequence = 0;
let breakerResetInFlight = false;
let pendingOrders = new Map();
let pendingRequestSequence = 0;
let approvalDialogState = null;
let approvalRequestSequence = 0;
let rejectionOrderId = null;
let unsafePanicLatched = false;

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
  if (value === undefined || value === null || value === "") {
    return fallback;
  }
  return String(value);
}

function listText(values) {
  return Array.isArray(values) && values.length
    ? values.map((value) => String(value)).join(", ")
    : "None reported";
}

function errorText(error) {
  const message = error && typeof error.message === "string"
    ? error.message
    : "Request failed";
  const requestId = error && typeof error.requestId === "string"
    ? ` Request ${error.requestId}.`
    : "";
  return `${message}.${requestId}`;
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
  if (dialog.id === "approval-dialog") {
    poisonApprovalState();
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
    if (dialog.id === "approval-dialog") {
      poisonApprovalState();
    }
    if (target && typeof target.focus === "function") {
      target.focus();
    }
  });
}

function setState(element, label, kind) {
  element.textContent = label;
  element.className = `state state-${kind}`;
}

function makeTable(headers, rows) {
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
    values.forEach((value) => row.appendChild(node("td", value)));
    body.appendChild(row);
  });
  table.appendChild(body);
  wrapper.appendChild(table);
  return wrapper;
}

function appendReceipt(title, summary, rows, kind = "") {
  const panel = byId("receipt-panel");
  const empty = panel.querySelector(".empty-state");
  if (empty) {
    empty.remove();
  }
  const receipt = node("article", null, `receipt ${kind}`.trim());
  receipt.appendChild(node("h3", title));
  receipt.appendChild(node("p", summary));
  const details = node("dl", null, "receipt-grid");
  rows.forEach(([label, value]) => {
    details.appendChild(node("dt", label));
    details.appendChild(node("dd", readable(value)));
  });
  receipt.appendChild(details);
  panel.prepend(receipt);
}

function orderSummary(order) {
  const amount = order.notional
    ? `$${order.notional}`
    : `${order.qty} shares`;
  const limit = order.limit_price ? ` at ${order.limit_price}` : "";
  return `${order.side} ${amount} · ${order.order_type}${limit}`;
}

async function refreshPending() {
  const requestToken = ++pendingRequestSequence;
  pendingOrders = new Map();
  const list = byId("pending-list");
  clear(list);
  list.appendChild(node("li", "Refreshing pending proposals…", "empty-state"));
  updateApprovalButton();
  let payload;
  try {
    payload = await api("/pending");
  } catch (error) {
    if (requestToken === pendingRequestSequence) {
      clear(list);
      list.appendChild(node(
        "li",
        "Pending proposal truth is unavailable.",
        "empty-state",
      ));
      updateApprovalButton();
    }
    throw error;
  }
  if (requestToken !== pendingRequestSequence) {
    return;
  }
  clear(list);
  const pending = Array.isArray(payload.pending) ? payload.pending : [];
  const verifiedPending = pending
    .filter((order) => canonicalPendingOrder(order) !== null)
    .map((order) => Object.freeze({...order}));
  pendingOrders = new Map(
    verifiedPending.map((order) => [order.order_id, order]),
  );
  updateApprovalButton();
  if (!verifiedPending.length) {
    list.appendChild(node(
      "li",
      "No verified pending proposals.",
      "empty-state",
    ));
    return;
  }
  verifiedPending.forEach((order) => {
    const item = node("li", null, "ledger-entry");
    const content = node("div");
    const title = node("div", null, "ledger-entry-title");
    title.appendChild(node("strong", readable(order.ticker)));
    title.appendChild(node("span", "Awaiting human decision", "status-chip"));
    content.appendChild(title);
    content.appendChild(node(
      "div",
      orderSummary(order),
      "ledger-entry-data",
    ));
    content.appendChild(node(
      "div",
      `Expires ${readable(order.expires_at)}`,
      "ledger-entry-data",
    ));

    const actions = node("div", null, "ledger-actions");
    const approve = node("button", "Review approval");
    approve.type = "button";
    approve.addEventListener("click", () => openApproval(order.order_id, approve));
    const reject = node("button", "Reject", "button-danger");
    reject.type = "button";
    reject.addEventListener("click", () => openRejection(order.order_id, reject));
    actions.append(approve, reject);
    item.append(content, actions);
    list.appendChild(item);
  });
}

function canonicalId(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function canonicalPendingOrder(order) {
  if (
    !order
    || canonicalId(order.order_id) === null
    || typeof order.ticker !== "string"
    || !order.ticker
    || typeof order.side !== "string"
    || !order.side
    || typeof order.order_type !== "string"
    || !order.order_type
    || order.status !== "proposed"
    || order.expired !== false
    || typeof order.expires_at !== "string"
    || !order.expires_at
  ) {
    return null;
  }
  return order;
}

function exactNullableString(left, right) {
  if (left === null || right === null) {
    return left === right;
  }
  return typeof left === "string" && left === right;
}

function pendingMatchesProof(pending, proof) {
  if (!pending || !proof || !proof.order) {
    return false;
  }
  const order = proof.order;
  return (
    pending.order_id === order.order_id
    && pending.ticker === order.symbol
    && pending.side === order.side
    && pending.order_type === order.order_type
    && exactNullableString(pending.qty, order.quantity)
    && exactNullableString(pending.notional, order.notional)
    && exactNullableString(pending.limit_price, order.limit_price)
    && pending.expires_at === proof.expires_at
  );
}

function proofHasRequiredFields(proof, targetOrderId, pending) {
  if (
    !proof
    || proof.complete !== true
    || proof.broker !== "Alpaca"
    || proof.mode !== "paper"
    || !proof.order
    || !proof.exposure
    || canonicalId(proof.order.order_id) !== targetOrderId
    || !pendingMatchesProof(pending, proof)
  ) {
    return false;
  }
  const order = proof.order;
  const exposure = proof.exposure;
  const exactAmount = (
    (typeof order.quantity === "string" && order.quantity)
    || (typeof order.notional === "string" && order.notional)
  );
  const expiry = Date.parse(proof.expires_at);
  return Boolean(
    typeof proof.expires_at === "string"
    && proof.expires_at
    && Number.isFinite(expiry)
    && expiry > Date.now()
    && typeof order.symbol === "string"
    && order.symbol
    && typeof order.side === "string"
    && order.side
    && typeof order.order_type === "string"
    && order.order_type
    && exactAmount
    && typeof exposure.current_position_quantity === "string"
    && typeof exposure.current_signed_notional === "string"
    && typeof exposure.resulting_signed_notional === "string"
    && typeof exposure.as_of === "string"
    && exposure.as_of
  );
}

function approvalTokenIsCurrent(requestToken, targetOrderId) {
  const state = approvalDialogState;
  return Boolean(
    state
    && state.requestToken === requestToken
    && state.targetOrderId === targetOrderId
    && requestToken === approvalRequestSequence
    && byId("approval-dialog").open
  );
}

function currentApprovalStateIsActionable() {
  const state = approvalDialogState;
  if (
    !state
    || state.submitting === true
    || !approvalTokenIsCurrent(state.requestToken, state.targetOrderId)
  ) {
    return false;
  }
  const pending = pendingOrders.get(state.targetOrderId);
  return proofHasRequiredFields(
    state.proof,
    state.targetOrderId,
    pending,
  );
}

function clearApprovalProofFields() {
  byId("approval-broker").textContent = "Unknown";
  byId("approval-mode").textContent = "Unknown";
  byId("approval-symbol").textContent = "Unknown";
  byId("approval-side").textContent = "Unknown";
  byId("approval-order-type").textContent = "Unknown";
  byId("approval-quantity").textContent = "Not used";
  byId("approval-notional").textContent = "Not used";
  byId("approval-limit-price").textContent = "Not used";
  byId("approval-expiry").textContent = "Unknown";
  byId("approval-current-quantity").textContent = "Unknown";
  byId("approval-current-exposure").textContent = "Unknown";
  byId("approval-resulting-exposure").textContent = "Unknown";
  byId("approval-exposure-time").textContent = "Unknown";
}

function poisonApprovalState() {
  approvalRequestSequence += 1;
  approvalDialogState = null;
  byId("approval-confirm-button").disabled = true;
  byId("approval-reason").value = "";
}

function renderApprovalProof(proof, requestToken, targetOrderId) {
  if (!approvalTokenIsCurrent(requestToken, targetOrderId)) {
    return;
  }
  const pending = pendingOrders.get(targetOrderId);
  const complete = proofHasRequiredFields(proof, targetOrderId, pending);
  if (!complete) {
    clearApprovalProofFields();
    approvalDialogState = Object.freeze({
      ...approvalDialogState,
      proof: null,
    });
    const missing = proof && Array.isArray(proof.missing_proof)
      ? listText(proof.missing_proof)
      : "required or exact proof fields";
    const status = byId("approval-proof-status");
    status.textContent = `Approval disabled. Refresh missing proof: ${missing}.`;
    status.className = "proof-status";
    updateApprovalButton();
    return;
  }

  const order = proof && proof.order ? proof.order : {};
  const exposure = proof && proof.exposure ? proof.exposure : {};
  byId("approval-broker").textContent = readable(proof && proof.broker);
  byId("approval-mode").textContent = readable(proof && proof.mode);
  byId("approval-symbol").textContent = readable(order.symbol);
  byId("approval-side").textContent = readable(order.side);
  byId("approval-order-type").textContent = readable(order.order_type);
  byId("approval-quantity").textContent = readable(order.quantity, "Not used");
  byId("approval-notional").textContent = readable(order.notional, "Not used");
  byId("approval-limit-price").textContent = readable(order.limit_price, "Not used");
  byId("approval-expiry").textContent = readable(proof && proof.expires_at);
  byId("approval-current-quantity").textContent = readable(
    exposure.current_position_quantity,
  );
  byId("approval-current-exposure").textContent = readable(
    exposure.current_signed_notional,
  );
  byId("approval-resulting-exposure").textContent = readable(
    exposure.resulting_signed_notional,
  );
  byId("approval-exposure-time").textContent = readable(exposure.as_of);

  const status = byId("approval-proof-status");
  status.textContent = "Complete server proof received. Enter a reason to enable approval.";
  status.className = "proof-status proof-status-complete";
  approvalDialogState = Object.freeze({
    ...approvalDialogState,
    proof: Object.freeze(proof),
  });
  updateApprovalButton();
}

function updateApprovalButton() {
  const reason = byId("approval-reason").value.trim();
  byId("approval-confirm-button").disabled = !(
    currentApprovalStateIsActionable() && reason
  );
}

async function openApproval(orderId, trigger) {
  const targetOrderId = canonicalId(orderId);
  const pending = targetOrderId === null
    ? null
    : pendingOrders.get(targetOrderId);
  if (!pending) {
    notify("Approval target is no longer a current pending order.", "notice-error");
    return;
  }
  const requestToken = ++approvalRequestSequence;
  approvalDialogState = Object.freeze({
    targetOrderId,
    requestToken,
    pendingOrder: pending,
    proof: null,
    invoker: trigger || document.activeElement,
    submitting: false,
  });
  byId("approval-reason").value = "";
  byId("approval-confirm-button").disabled = true;
  clearApprovalProofFields();
  byId("approval-proof-status").textContent = "Loading exact server proof…";
  byId("approval-proof-status").className = "proof-status";
  const dialog = byId("approval-dialog");
  showDialog(dialog, trigger);
  try {
    const proof = await api(`/pending/${targetOrderId}/confirmation`);
    if (!approvalTokenIsCurrent(requestToken, targetOrderId)) {
      return;
    }
    renderApprovalProof(proof, requestToken, targetOrderId);
    byId("approval-reason").focus();
  } catch (error) {
    if (!approvalTokenIsCurrent(requestToken, targetOrderId)) {
      return;
    }
    clearApprovalProofFields();
    approvalDialogState = Object.freeze({
      ...approvalDialogState,
      proof: null,
    });
    byId("approval-proof-status").textContent = (
      `Approval disabled. ${errorText(error)}`
    );
    updateApprovalButton();
  }
}

async function submitApproval(event) {
  event.preventDefault();
  const state = approvalDialogState;
  const reason = byId("approval-reason").value.trim();
  if (!state || !reason || !currentApprovalStateIsActionable()) {
    updateApprovalButton();
    return;
  }
  const orderId = state.targetOrderId;
  if (
    !approvalTokenIsCurrent(state.requestToken, orderId)
    || !proofHasRequiredFields(
      state.proof,
      orderId,
      pendingOrders.get(orderId),
    )
  ) {
    updateApprovalButton();
    return;
  }
  approvalDialogState = Object.freeze({
    ...state,
    submitting: true,
  });
  poisonApprovalState();
  try {
    const result = await api(`/approve/${orderId}`, jsonPost({reason}));
    appendReceipt(
      `Order ${orderId} approval`,
      "Server accepted the human decision after its execution-time checks.",
      [
        ["Status", readable(result.status)],
        ["Executed", result.executed === true ? "Yes" : "No"],
        ["Broker order", readable(result.broker_order_id)],
      ],
      "receipt-safe",
    );
    notify(`Order ${orderId} approval returned ${readable(result.status)}.`, "notice-success");
    closeDialog(byId("approval-dialog"));
    await refreshAll();
  } catch (error) {
    notify(errorText(error), "notice-error");
    if (error.code === "approval_conflict") {
      closeDialog(byId("approval-dialog"));
      await refreshPending();
    } else {
      clearApprovalProofFields();
      byId("approval-proof-status").textContent = (
        "Approval state was cleared after the failed submission. Close and review again."
      );
    }
  }
}

function openRejection(orderId, trigger) {
  rejectionOrderId = orderId;
  byId("rejection-reason").value = "";
  showDialog(byId("rejection-dialog"), trigger);
  byId("rejection-reason").focus();
}

async function submitRejection(event) {
  event.preventDefault();
  const reason = byId("rejection-reason").value.trim();
  if (!reason || rejectionOrderId === null) {
    notify("A non-empty rejection reason is required.", "notice-error");
    return;
  }
  const orderId = rejectionOrderId;
  try {
    const result = await api(`/reject/${orderId}`, jsonPost({reason}));
    appendReceipt(
      `Order ${orderId} rejected`,
      "The human rejection was recorded.",
      [["Status", readable(result.status)]],
    );
    closeDialog(byId("rejection-dialog"));
    await refreshPending();
  } catch (error) {
    notify(errorText(error), "notice-error");
  }
}

function renderPanicReceipt(receipt, confirmedSafe) {
  const local = receipt && receipt.unsafe_local_state
    ? receipt.unsafe_local_state
    : {};
  const rows = [
    ["Safe", confirmedSafe ? "Explicitly confirmed" : "Not confirmed"],
    ["Local enumeration", readable(receipt && receipt.local_enumeration, "Unknown")],
    ["Remote enumeration", readable(receipt && receipt.remote_enumeration, "Unknown")],
    ["Confirmed canceled", listText(receipt && receipt.confirmed_canceled)],
    ["Unconfirmed local orders", listText(receipt && receipt.unconfirmed_order_ids)],
    ["Remote open orders", listText(receipt && receipt.remote_open_order_ids)],
    ["Local live or unknown", listText(local.live_or_unknown_order_ids)],
    ["Latched local orders", listText(local.latched_order_ids)],
    ["Unsafe fills", listText(local.unsafe_fill_ids)],
    ["Active rules", listText(local.active_rule_ids)],
    ["Unsafe rule groups", listText(local.unsafe_rule_group_ids)],
    ["Unknown categories", listText(local.unknown_categories)],
  ];
  appendReceipt(
    confirmedSafe ? "Panic safe receipt" : "Panic incomplete receipt",
    readable(
      receipt && receipt.message,
      confirmedSafe
        ? "Server explicitly confirmed a safe state."
        : "Safety could not be confirmed.",
    ),
    rows,
    confirmedSafe ? "receipt-safe" : "receipt-unsafe",
  );
}

function latchUnsafePanic(receipt) {
  unsafePanicLatched = true;
  const banner = byId("critical-banner");
  const localState = readable(
    receipt && receipt.local_enumeration,
    "unknown",
  );
  const remoteState = readable(
    receipt && receipt.remote_enumeration,
    "unknown",
  );
  byId("critical-banner-title").textContent = "Panic safety unconfirmed";
  byId("critical-banner-message").textContent = (
    `Panic remains incomplete. Local enumeration: ${localState}; `
    + `remote enumeration: ${remoteState}. Review the full receipt below.`
  );
  banner.hidden = false;
}

async function submitPanic(event) {
  event.preventDefault();
  const reason = byId("panic-reason").value.trim();
  if (!reason) {
    notify("A non-empty panic reason is required.", "notice-error");
    return;
  }
  closeDialog(byId("panic-dialog"));
  try {
    const receipt = await api("/panic", jsonPost({reason}));
    if (receipt && receipt.safe === true) {
      renderPanicReceipt(receipt, true);
      unsafePanicLatched = false;
      renderSafetyUnknown(
        "Refreshing persisted local safety state after the broker-verified receipt.",
      );
      notify("Panic receipt explicitly confirms a safe state.", "notice-success");
    } else {
      renderPanicReceipt(receipt, false);
      latchUnsafePanic(receipt);
      notify("Panic response did not explicitly confirm safety.", "notice-error");
    }
  } catch (error) {
    const receipt = error && error.body ? error.body.receipt : null;
    renderPanicReceipt(receipt, false);
    latchUnsafePanic(receipt);
    notify(`Panic is incomplete. ${errorText(error)}`, "notice-error");
  }
  await refreshAll();
}

function selectedBreakerProof() {
  const scope = byId("breaker-scope").value;
  if (
    !breakerScopes.includes(scope)
    || !latestHealth
    || latestHealth.requestToken !== healthRequestSequence
    || Date.now() - latestHealth.receivedAt > HEALTH_OBSERVATION_MAX_AGE_MS
  ) {
    return null;
  }
  const health = latestHealth.payload;
  const generation = health.killswitch_generation[scope];
  const tripped = health.killswitch[scope] === true;
  const healthComplete = (
    health.db_ok === true
    && health.daemon_alive === true
  );
  if (
    !tripped
    || !Number.isSafeInteger(generation)
    || generation <= 0
    || !healthComplete
  ) {
    return null;
  }
  return {
    scope,
    generation,
    requestToken: latestHealth.requestToken,
  };
}

function updateBreakerReset() {
  const scope = byId("breaker-scope").value;
  const health = latestHealth
    && latestHealth.requestToken === healthRequestSequence
    ? latestHealth.payload
    : null;
  const generation = health
    ? health.killswitch_generation[scope]
    : null;
  byId("breaker-generation").textContent = (
    Number.isSafeInteger(generation) && generation > 0
      ? String(generation)
      : "Unknown"
  );
  const healthComplete = Boolean(
    health
    && health.db_ok === true
    && health.daemon_alive === true
  );
  byId("breaker-health").textContent = (
    healthComplete ? "Observed healthy" : "Unconfirmed"
  );
  const reason = byId("breaker-reset-reason").value.trim();
  byId("breaker-reset-button").disabled = !(
    !breakerResetInFlight && selectedBreakerProof() && reason
  );
}

function renderUnknownHealth() {
  latestHealth = null;
  byId("truth-broker").textContent = "Unknown";
  byId("truth-mode").textContent = "Unknown";
  setState(byId("truth-database"), "Unknown", "caution");
  setState(byId("truth-daemon"), "Unknown", "caution");
  setState(byId("truth-safety"), "Unknown", "caution");
  breakerScopes.forEach((scope) => {
    setState(
      byId(`truth-${scope}-breaker`),
      "Unknown",
      "caution",
    );
  });
  renderSafetyUnknown();
  updateBreakerReset();
}

function renderSafetyUnknown(message) {
  const banner = byId("critical-banner");
  byId("critical-banner-title").textContent = "Safety state unverified";
  byId("critical-banner-message").textContent = (
    message
    || "Complete persisted local safety evidence is unavailable; broker open orders are unverified."
  );
  banner.hidden = false;
}

function invalidateHealthObservation() {
  healthRequestSequence += 1;
  renderUnknownHealth();
}

function isCanonicalIdList(value) {
  return (
    Array.isArray(value)
    && value.every(
      (item) => Number.isSafeInteger(item) && item > 0,
    )
  );
}

function breakerScopeIsCanonical(breaker) {
  if (
    ["loss", "drawdown", "data", "liquidity"].includes(
      breaker.kind,
    )
  ) {
    return (
      breaker.target.length > 0
      && breaker.scope === `${breaker.kind}:${breaker.target}`
    );
  }
  if (
    ["broker_drift", "operator_global"].includes(
      breaker.kind,
    )
  ) {
    return (
      breaker.target === ""
      && breaker.scope === breaker.kind
    );
  }
  return false;
}

function safetyPayloadIsComplete(health) {
  const safety = health.safety;
  if (
    !safety
    || typeof safety !== "object"
    || safety.complete !== true
    || !["unsafe", "locally_clear"].includes(safety.state)
    || safety.local_enumeration !== "confirmed"
    || safety.remote_broker_open_orders !== "unverified"
    || !Array.isArray(safety.active_breakers)
    || !Array.isArray(safety.unknown_categories)
    || safety.unknown_categories.length !== 0
    || !safety.operator_global_breaker
    || typeof safety.operator_global_breaker !== "object"
    || typeof safety.operator_global_breaker.tripped !== "boolean"
    || !Number.isSafeInteger(
      safety.operator_global_breaker.generation,
    )
    || safety.operator_global_breaker.generation < 0
    || !safety.unsafe_local_state
    || typeof safety.unsafe_local_state !== "object"
    || !Array.isArray(
      safety.unsafe_local_state.unknown_categories,
    )
    || safety.unsafe_local_state.unknown_categories.length !== 0
  ) {
    return false;
  }

  for (const field of unsafeLocalIdFields) {
    if (!isCanonicalIdList(safety.unsafe_local_state[field])) {
      return false;
    }
  }

  const activeScopes = new Map();
  for (const breaker of safety.active_breakers) {
    if (
      !breaker
      || typeof breaker !== "object"
      || typeof breaker.scope !== "string"
      || !breaker.scope
      || typeof breaker.kind !== "string"
      || !breaker.kind
      || typeof breaker.target !== "string"
      || !Number.isSafeInteger(breaker.generation)
      || breaker.generation <= 0
      || activeScopes.has(breaker.scope)
      || !breakerScopeIsCanonical(breaker)
    ) {
      return false;
    }
    activeScopes.set(breaker.scope, breaker.generation);
  }

  const globalBreaker = safety.operator_global_breaker;
  const activeGlobalGeneration = activeScopes.get("operator_global");
  if (
    (
      globalBreaker.tripped
      && (
        globalBreaker.generation <= 0
        || activeGlobalGeneration !== globalBreaker.generation
      )
    )
    || (
      !globalBreaker.tripped
      && activeGlobalGeneration !== undefined
    )
  ) {
    return false;
  }

  for (const scope of breakerScopes) {
    const activeGeneration = activeScopes.get(`loss:${scope}`);
    if (
      health.killswitch[scope] !== (
        activeGeneration !== undefined
      )
      || (
        activeGeneration !== undefined
        && health.killswitch_generation[scope]
          !== activeGeneration
      )
    ) {
      return false;
    }
  }

  const unsafeLocal = unsafeLocalIdFields.some(
    (field) => safety.unsafe_local_state[field].length > 0,
  );
  const knownUnsafe = activeScopes.size > 0 || unsafeLocal;
  return safety.state === (
    knownUnsafe ? "unsafe" : "locally_clear"
  );
}

function healthPayloadIsComplete(health) {
  if (
    !health
    || health.broker !== "Alpaca"
    || health.mode !== "paper"
    || health.db_ok !== true
    || typeof health.daemon_alive !== "boolean"
    || !health.killswitch
    || typeof health.killswitch !== "object"
    || !health.killswitch_generation
    || typeof health.killswitch_generation !== "object"
  ) {
    return false;
  }
  if (
    health.daemon_alive === true
    && (
      typeof health.heartbeat_age_seconds !== "number"
      || !Number.isFinite(health.heartbeat_age_seconds)
      || health.heartbeat_age_seconds < 0
    )
  ) {
    return false;
  }
  for (const scope of breakerScopes) {
    if (
      typeof health.killswitch[scope] !== "boolean"
      || !Number.isSafeInteger(health.killswitch_generation[scope])
      || health.killswitch_generation[scope] < 0
    ) {
      return false;
    }
  }
  if (typeof health.observed_at !== "string") {
    return false;
  }
  const observedAt = Date.parse(health.observed_at);
  const age = Date.now() - observedAt;
  if (
    !Number.isFinite(observedAt)
    || age < -5000
    || age > HEALTH_OBSERVATION_MAX_AGE_MS
  ) {
    return false;
  }
  return safetyPayloadIsComplete(health);
}

function renderSafetyTruth(safety) {
  if (safety.state === "locally_clear") {
    setState(
      byId("truth-safety"),
      "Locally clear · broker open orders unverified",
      "caution",
    );
    if (!unsafePanicLatched) {
      byId("critical-banner").hidden = true;
    }
    return;
  }

  setState(
    byId("truth-safety"),
    "Unsafe · persisted local evidence",
    "alarm",
  );
  const evidence = [];
  const activeScopes = safety.active_breakers.map(
    (breaker) => breaker.scope,
  );
  if (activeScopes.length) {
    evidence.push(`active breakers: ${activeScopes.join(", ")}`);
  }
  const categoryLabels = {
    live_or_unknown_order_ids: "live or unknown orders",
    latched_order_ids: "latched orders",
    unsafe_fill_ids: "unsafe fills",
    active_rule_ids: "active rules",
    unsafe_rule_group_ids: "unsafe rule groups",
  };
  unsafeLocalIdFields.forEach((field) => {
    const ids = safety.unsafe_local_state[field];
    if (ids.length) {
      evidence.push(`${categoryLabels[field]}: ${ids.join(", ")}`);
    }
  });
  byId("critical-banner-title").textContent = (
    "Persisted safety warning"
  );
  byId("critical-banner-message").textContent = (
    `Local safety is unsafe (${evidence.join("; ")}). `
    + "Broker open orders remain unverified after reload."
  );
  byId("critical-banner").hidden = false;
}

function renderHealth(health) {
  byId("truth-broker").textContent = health.broker;
  byId("truth-mode").textContent = health.mode;
  setState(byId("truth-database"), "Available", "verified");
  if (health.daemon_alive === true) {
    setState(
      byId("truth-daemon"),
      `Fresh · ${readable(health.heartbeat_age_seconds)}s`,
      "verified",
    );
  } else {
    setState(byId("truth-daemon"), "Stale or absent", "caution");
  }
  breakerScopes.forEach((scope) => {
    const tripped = health.killswitch[scope];
    const generation = health.killswitch_generation[scope];
    setState(
      byId(`truth-${scope}-breaker`),
      `${tripped ? "Tripped" : "Clear"} · gen ${generation}`,
      tripped ? "alarm" : "verified",
    );
  });
  renderSafetyTruth(health.safety);
  updateBreakerReset();
}

async function submitBreakerReset(event) {
  event.preventDefault();
  const proof = selectedBreakerProof();
  const reason = byId("breaker-reset-reason").value.trim();
  if (
    !proof
    || !reason
    || breakerResetInFlight
    || proof.requestToken !== healthRequestSequence
  ) {
    notify(
      "Reset requires a tripped scope, positive generation, healthy server, and reason.",
      "notice-error",
    );
    updateBreakerReset();
    return;
  }
  breakerResetInFlight = true;
  invalidateHealthObservation();
  try {
    const result = await api("/killswitch/reset", jsonPost({
      asset_class: proof.scope,
      reason,
      expected_generation: proof.generation,
    }));
    appendReceipt(
      `${proof.scope} breaker reset`,
      "The server accepted the scoped reset after fresh health checks.",
      [
        ["Scope", readable(result.asset_class)],
        ["Observed generation", String(proof.generation)],
        ["Result generation", readable(result.generation)],
        ["Tripped", result.tripped === true ? "Yes" : "No"],
      ],
      "receipt-safe",
    );
    byId("breaker-reset-reason").value = "";
    await refreshHealth();
  } catch (error) {
    renderUnknownHealth();
    notify(errorText(error), "notice-error");
  } finally {
    breakerResetInFlight = false;
    updateBreakerReset();
  }
}

async function refreshHealth() {
  const requestToken = ++healthRequestSequence;
  renderUnknownHealth();
  let health;
  try {
    health = await api("/health");
  } catch (error) {
    if (requestToken !== healthRequestSequence) {
      return false;
    }
    renderUnknownHealth();
    throw error;
  }
  if (requestToken !== healthRequestSequence) {
    return false;
  }
  if (!healthPayloadIsComplete(health)) {
    renderUnknownHealth();
    return false;
  }
  latestHealth = Object.freeze({
    requestToken,
    receivedAt: Date.now(),
    payload: Object.freeze(health),
  });
  renderHealth(health);
  return true;
}

async function refreshPositions() {
  const payload = await api("/positions");
  const target = byId("positions");
  clear(target);
  const positions = Array.isArray(payload.positions) ? payload.positions : [];
  if (!positions.length) {
    target.appendChild(node("p", "No open positions.", "empty-state"));
    return;
  }
  target.appendChild(makeTable(
    ["Symbol", "Quantity", "Average", "Last", "Value"],
    positions.map((position) => [
      position.ticker,
      position.qty,
      position.avg_entry_price,
      position.current_price,
      position.market_value,
    ]),
  ));
}

async function refreshHoldings() {
  const payload = await api("/holdings");
  const target = byId("holdings");
  clear(target);
  byId("external-stale").textContent = (
    payload.external_stale === true
      ? "External data is stale."
      : payload.external_available === false
        ? "No external source is enabled."
        : "External data is current."
  );
  const holdings = [
    ...(Array.isArray(payload.alpaca) ? payload.alpaca : []),
    ...(Array.isArray(payload.external) ? payload.external : []),
  ];
  if (!holdings.length) {
    target.appendChild(node("p", "No holdings reported.", "empty-state"));
    return;
  }
  const totals = payload.combined_by_ticker || {};
  target.appendChild(makeTable(
    ["Symbol", "Source", "Quantity", "Value", "Combined"],
    holdings.map((holding) => [
      holding.ticker,
      `${holding.source}${holding.read_only ? " · read-only" : ""}`,
      holding.qty,
      holding.market_value !== undefined
        ? holding.market_value
        : holding.current_value,
      totals[holding.ticker],
    ]),
  ));
}

async function refreshRiskLog() {
  const payload = await api("/log");
  const target = byId("risk-log");
  clear(target);
  const events = Array.isArray(payload.risk_events)
    ? payload.risk_events
    : [];
  if (!events.length) {
    target.appendChild(node("p", "No risk events reported.", "empty-state"));
    return;
  }
  events.forEach((event) => {
    const row = node("article", null, "event-row");
    row.appendChild(node(
      "div",
      `${readable(event.at)} · ${readable(event.type)}`,
      "event-time",
    ));
    row.appendChild(node("div", readable(event.reason), "event-reason"));
    target.appendChild(row);
  });
}

function appendChat(who, message) {
  const row = node("article", null, "chat-message");
  row.appendChild(node("div", who, "message-who"));
  row.appendChild(node("div", message, "message-text"));
  byId("chat-log").appendChild(row);
  byId("chat-log").scrollTop = byId("chat-log").scrollHeight;
}

async function submitChat(event) {
  event.preventDefault();
  const input = byId("chat-input");
  const message = input.value.trim();
  if (!message) {
    return;
  }
  input.value = "";
  appendChat("Operator", message);
  try {
    const result = await api("/chat", jsonPost({message}));
    (Array.isArray(result.tool_calls) ? result.tool_calls : []).forEach((call) => {
      appendChat(
        "Tool proposal",
        `${readable(call.name)} · ${JSON.stringify(call.input || {})}`,
      );
    });
    appendChat("Assistant", readable(result.reply, "No reply returned."));
    await refreshPending();
  } catch (error) {
    appendChat("System", errorText(error));
  }
}

async function refreshAll() {
  const jobs = [
    refreshHealth(),
    refreshPending(),
    refreshPositions(),
    refreshHoldings(),
    refreshRiskLog(),
  ];
  const results = await Promise.allSettled(jobs);
  const failed = results.find((result) => result.status === "rejected");
  if (failed) {
    notify(errorText(failed.reason), "notice-error");
  }
  if (unsafePanicLatched) {
    byId("critical-banner").hidden = false;
  }
}

async function initialize() {
  try {
    const session = await loadSession();
    byId("session-actor").textContent = readable(session.actor);
    byId("truth-operator").textContent = readable(session.actor);
  } catch (_error) {
    return;
  }

  [
    byId("approval-dialog"),
    byId("rejection-dialog"),
    byId("panic-dialog"),
  ].forEach(bindDialogReturnFocus);
  byId("sign-out").addEventListener("click", async () => {
    try {
      await logout();
    } catch (error) {
      notify(errorText(error), "notice-error");
    }
  });
  byId("refresh-console").addEventListener("click", refreshAll);
  byId("approval-form").addEventListener("submit", submitApproval);
  byId("approval-reason").addEventListener("input", updateApprovalButton);
  byId("approval-cancel").addEventListener(
    "click",
    () => closeDialog(byId("approval-dialog")),
  );
  byId("rejection-form").addEventListener("submit", submitRejection);
  byId("rejection-cancel").addEventListener(
    "click",
    () => closeDialog(byId("rejection-dialog")),
  );
  byId("panic-open").addEventListener("click", (event) => {
    byId("panic-reason").value = "";
    showDialog(byId("panic-dialog"), event.currentTarget);
    byId("panic-reason").focus();
  });
  byId("panic-form").addEventListener("submit", submitPanic);
  byId("panic-cancel").addEventListener(
    "click",
    () => closeDialog(byId("panic-dialog")),
  );
  byId("breaker-reset-form").addEventListener("submit", submitBreakerReset);
  byId("breaker-scope").addEventListener("change", updateBreakerReset);
  byId("breaker-reset-reason").addEventListener("input", updateBreakerReset);
  byId("chat-form").addEventListener("submit", submitChat);

  await refreshAll();
  window.setInterval(refreshAll, 10000);
}

initialize();
