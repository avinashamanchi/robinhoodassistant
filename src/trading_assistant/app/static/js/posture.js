"use strict";

const PROVIDER_FIELDS = Object.freeze([
  "budget_used",
  "budget_remaining",
  "budget_limit",
  "input_tokens_used",
  "input_tokens_remaining",
  "input_tokens_limit",
  "output_tokens_used",
  "output_tokens_remaining",
  "output_tokens_limit",
]);

const ENVIRONMENT_IDS = Object.freeze([
  "environment-breaker",
  "environment-daemon",
  "environment-reconciliation",
  "environment-quote",
]);

function byId(root, id) {
  if (!root || typeof root.getElementById !== "function") {
    return null;
  }
  return root.getElementById(id);
}

function isRecord(value) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
  );
}

function isNonblankString(value) {
  return typeof value === "string" && Boolean(value.trim());
}

function parseObservedAt(value) {
  if (
    !isNonblankString(value)
    || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
  ) {
    return null;
  }
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function exactNonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function normalizeCheck(check) {
  if (
    !isRecord(check)
    || !isNonblankString(check.name)
    || !isNonblankString(check.status)
    || !isNonblankString(check.detail_code)
    || parseObservedAt(check.observed_at) === null
    || (
      check.scope !== undefined
      && check.scope !== null
      && !isNonblankString(check.scope)
    )
  ) {
    return null;
  }

  for (const field of PROVIDER_FIELDS) {
    if (
      check[field] !== undefined
      && check[field] !== null
      && !exactNonnegativeInteger(check[field])
    ) {
      return null;
    }
  }

  return Object.freeze({...check});
}

function unavailablePosture(reason = "Security posture is unavailable.") {
  return Object.freeze({
    valid: false,
    canTrade: false,
    reason,
    observedAt: null,
    ageSeconds: null,
    checks: Object.freeze([]),
    provider: Object.freeze({
      blocked: true,
      reason: "Provider call blocked before network I/O",
      checks: Object.freeze([]),
    }),
  });
}

function providerCheckIsComplete(check) {
  return (
    PROVIDER_FIELDS.every((field) => exactNonnegativeInteger(check[field]))
    && parseObservedAt(check.reset_at) !== null
  );
}

export function normalizePosture(payload, now = Date.now()) {
  if (
    !isRecord(payload)
    || payload.can_trade !== false
    || !Array.isArray(payload.checks)
  ) {
    return unavailablePosture();
  }
  const observedAt = parseObservedAt(payload.observed_at);
  if (observedAt === null) {
    return unavailablePosture("Security posture observation time is Unknown.");
  }

  const checks = payload.checks.map(normalizeCheck);
  if (checks.some((check) => check === null)) {
    return unavailablePosture("Security posture response was invalid.");
  }

  const providerChecks = checks.filter(
    (check) => check.name === "provider_budget",
  );
  const providerBlocked = (
    providerChecks.length === 0
    || providerChecks.some((check) => (
      check.status !== "pass"
      || !providerCheckIsComplete(check)
      || check.budget_remaining === 0
      || check.input_tokens_remaining === 0
      || check.output_tokens_remaining === 0
    ))
  );
  const currentTime = Number.isFinite(now) ? now : Date.now();

  return Object.freeze({
    valid: true,
    canTrade: false,
    reason: "",
    observedAt: payload.observed_at,
    ageSeconds: Math.max(0, Math.floor((currentTime - observedAt) / 1000)),
    checks: Object.freeze(checks),
    provider: Object.freeze({
      blocked: providerBlocked,
      reason: providerBlocked
        ? "Provider call blocked before network I/O"
        : "Provider budget evidence is available.",
      checks: Object.freeze(providerChecks),
    }),
  });
}

function statusClass(check) {
  if (check.name === "runtime_tenure") {
    if (check.status === "held") {
      return "is-verified";
    }
    if (check.status === "fenced") {
      return "is-blocked";
    }
    return "is-caution";
  }
  if (["pass", "paper", "clear", "fresh", "disabled"].includes(check.status)) {
    return "is-verified";
  }
  if (check.status === "stale") {
    return "is-stale";
  }
  if (["blocked", "tripped", "enabled", "present", "held", "fenced"].includes(check.status)) {
    return "is-blocked";
  }
  return "is-unknown";
}

function readableName(value) {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function setElementState(element, value, state = "is-unknown") {
  if (!element) {
    return;
  }
  element.textContent = value;
  element.className = state;
}

function setMeter(element, value, max, label, state = "is-unknown") {
  if (!element) {
    return;
  }
  element.textContent = "";
  element.max = max;
  element.value = value;
  element.className = state;
  if (typeof element.setAttribute === "function") {
    element.setAttribute("aria-valuetext", label);
  }
}

function budgetSummary(checks, field, limitField) {
  return checks
    .map((check) => (
      `${check.scope}: ${check[field]} / ${check[limitField]}`
    ))
    .join(" · ");
}

function mostConstrainedRatio(checks, field, limitField) {
  return Math.min(...checks.map((check) => {
    const limit = check[limitField];
    if (limit === 0) {
      return 0;
    }
    return check[field] / limit;
  }));
}

function renderBudget(normalized, root) {
  const checks = normalized.provider.checks;
  if (
    checks.length === 0
    || checks.some((check) => !providerCheckIsComplete(check))
  ) {
    [
      "provider-budget-calls",
      "provider-budget-input",
      "provider-budget-output",
      "provider-budget-reset",
    ].forEach((id) => {
      setElementState(byId(root, id), "Unknown", "is-blocked");
    });
    [
      "provider-budget-calls-meter",
      "provider-budget-input-meter",
      "provider-budget-output-meter",
    ].forEach((id) => {
      setMeter(byId(root, id), 0, 1, "Unavailable", "is-unknown");
    });
    return;
  }

  const budgetState = normalized.provider.blocked
    ? "is-blocked"
    : "is-verified";
  const metrics = [
    ["budget_remaining", "budget_limit", "provider-budget-calls", "provider-budget-calls-meter"],
    ["input_tokens_remaining", "input_tokens_limit", "provider-budget-input", "provider-budget-input-meter"],
    ["output_tokens_remaining", "output_tokens_limit", "provider-budget-output", "provider-budget-output-meter"],
  ];
  metrics.forEach(([field, limitField, valueId, meterId]) => {
    const summary = budgetSummary(checks, field, limitField);
    setElementState(byId(root, valueId), summary, budgetState);
    setMeter(
      byId(root, meterId),
      mostConstrainedRatio(checks, field, limitField),
      1,
      summary,
      budgetState,
    );
  });
  const resetTimes = [...new Set(checks.map((check) => check.reset_at))];
  setElementState(
    byId(root, "provider-budget-reset"),
    resetTimes.join(" · "),
    budgetState,
  );
}

function matchingChecks(normalized, name) {
  return normalized.checks.filter((check) => check.name === name);
}

function summarizeStatuses(checks) {
  if (!checks.length) {
    return Object.freeze({label: "Unknown", className: "is-unknown"});
  }
  const labels = checks.map((check) => (
    check.scope
      ? `${check.scope}: ${check.status}`
      : check.status
  ));
  const className = checks.some(
    (check) => statusClass(check) === "is-blocked",
  )
    ? "is-blocked"
    : checks.some((check) => statusClass(check) === "is-stale")
      ? "is-stale"
      : checks.some((check) => statusClass(check) === "is-caution")
        ? "is-caution"
      : checks.every(
        (check) => statusClass(check) === "is-verified",
      )
        ? "is-verified"
        : "is-unknown";
  return Object.freeze({label: labels.join(" · "), className});
}

function renderEnvironment(normalized, root) {
  const brokerMode = matchingChecks(normalized, "broker_mode")[0];
  setElementState(
    byId(root, "environment-mode"),
    brokerMode && brokerMode.status === "paper"
      ? "ALPACA PAPER"
      : "ALPACA PAPER · UNVERIFIED",
    brokerMode && brokerMode.status === "paper"
      ? "status-chip is-verified"
      : "status-chip is-unknown",
  );
  const environmentChecks = [
    ["environment-breaker", "circuit_breaker"],
    ["environment-daemon", "daemon_heartbeat"],
    ["environment-reconciliation", "startup_reconciliation"],
    ["environment-quote", "quote_freshness"],
  ];
  environmentChecks.forEach(([elementId, checkName]) => {
    const summary = summarizeStatuses(matchingChecks(normalized, checkName));
    setElementState(
      byId(root, elementId),
      summary.label,
      summary.className,
    );
  });
  setElementState(
    byId(root, "environment-observed"),
    normalized.observedAt,
    "is-caution",
  );
}

export function renderPosture(normalized, root = document) {
  if (!normalized || normalized.valid !== true) {
    clearPosture(
      {message: normalized?.reason || "Security posture is unavailable"},
      root,
    );
    return Object.freeze({
      modelBlocked: true,
      modelReason: "Provider call blocked before network I/O",
    });
  }

  const panel = byId(root, "security-posture-panel");
  if (panel) {
    panel.className = "panel posture-panel";
  }
  const list = byId(root, "security-posture-list");
  if (list) {
    list.textContent = "";
    normalized.checks.forEach((check) => {
      const row = root.createElement("article");
      row.className = `posture-row ${statusClass(check)}`;
      const heading = root.createElement("strong");
      heading.textContent = readableName(check.name);
      const status = root.createElement("span");
      status.className = `status-chip ${statusClass(check)}`;
      status.textContent = check.status;
      const detail = root.createElement("small");
      detail.textContent = check.scope
        ? `${check.scope} · ${check.detail_code}`
        : check.detail_code;
      row.appendChild(heading);
      row.appendChild(status);
      row.appendChild(detail);
      list.appendChild(row);
    });
  }
  setElementState(
    byId(root, "security-posture-observed"),
    `Observed ${normalized.observedAt} · ${normalized.ageSeconds}s old`,
    "is-caution",
  );
  renderBudget(normalized, root);
  renderEnvironment(normalized, root);
  return Object.freeze({
    modelBlocked: normalized.provider.blocked,
    modelReason: normalized.provider.reason,
  });
}

export function clearPosture(error = {}, root = document) {
  const panel = byId(root, "security-posture-panel");
  if (panel) {
    panel.className = "panel posture-panel has-error is-unknown";
  }
  const requestId = isNonblankString(error.requestId)
    ? ` Request ${error.requestId}.`
    : "";
  const message = isNonblankString(error.message)
    ? error.message
    : "Security posture is unavailable";
  setElementState(
    byId(root, "security-posture-list"),
    `${message}.${requestId}`,
    "is-unknown",
  );
  setElementState(
    byId(root, "security-posture-observed"),
    "Observed: Unknown",
    "is-unknown",
  );
  [
    "provider-budget-calls",
    "provider-budget-input",
    "provider-budget-output",
    "provider-budget-reset",
  ].forEach((id) => {
    setElementState(byId(root, id), "Unknown", "is-unknown");
  });
  [
    "provider-budget-calls-meter",
    "provider-budget-input-meter",
    "provider-budget-output-meter",
  ].forEach((id) => {
    setMeter(byId(root, id), 0, 1, "Unknown", "is-unknown");
  });
  setElementState(
    byId(root, "environment-mode"),
    "ALPACA PAPER · UNVERIFIED",
    "status-chip is-unknown",
  );
  ENVIRONMENT_IDS.forEach((id) => {
    setElementState(byId(root, id), "Unknown", "is-unknown");
  });
  setElementState(
    byId(root, "environment-observed"),
    "Unknown",
    "is-unknown",
  );
  return Object.freeze({
    modelBlocked: true,
    modelReason: "Provider call blocked before network I/O",
  });
}
