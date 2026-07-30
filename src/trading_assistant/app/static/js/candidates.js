"use strict";

const TERMINAL_CODES = new Set([
  "candidate_expired",
  "candidate_replayed",
  "candidate_risk_rejected",
  "candidate_quote_stale",
  "candidate_signature_invalid",
  "candidate_kind_mismatch",
  "candidate_actor_mismatch",
  "candidate_session_mismatch",
  "candidate_issued_in_future",
  "candidate_receipt_missing",
  "candidate_receipt_inconsistent",
  "candidate_receipt_invalid",
  "candidate_target_conflict",
]);

function isRecord(value) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
  );
}

function nonblank(value) {
  return typeof value === "string" && Boolean(value.trim());
}

function awareTimestamp(value) {
  return (
    nonblank(value)
    && /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    && Number.isFinite(Date.parse(value))
  );
}

function validAmount(value) {
  return value === null || (
    typeof value === "string"
    && /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)
    && Number(value) > 0
  );
}

function validOrderShape(payload) {
  if (
    !isRecord(payload)
    || !nonblank(payload.ticker)
    || !["buy", "sell"].includes(payload.side)
    || !["market", "limit"].includes(payload.order_type)
    || !validAmount(payload.quantity)
    || !validAmount(payload.notional)
    || !validAmount(payload.limit_price)
    || !validAmount(payload.reference_price)
    || !awareTimestamp(payload.quote_as_of)
    || !nonblank(payload.thesis)
    || ((payload.quantity === null) === (payload.notional === null))
    || ((payload.order_type === "limit") !== (payload.limit_price !== null))
  ) {
    return false;
  }
  return true;
}

function validRuleShape(payload) {
  return Boolean(
    isRecord(payload)
    && nonblank(payload.ticker)
    && isRecord(payload.condition)
    && ["price_below", "price_above"].includes(payload.condition.comparator)
    && validAmount(payload.condition.trigger_price)
    && isRecord(payload.action)
    && validOrderShape({
      ticker: payload.ticker,
      ...payload.action,
      reference_price: payload.reference_price,
      quote_as_of: payload.quote_as_of,
      thesis: payload.thesis,
    })
  );
}

export function candidateIsValid(candidate) {
  return Boolean(
    isRecord(candidate)
    && candidate.version === 1
    && ["order", "rule"].includes(candidate.kind)
    && nonblank(candidate.actor)
    && nonblank(candidate.session_binding)
    && awareTimestamp(candidate.issued_at)
    && awareTimestamp(candidate.expires_at)
    && Date.parse(candidate.expires_at) > Date.parse(candidate.issued_at)
    && nonblank(candidate.nonce)
    && nonblank(candidate.signature)
    && (
      candidate.kind === "order"
        ? validOrderShape(candidate.payload)
        : validRuleShape(candidate.payload)
    )
  );
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== undefined && text !== null) {
    element.textContent = String(text);
  }
  return element;
}

function addField(list, label, value) {
  const field = node("div", null, "candidate-fact");
  field.appendChild(node("dt", label));
  field.appendChild(node("dd", value === null ? "None" : value));
  list.appendChild(field);
}

function renderPayloadFields(candidate, list) {
  const payload = candidate.payload;
  addField(list, "Symbol", payload.ticker);
  if (candidate.kind === "order") {
    addField(list, "Side", payload.side);
    addField(list, "Quantity", payload.quantity);
    addField(list, "Notional", payload.notional);
    addField(list, "Type", payload.order_type);
    addField(list, "Limit", payload.limit_price);
  } else {
    addField(list, "Condition", payload.condition.comparator);
    addField(list, "Trigger", payload.condition.trigger_price);
    addField(list, "Side", payload.action.side);
    addField(list, "Quantity", payload.action.quantity);
    addField(list, "Notional", payload.action.notional);
    addField(list, "Type", payload.action.order_type);
    addField(list, "Limit", payload.action.limit_price);
  }
  addField(list, "Reference", payload.reference_price);
  addField(list, "Quote observed", payload.quote_as_of);
  addField(list, "Issued", candidate.issued_at);
  addField(list, "Expires", candidate.expires_at);
  addField(list, "Actor", candidate.actor);
}

function errorRequestId(error) {
  return nonblank(error?.requestId) ? ` Request ${error.requestId}.` : "";
}

function rateFailureText(error) {
  const parts = ["Candidate queue rate limited."];
  if (Number.isSafeInteger(error?.retryAfter) && error.retryAfter >= 0) {
    parts.push(`Retry after ${error.retryAfter} seconds.`);
  }
  if (
    Number.isSafeInteger(error?.rateLimitReset)
    && error.rateLimitReset >= 0
  ) {
    parts.push(
      `Rate limit resets ${new Date(error.rateLimitReset * 1000).toISOString()}.`,
    );
  }
  if (nonblank(error?.requestId)) {
    parts.push(`Request ${error.requestId}.`);
  }
  return parts.join(" ");
}

function queueSuccessText(candidate, result) {
  const title = candidate.kind === "order"
    ? "Proposal queued — not executed"
    : "Rule queued — not executed";
  return `${title}. Target ${result.target_id}. Executed ${String(result.executed)}.`;
}

function validQueueReceipt(candidate, result) {
  const expectedStatus = candidate.kind === "order" ? "proposed" : "queued";
  return Boolean(
    isRecord(result)
    && result.status === expectedStatus
    && Number.isSafeInteger(result.target_id)
    && result.target_id > 0
    && result.executed === false
  );
}

function invalidReceiptError() {
  const error = new Error("Candidate queue receipt was invalid");
  error.code = "candidate_receipt_invalid";
  return error;
}

export async function queueCandidate(
  candidate,
  reason,
  {api, jsonPost},
) {
  if (!candidateIsValid(candidate)) {
    throw new TypeError("Candidate envelope is invalid");
  }
  const normalizedReason = typeof reason === "string" ? reason.trim() : "";
  if (!normalizedReason) {
    throw new TypeError("Operator reason is required");
  }
  if (typeof api !== "function" || typeof jsonPost !== "function") {
    throw new TypeError("Candidate queue transport is unavailable");
  }
  const path = candidate.kind === "order"
    ? "/candidates/order/queue"
    : "/candidates/rule/queue";
  return api(path, jsonPost({
    candidate,
    reason: normalizedReason,
  }));
}

function renderCandidateCard(candidate, index, options) {
  const card = node("article", null, "candidate-card");
  card.appendChild(node(
    "h4",
    `${candidate.kind === "order" ? "Order" : "Rule"} candidate · ${candidate.payload.ticker}`,
  ));
  card.appendChild(node(
    "p",
    "Immutable signed envelope. Queueing cannot execute it.",
    "candidate-boundary-copy",
  ));
  const fields = node("dl", null, "candidate-facts");
  renderPayloadFields(candidate, fields);
  card.appendChild(fields);
  card.appendChild(node("p", candidate.payload.thesis, "candidate-thesis"));

  const form = node("form", null, "candidate-form");
  const reasonId = `candidate-${index}-reason`;
  const label = node("label", "Operator reason");
  label.htmlFor = reasonId;
  const reason = node("textarea");
  reason.id = reasonId;
  reason.name = "reason";
  reason.required = true;
  reason.rows = 3;
  const button = node(
    "button",
    candidate.kind === "order"
      ? "Queue proposal"
      : "Queue rule",
    "button-primary",
  );
  button.type = "button";
  form.appendChild(label);
  form.appendChild(reason);
  form.appendChild(button);
  card.appendChild(form);
  const status = node(
    "p",
    "Not queued.",
    "candidate-status is-unknown",
  );
  card.appendChild(status);

  if (Date.parse(candidate.expires_at) <= options.now()) {
    button.disabled = true;
    reason.disabled = true;
    status.className = "candidate-status is-blocked";
    status.textContent = "Candidate expired. Request a new signed candidate.";
    return card;
  }

  button.addEventListener("click", async (event) => {
    event.preventDefault();
    const operatorReason = reason.value.trim();
    if (!operatorReason) {
      status.className = "candidate-status is-caution";
      status.textContent = "Enter a nonblank operator reason before queueing.";
      reason.focus?.();
      return;
    }
    button.disabled = true;
    status.className = "candidate-status is-caution";
    status.textContent = "Queue request in progress. Nothing has executed.";
    try {
      const result = await options.queue(candidate, operatorReason);
      if (!validQueueReceipt(candidate, result)) {
        throw invalidReceiptError();
      }
      reason.disabled = true;
      status.className = "candidate-status is-verified";
      status.textContent = queueSuccessText(candidate, result);
      if (typeof options.receiptHandler === "function") {
        options.receiptHandler(candidate, result);
      }
    } catch (error) {
      const terminal = TERMINAL_CODES.has(error?.code);
      const rateLimited = error?.status === 429 || error?.code === "rate_limited";
      status.className = "candidate-status is-blocked";
      if (rateLimited) {
        status.textContent = rateFailureText(error);
      } else {
        const message = nonblank(error?.message)
          ? error.message
          : "Candidate queue request failed";
        status.textContent = `${message}.${errorRequestId(error)}`;
      }
      if (terminal || rateLimited) {
        reason.disabled = true;
        button.disabled = true;
      } else {
        button.disabled = false;
      }
    }
  });
  return card;
}

export function renderCandidates(
  target,
  candidates,
  {
    now = () => Date.now(),
    queue,
    receiptHandler = null,
  } = {},
) {
  if (!target) {
    return;
  }
  target.textContent = "";
  if (!Array.isArray(candidates) || candidates.length === 0) {
    target.appendChild(node(
      "p",
      "No signed candidates in this page session.",
      "empty-state",
    ));
    return;
  }
  candidates.forEach((candidate, index) => {
    if (!candidateIsValid(candidate)) {
      const invalid = node("article", null, "candidate-card has-error");
      invalid.appendChild(node("h4", "Invalid signed candidate"));
      invalid.appendChild(node(
        "p",
        "This candidate is not actionable. Request a new signed candidate.",
        "candidate-status is-blocked",
      ));
      target.appendChild(invalid);
      return;
    }
    target.appendChild(renderCandidateCard(candidate, index, {
      now,
      queue,
      receiptHandler,
    }));
  });
}
