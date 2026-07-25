"use strict";

import {
  api,
  jsonPost,
  loadSession,
  logout,
} from "/static/js/auth.js";

const byId = (id) => document.getElementById(id);
const breakerScopes = ["equity", "crypto"];
const dialogReturnFocus = new Map();

let latestHealth = null;
let approvalOrderId = null;
let approvalProof = null;
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
  dialog.addEventListener("close", () => {
    const target = dialogReturnFocus.get(dialog);
    dialogReturnFocus.delete(dialog);
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
  const payload = await api("/pending");
  const list = byId("pending-list");
  clear(list);
  const pending = Array.isArray(payload.pending) ? payload.pending : [];
  if (!pending.length) {
    list.appendChild(node("li", "No pending proposals.", "empty-state"));
    return;
  }
  pending.forEach((order) => {
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

function proofHasRequiredFields(proof) {
  if (!proof || proof.complete !== true || !proof.order || !proof.exposure) {
    return false;
  }
  const order = proof.order;
  const exposure = proof.exposure;
  const exactAmount = (
    (typeof order.quantity === "string" && order.quantity)
    || (typeof order.notional === "string" && order.notional)
  );
  return Boolean(
    typeof proof.broker === "string"
    && proof.broker
    && typeof proof.mode === "string"
    && proof.mode
    && typeof proof.expires_at === "string"
    && proof.expires_at
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

function renderApprovalProof(proof) {
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

  const complete = proofHasRequiredFields(proof);
  const status = byId("approval-proof-status");
  if (complete) {
    status.textContent = "Complete server proof received. Enter a reason to enable approval.";
    status.className = "proof-status proof-status-complete";
    approvalProof = proof;
  } else {
    const missing = proof && Array.isArray(proof.missing_proof)
      ? listText(proof.missing_proof)
      : "required proof fields";
    status.textContent = `Approval disabled. Refresh missing proof: ${missing}.`;
    status.className = "proof-status";
    approvalProof = null;
  }
  updateApprovalButton();
}

function updateApprovalButton() {
  const reason = byId("approval-reason").value.trim();
  byId("approval-confirm-button").disabled = !(approvalProof && reason);
}

async function openApproval(orderId, trigger) {
  approvalOrderId = orderId;
  approvalProof = null;
  byId("approval-reason").value = "";
  byId("approval-confirm-button").disabled = true;
  renderApprovalProof(null);
  const dialog = byId("approval-dialog");
  showDialog(dialog, trigger);
  try {
    const proof = await api(`/pending/${orderId}/confirmation`);
    renderApprovalProof(proof);
    byId("approval-reason").focus();
  } catch (error) {
    renderApprovalProof(null);
    byId("approval-proof-status").textContent = (
      `Approval disabled. ${errorText(error)}`
    );
  }
}

async function submitApproval(event) {
  event.preventDefault();
  const reason = byId("approval-reason").value.trim();
  if (!approvalProof || !reason || approvalOrderId === null) {
    updateApprovalButton();
    return;
  }
  const orderId = approvalOrderId;
  byId("approval-confirm-button").disabled = true;
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
      updateApprovalButton();
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
      byId("critical-banner").hidden = true;
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
  if (!breakerScopes.includes(scope) || !latestHealth) {
    return null;
  }
  const generation = latestHealth.killswitch_generation
    ? latestHealth.killswitch_generation[scope]
    : null;
  const tripped = latestHealth.killswitch
    ? latestHealth.killswitch[scope] === true
    : false;
  const healthComplete = (
    latestHealth.db_ok === true
    && latestHealth.daemon_alive === true
  );
  if (
    !tripped
    || !Number.isInteger(generation)
    || generation <= 0
    || !healthComplete
  ) {
    return null;
  }
  return {scope, generation};
}

function updateBreakerReset() {
  const scope = byId("breaker-scope").value;
  const generation = latestHealth && latestHealth.killswitch_generation
    ? latestHealth.killswitch_generation[scope]
    : null;
  byId("breaker-generation").textContent = (
    Number.isInteger(generation) && generation > 0
      ? String(generation)
      : "None"
  );
  const healthComplete = Boolean(
    latestHealth
    && latestHealth.db_ok === true
    && latestHealth.daemon_alive === true
  );
  byId("breaker-health").textContent = (
    healthComplete ? "Observed healthy" : "Incomplete"
  );
  const reason = byId("breaker-reset-reason").value.trim();
  byId("breaker-reset-button").disabled = !(
    selectedBreakerProof() && reason
  );
}

async function submitBreakerReset(event) {
  event.preventDefault();
  const proof = selectedBreakerProof();
  const reason = byId("breaker-reset-reason").value.trim();
  if (!proof || !reason) {
    notify(
      "Reset requires a tripped scope, positive generation, healthy server, and reason.",
      "notice-error",
    );
    updateBreakerReset();
    return;
  }
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
    notify(errorText(error), "notice-error");
  }
  updateBreakerReset();
}

async function refreshHealth() {
  const health = await api("/health");
  latestHealth = health;
  byId("truth-broker").textContent = readable(health.broker);
  byId("truth-mode").textContent = readable(health.mode);
  if (health.db_ok === true) {
    setState(byId("truth-database"), "Available", "verified");
  } else {
    setState(byId("truth-database"), "Unavailable", "alarm");
  }
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
    const tripped = health.killswitch && health.killswitch[scope] === true;
    const generation = health.killswitch_generation
      ? health.killswitch_generation[scope]
      : null;
    setState(
      byId(`truth-${scope}-breaker`),
      `${tripped ? "Tripped" : "Clear"} · gen ${readable(generation, "none")}`,
      tripped ? "alarm" : "verified",
    );
  });
  updateBreakerReset();
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
