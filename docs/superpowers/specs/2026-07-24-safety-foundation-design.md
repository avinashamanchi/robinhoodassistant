# Safety Foundation — Subproject Design

**Date:** 2026-07-24  
**Parent:** `2026-07-24-evidence-first-trading-platform-design.md`  
**Status:** Approved direction, pending written-spec review

## 1. Objective

Close the known control-plane, persistence, broker-side-effect, concurrency, and
service-boundary defects before adding new data, model, connector, or UI
capabilities.

The subproject is complete when a crash, retry, duplicate request, stale input,
missing secret, malformed rule, or concurrent OCO trigger cannot silently create
new risk or falsely report safety.

## 2. Scope

### Included

- Alembic migrations and startup schema-version enforcement.
- Fail-closed API authentication and secure browser sessions.
- Authentication for every endpoint except minimal liveness.
- CSRF, CSP, frame denial, secure headers, audit identity, and sensitive-log
  permissions.
- Durable submission outbox for every order path.
- `SUBMITTING` and `ACCEPTANCE_UNKNOWN` recovery by client order ID.
- Typed rule conditions/actions/states.
- Plan-level/OCO execution leases.
- Risk checks for buying power, quote age, pending exposure, daily loss, and
  persisted circuit breakers.
- Broker-drift and data-integrity breakers.
- One shared composition root.
- Incremental extraction of the current god modules into focused services.
- Heartbeat upsert/retention and indexed reconciliation cursors.
- Regression, crash, concurrency, migration, and security tests.

### Excluded

- New trading signals, model providers, or strategy tuning.
- Event/news/SEC/ALFRED ingestion.
- Backtest methodology changes.
- Full UI redesign beyond the authentication and truthful-action changes needed
  to make the current control plane safe.
- Robinhood, Composio, live trading, and autonomous execution.

## 3. Compatibility posture

The existing SQLite data remains authoritative and is migrated in place only
after a verified backup. Upgrade and downgrade behavior is tested against a copy,
not the operator database. Existing paper orders, fills, rules, reports, and
plans remain readable.

The current external Robinhood source stays disabled throughout the subproject
and is removed from install/runtime paths before completion. Historical rows
labeled as external holdings are retained as inert audit data.

## 4. Persistence design

### 4.1 Migrations

- Add Alembic with one baseline revision matching the current schema.
- Add incremental revisions for new states, audit fields, leases, and cursors.
- Startup checks the required revision and fails with an explicit upgrade command.
- Only an operator command runs migrations.
- CI creates a legacy database fixture, upgrades it, verifies data, and starts the
  app and worker against it.

### 4.2 Order state

Add:

- `approval_actor`, `approval_reason`, `approved_at`;
- `submission_attempt`, `submission_started_at`;
- `acceptance_state`, `last_reconciled_at`, `last_error_code`;
- `version` for optimistic state transitions.

Legal execution path:

```text
PROPOSED
  -> APPROVAL_RECORDED
  -> SUBMITTING
  -> SUBMITTED | ACCEPTANCE_UNKNOWN
  -> PARTIALLY_FILLED | FILLED | CANCELED | REJECTED
```

The approval transaction records the actor and consumes the proposal. A second
transaction claims `APPROVAL_RECORDED -> SUBMITTING`. Only then does the worker
call the broker. Broker success or definitive rejection is persisted afterward.
Timeout/connection ambiguity becomes `ACCEPTANCE_UNKNOWN`.

The reconciler looks up `SUBMITTING` and `ACCEPTANCE_UNKNOWN` orders by
idempotency/client order ID before any retry. An order with unknown acceptance
reserves exposure until resolved.

### 4.3 Rule groups

Persist typed condition and action payload versions. Add a rule-group row with:

- state and optimistic version;
- lease owner and expiry;
- terminal child rule ID;
- last evaluation and error metadata.

Workers atomically lease a group before evaluating/submitting any child. A
terminal claim cancels siblings in the same transaction. Expired leases recover
to active after reconciliation confirms no unknown submission.

## 5. Authentication and browser security

- App startup requires a non-empty operator secret outside tests.
- `/health/live` returns only process/database liveness booleans.
- All other routes require an authenticated session.
- `/auth/login` exchanges the operator secret for a short-lived signed session.
- The session cookie is `HttpOnly`, `SameSite=Strict`, path-scoped, and `Secure`
  whenever TLS is used.
- Mutations require a CSRF token tied to the session.
- Approval, kill-switch reset, connector changes, and future live actions require
  recent reauthentication.
- Security headers include CSP, `frame-ancestors 'none'`,
  `X-Content-Type-Options`, strict referrer policy, and permissions policy.
- API responses set `Cache-Control: no-store` for account and execution data.
- The bearer token is removed from browser `localStorage`.

Tests may inject an authenticated principal explicitly. There is no fail-open
runtime branch.

## 6. Risk and circuit breakers

`RiskEngine.check()` remains deterministic. The assembled snapshot gains:

- quote source timestamp and freshness verdict;
- pending buy/sell exposure and reserved exit quantity;
- buying power and cash;
- realized and unrealized daily P&L;
- account high-water mark and current drawdown;
- broker-reconciliation status;
- provider health and spread/liquidity status.

The execution path checks every configured limit synchronously. A background loop
may trip breakers early, but execution cannot depend on that loop having run.

New persisted breaker scopes:

- `loss:{asset_class}`;
- `drawdown:{asset_class}`;
- `data:{provider_or_asset_class}`;
- `liquidity:{symbol_or_asset_class}`;
- `broker_drift`;
- `operator_global`.

Breaker reset is a state transition with actor, reason, prior health report, and
timestamp. Resetting one scope cannot clear another.

## 7. Service boundaries

Introduce one `ApplicationContainer` in `bootstrap.py`. It creates the engine,
repositories, broker clients, clocks, risk policies, services, and workers.

Extract behavior without a flag-day rewrite:

1. repositories and typed persisted models;
2. `PortfolioSnapshotService`;
3. `OrderApplicationService`;
4. `OrderSubmissionService`;
5. `ReconciliationService`;
6. `RuleApplicationService` and `RuleWorker`;
7. `OperationsService`.

API routers and daemon tasks depend on these interfaces. Temporary compatibility
methods may delegate from `TradingService`, but new behavior is added only to the
new services. The compatibility facade is removed when no caller remains.

## 8. Operational behavior

- Heartbeat uses one upserted row per process and bounded history for incidents.
- Reconciliation uses persisted cursors and broker activity IDs.
- Network I/O occurs outside SQLite write transactions.
- Retriable provider failures use bounded exponential backoff with jitter.
- Every mutation has a principal, request ID, idempotency key, result, and latency.
- Logs are `0600`, rotate, and install redaction in API, worker, MCP, and commands.
- Startup health reports required and optional subsystem failures explicitly;
  configured required subsystems fail startup.

## 9. Error semantics

- `401`: no valid session.
- `403`: CSRF, reauthentication, or policy denial.
- `409`: stale version, already-consumed approval, lease conflict, unknown
  acceptance, or breaker conflict.
- `422`: invalid typed command.
- `429`: principal budget/concurrency/rate limit.
- `503`: required broker, data, database, or reconciliation dependency unhealthy.

User-facing errors include a stable code and request ID, never a secret or raw
provider payload.

## 10. Test design

### Migration

- upgrade a populated legacy fixture;
- preserve order/fill/rule/report relationships;
- reject startup at an old or unknown revision;
- verify backup before migration command proceeds.

### Authentication

- missing secret fails startup;
- all non-liveness routes reject anonymous requests;
- sessions expire and cannot be read by JavaScript;
- CSRF and frame/security headers are enforced;
- sensitive responses are non-cacheable;
- no token is stored in browser storage.

### Submission durability

Inject a crash before submission, after broker acceptance, and before each
database commit. Prove that:

- no duplicate broker order is created;
- unknown acceptance reserves exposure;
- reconciliation resolves by client order ID;
- panic includes unknown and remotely discovered open orders.

### Concurrency

- concurrent approval succeeds once;
- concurrent submission claim succeeds once;
- two sibling exits cannot both claim or execute;
- duplicate fills and provider events are idempotent;
- expired leases recover only after reconciliation.

### Risk

- every limit is checked synchronously at execution;
- stale quote, insufficient buying power, pending exposure, daily loss, drawdown,
  data breaker, and broker drift independently block new risk;
- breaker restart persistence and scoped reset are verified.

### Regression

- existing paper broker integration remains credential-gated and passes;
- current full deterministic suite remains green;
- new branch/line coverage gates apply to safety-critical modules;
- tracked/history secret scans remain clean.

## 11. Acceptance criteria

- Versioned migrations replace `create_all()` in every production composition
  root.
- No authenticated write or sensitive read can fail open.
- No browser credential is stored in JavaScript-readable persistent storage.
- No broker network request occurs inside an open SQLite write transaction.
- Every submission state is recoverable after process termination.
- OCO groups have one terminal execution under concurrent workers.
- Rules are schema-validated before persistence.
- All execution risk inputs are current, complete, and checked synchronously.
- Health and panic responses never overstate confirmed safety.
- Logs and audit rows identify actions without exposing secrets.
- Full deterministic and credentialed paper verification passes.

## 12. Completion gate

The data/evidence subproject may begin only after these criteria pass a dedicated
code review, crash drill, concurrency test run, migration rehearsal on a copied
database, and final paper-account reconciliation.

