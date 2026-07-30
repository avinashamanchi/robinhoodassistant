# Task 10 Report — Immutable Redacted Security Posture

## Outcome

Task 10 was implemented in `ab9f82a31b3b0a106b0700070e1c4e6033f358e7`.

Authenticated operators now have a local-only
`GET /security/posture` route under the exact `SESSION/session_read` policy.
It returns frozen, extra-forbid `PostureCheck` and
`SecurityPostureReport` models. `can_trade` is permanently
`Literal[False]`, and a source audit proves no production module outside the
posture definition references that field.

No migration was added. No Task 11 work was started.

## Architecture

### One startup-evidence chain

`ops.serve` now:

1. constructs one explicit `MacOSKeychainSecretProvider`;
2. calls `load_role_secrets("app", ...)` once;
3. runs the structural startup guard against that exact
   `RuntimeSecrets` object;
4. freezes stable structural checks plus provider/load timestamps into
   `StartupPostureEvidence`;
5. builds one `ApplicationContainer` from the same config, secrets, and
   evidence objects; and
6. passes that exact container/evidence pair into `create_app`.

Container/evidence identity mismatches fail closed. Unit-injected apps without
startup evidence report startup-derived checks as `unknown`. Route reads do
not retain, construct, or call a Keychain provider.

### SELECT-only local evidence

`DurableRateLimiter.inspect_pair()` reads the current fixed-window buckets
without creating, resetting, pruning, or committing them.

`ProviderBudgetService.inspect()` validates and reads provider/day/reservation
state without `BEGIN IMMEDIATE`, sweep, transition, latch, or commit. Expired
`started` and `unknown` reservations are reported as unresolved blocked
evidence while their durable rows remain unchanged. Posture never calls the
mutating `ProviderBudgetService.status()`.

Startup reconciliation is selected directly from stable status, generation,
and timestamp columns. Breaker reads select only scope, kind, target, trip
state, generation, and update time. Neither path loads or decrypts reasons,
actors, request IDs, or evidence JSON.

Quote posture is always:

```text
status=unknown
detail_code=quote_evidence_unavailable
```

No broker call, heartbeat inference, migration, or synthesized freshness was
added.

### Typed redaction boundary

Posture independently reports:

- paper mode;
- startup loopback/TLS evidence;
- macOS Keychain provider/load-time evidence;
- encryption migration/schema/progress and envelope scan state;
- all request-limit classes and reset times;
- supported provider budgets and UTC resets;
- webhook and Composio disabled state;
- quarantine counts;
- circuit breakers by canonical scope/generation only;
- daemon heartbeat freshness;
- reconciliation generation/timestamps;
- unavailable quote evidence;
- runtime tenure role/state/generation/safe timestamps;
- unsafe order/fill/rule/rule-group counts; and
- uncertain interlock count.

The schema has no dictionary/free-form detail field and no path, URL, secret,
presence, key-ID, hash, prompt, tool-call, external-text, actor, reason, or
request-ID field. Exceptions are normalized to fixed detail codes. A durable
store failure turns DB-derived checks `unknown` while config and immutable
startup checks remain independently reportable.

## Exact RED evidence

Before production implementation:

```text
uv run pytest tests/test_security_posture.py tests/test_ops.py -v

collected 18 items
4 failed, 14 passed in 1.47s
```

The four intended failures were:

- `GET /security/posture` returned `404`;
- `trading_assistant.operations.security_posture` was missing for the model
  tests; and
- `ProviderBudgetService.inspect` was missing.

The launcher-chain test then failed before its implementation with:

```text
AttributeError: module 'trading_assistant.ops.serve' has no attribute
'MacOSKeychainSecretProvider'
```

That RED established that the launcher did not yet own an explicit one-load
provider/evidence/container chain.

## Focused verification

The final exact Task 10 focused gate:

```text
uv run pytest tests/test_security_posture.py tests/test_route_policy.py \
  tests/test_ops.py -v

collected 135 items
135 passed in 12.16s
```

Additional affected verification:

```text
uv run pytest tests/test_bootstrap.py -v
53 passed, 1 warning in 3.13s

uv run pytest tests/test_transport_boundary.py -q
59 passed

uv run pytest tests/test_durable_limits.py tests/test_llm_budget.py \
  tests/test_llm_budget_review.py tests/test_llm_budget_review_2.py \
  tests/test_llm_budget_review_3.py -q
PASS (exit 0)

uv run pytest tests/test_api.py tests/test_auth.py \
  tests/test_backtests_api.py tests/test_candidate_boundary.py \
  tests/test_launch.py tests/test_plans_api.py \
  tests/test_reconciliation_service.py tests/test_runtime_tenure.py \
  tests/test_security.py tests/test_security_headers.py \
  tests/test_submission_barrier.py tests/test_task9_round2.py -q
PASS (exit 0, 100%)

uv run python -m compileall -q src/trading_assistant \
  tests/test_security_posture.py tests/test_route_policy.py \
  tests/test_ops.py tests/test_bootstrap.py \
  tests/test_transport_boundary.py
PASS

git diff --check
PASS
```

`uv run ruff check ...` was attempted but Ruff is not installed and the
project has no configured Ruff gate. The required compile, focused, full,
diff, and release-static gates passed.

## Full-suite and static verification

Exactly one no-argument full suite was run after focused green:

```text
uv run pytest
3262 passed, 1 skipped, 1 warning in 244.20s
```

The warning is the pre-existing third-party `websockets.legacy` deprecation
warning. The full suite was not rerun.

The required static gate then passed:

```text
uv run python scripts/check_release_safety.py
release static checks: PASS
```

## Read-only and no-I/O proof

Tests use temp file-backed SQLite, fixed clocks, and injected fakes only.

- All mapped tables are snapshotted before and after repeated and concurrent
  direct posture reports; every row remains equal.
- Expired provider reservations and provider-day rows remain equal before and
  after inspection.
- Repeated and concurrent authenticated GETs succeed while broker methods and
  Keychain construction are patched to raise.
- Route authentication and `session_read` exhaustion return the expected
  `401` and `429`.
- Serialized marker tests prove breaker/reconciliation narratives, actors,
  request IDs, external flags, hashes, prompts, tool calls, process identity,
  key IDs, and secret values do not appear.

The durable `session_read` rate-window accounting performed by existing route
middleware is the only expected HTTP-policy mutation. The posture aggregation
itself is SELECT-only and byte-for-byte table preserving.

No service, daemon, MCP server, real Keychain, credential, ignored runtime
database, broker, provider, notifier, or network endpoint was accessed by the
Task 10 posture path. It did not approve, submit, cancel, reconcile, reset,
notify, or start anything.

## Implementation files

- `src/trading_assistant/app/limits.py`
- `src/trading_assistant/app/main.py`
- `src/trading_assistant/app/policy.py`
- `src/trading_assistant/bootstrap.py`
- `src/trading_assistant/llm/budget.py`
- `src/trading_assistant/operations/__init__.py`
- `src/trading_assistant/operations/security_posture.py`
- `src/trading_assistant/operations/service.py`
- `src/trading_assistant/ops/serve.py`
- `tests/test_bootstrap.py`
- `tests/test_ops.py`
- `tests/test_route_policy.py`
- `tests/test_security_posture.py`
- `tests/test_transport_boundary.py`

## Review package

- Base: `3a8904a8f62e8b2281b09b764330c7b8f8e49ed7`
- Implementation: `ab9f82a31b3b0a106b0700070e1c4e6033f358e7`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-3a8904a..ab9f82a.diff`
- Diff size: 3,314 lines / 112,060 bytes

The packaged diff was reviewed for authority consumption, mutable service
calls, unsafe selected columns, startup identity drift, exception/narrative
leakage, and Task 11 scope. No open finding remains at this implementation
gate.

## Residual concerns

- Quote posture deliberately remains `unknown` until a later explicitly
  planned durable quote-observation source exists.
- Unit-injected apps deliberately report missing startup evidence as
  `unknown`; only the strict production launcher can supply authenticated
  startup evidence.
- The existing `websockets.legacy` warning remains unrelated and
  non-blocking.

These are explicit conservative states, not trading permissions.

---

## Fix round 1 amendment

### Outcome

All seven fresh-review findings were fixed in
`8e553d4ab1bf20e99bbfa41594f1d8d8733b0c0e`.

The route remains authenticated `SESSION/session_read`, local-only, typed,
redacted, and non-authoritative. Its one permitted policy write is ordinary
`RateWindow` accounting. It performs no `ConcurrencyLease` write and no
domain-table write.

### Architecture decisions

1. `RoutePolicy.lease_free_bounded_read` defaults to `False`. Only
   `GET /security/posture` enables it, and construction rejects mutation,
   idempotency, broker, provider, target, or coalescing authority on such a
   policy. Authentication and durable rate limiting still run before the
   handler.
2. `SensitiveEncryptionPostureInspector` selects only the migration
   singleton ID, schema/state/progress integers, and safe lifecycle
   timestamps. The route has no engine/cipher dependency and never invokes
   `SensitiveEncryptionStateInspector`, `inspect_sensitive_envelopes`, or
   `decrypt`. A complete row passes only when the canonical startup receipt
   recorded a successful encryption scan and the safe migration timestamp has
   not advanced beyond that scan.
3. `validate_startup_reconciliation_snapshot` is the single pure validator
   used by both posture and `StartupReconciliationGate.is_current()`. It
   requires positive exact-int generations, exact completion of the current
   generation, allowed status, ordered required timestamps, and no future
   evidence.
4. Posture validates complete known domains before counting
   `Order.status`, `Order.acceptance_state`, `Order.plan_cancel_state`,
   `Fill.reconciliation_state`, `Rule.state`, and the narrower valid
   `RuleGroup.state` domain. Unknown values produce
   `unknown/state_domain_invalid`, never `clear`.
5. `StartupGuardReceipt` is opaque, immutable, identity-bound to the exact
   config/secrets, and issued only from the private startup path after the
   exact canonical successful check set. Public `build_container` and
   `create_app` accept no evidence/receipt; guarded composition uses private
   entrypoints and rejects fabricated, partial, mismatched, or publicly
   attached receipts.
6. Circuit breakers serialize only the fixed categories `account`, `equity`,
   `crypto`, and `liquidity`, with aggregate trip counts and maximum positive
   generation. Raw scope keys, symbols, targets, reasons, actors, and request
   IDs never enter the response. Invalid scope/generation evidence makes all
   breaker categories unknown.
7. Both posture models use strict Pydantic configuration. Exact-type
   validators reject bool/string/float coercion into integer fields, reject
   non-float age evidence, and require `type(can_trade) is bool` with the value
   exactly `False`.

This is Python API and call-graph isolation, not cryptographic isolation.
Private entrypoints and the sealed sentinel enforce the current production
composition boundary; the separate Task 11 static caller restriction remains
out of scope and was not started.

### Exact RED evidence

The fix-round tests were written before production changes. The exact
aggregate RED command was:

```text
uv run pytest tests/test_security_posture.py tests/test_route_policy.py \
  tests/test_bootstrap.py tests/test_launch.py \
  tests/test_transport_boundary.py --tb=no

48 failed, 262 passed, 1 warning in 21.19s
```

Those failures covered lease mutation, decrypting route inspection,
reconciliation parity/corruption, unknown persisted domains, startup
provenance, breaker target redaction, and strict scalar rejection.

Diff review added four narrower fail-closed regressions before their
production corrections:

```text
uv run pytest \
  tests/test_security_posture.py::test_database_failure_keeps_config_and_startup_checks_reportable \
  --tb=short
1 failed in 0.12s

uv run pytest \
  tests/test_security_posture.py::test_malformed_breaker_generation_is_unknown_never_clear \
  --tb=short
1 failed in 0.24s

uv run pytest \
  tests/test_security_posture.py::test_rule_group_rejects_rule_only_processing_state \
  --tb=short
1 failed in 0.16s

uv run pytest \
  tests/test_security_posture.py::test_startup_receipt_issuer_rejects_partial_or_inconsistent_guard_checks \
  --tb=short
2 failed in 0.13s
```

### Focused GREEN evidence

The final exact Task 10 focused gate was:

```text
uv run pytest tests/test_security_posture.py tests/test_route_policy.py \
  tests/test_bootstrap.py tests/test_launch.py \
  tests/test_transport_boundary.py --tb=short

314 passed, 1 warning in 20.23s
```

Additional affected focused batches:

```text
uv run pytest tests/test_api.py tests/test_auth.py \
  tests/test_backtests_api.py tests/test_ops.py tests/test_security.py \
  tests/test_security_headers.py --tb=short
270 passed, 1 warning in 35.09s

uv run pytest tests/test_planning.py \
  tests/test_reconciliation_service.py \
  tests/test_startup_reconciliation.py --tb=short
181 passed, 1 warning in 12.69s

uv run pytest tests/test_candidate_boundary.py \
  tests/test_cooperative_control.py tests/test_plans_api.py \
  tests/test_runtime_tenure.py tests/test_submission_barrier.py \
  tests/test_task9_round2.py tests/test_watchdog.py --tb=short
450 passed, 1 warning in 48.04s

uv run python -m compileall -q src/trading_assistant \
  tests/test_security_posture.py tests/test_route_policy.py \
  tests/test_bootstrap.py tests/test_launch.py \
  tests/test_transport_boundary.py
PASS

git diff --check
PASS
```

The warning in the pytest batches is the existing third-party
`websockets.legacy` deprecation.

### Sole full suite and static gate

After focused green and diff review, exactly one no-argument full suite was
run:

```text
uv run pytest
3297 passed, 1 skipped, 1 warning in 251.65s
```

It was not rerun. The required static gate then passed:

```text
uv run python scripts/check_release_safety.py
release static checks: PASS
```

### Read-only/no-I/O proof

The repeated/concurrent authenticated GET regression patches broker reads and
mutations, Keychain construction, provider-budget mutators, notifier send,
the decrypting startup inspector, the envelope scanner, and cipher decryption
to raise. Six GETs complete successfully. A before/after snapshot of every
mapped table is identical except normal `RateWindow` accounting;
`ConcurrencyLease` is exactly unchanged.

Separate fixed-clock/temp-SQLite cases prove:

- expired `started`/`unknown` provider reservations are blocked evidence with
  no sweep/latch/update;
- corrupt/future reconciliation rows agree with the authoritative gate and
  never pass;
- hostile unknown order/fill/rule/group states never report clear;
- `liquidity:APP_API_TOKEN`, target symbols, decrypted narratives, key IDs,
  hashes, prompts, tool calls, paths, URLs, and exception text do not appear
  in serialized JSON;
- fabricated/partial/mismatched startup evidence cannot yield a passing
  startup-derived posture check; and
- `can_trade` is an exact built-in `False` and no production consumer
  references it.

All tests used temp SQLite, fixed clocks, and fakes. No service, daemon, MCP,
real Keychain/credential, ignored `trading_assistant.db`, broker, provider,
notifier, network endpoint, trade, reconciliation, reset, prune, sweep,
reserve, or push action was performed.

### Fix-round implementation files

- `src/trading_assistant/app/main.py`
- `src/trading_assistant/app/policy.py`
- `src/trading_assistant/bootstrap.py`
- `src/trading_assistant/operations/security_posture.py`
- `src/trading_assistant/operations/service.py`
- `src/trading_assistant/ops/serve.py`
- `src/trading_assistant/orders/startup.py`
- `tests/test_bootstrap.py`
- `tests/test_launch.py`
- `tests/test_route_policy.py`
- `tests/test_security_posture.py`
- `tests/test_transport_boundary.py`

### Fix-round review package

- Base:
  `c1f9041c2757739da3fb0d3cfa3a9c75a5fed982`
- Implementation:
  `8e553d4ab1bf20e99bbfa41594f1d8d8733b0c0e`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-c1f9041..8e553d4.diff`
- Diff size:
  3,403 lines / 116,945 bytes

The bounded package was reviewed against all seven fresh findings, production
callers, authority consumption, selected columns, middleware ordering,
narrative/target leakage, and Task 11 scope. No open implementation finding
remains at this gate.

### Residual concerns after fix round 1

- Quote evidence intentionally remains
  `unknown/quote_evidence_unavailable`.
- Route-time encryption posture authenticates the successful startup full
  scan against unchanged safe migration metadata. A post-start ciphertext
  change that bypasses migration metadata cannot be rediscovered without
  violating the route's no-ciphertext/no-decryption requirement; this is not
  authority because `can_trade` remains exactly `False`.
- Unit-injected apps deliberately report startup-derived evidence as unknown.
- The receipt boundary is normal Python private-API/call-graph enforcement,
  not a cryptographic capability. Task 11 may add static caller restrictions,
  but no Task 11 work was performed here.
- The pre-existing `websockets.legacy` warning remains unrelated and
  non-blocking.

These residuals are conservative evidence limits, not permissions.

---

## Fix round 2 amendment

### Outcome

All four fresh findings were fixed in
`87aa612a382d29e95e44bcf57728637d8cf84b5a`.

The posture route remains authenticated `SESSION/session_read`, local-only,
typed, redacted, and non-authoritative. The round changes trading authority
only to make the existing reconciliation requirement fail closed in
`PortfolioSnapshotService`; no posture result is consumed as authority.

### Architecture decisions

1. `PortfolioSnapshotService` now selects only reconciliation generation,
   status, and lifecycle timestamps, then calls the same pure
   `validate_startup_reconciliation_snapshot` function as
   `StartupReconciliationGate.is_current()` and posture. The snapshot's one
   `captured_at` value is passed as `observed_at`; future, corrupt,
   incomplete, or out-of-order rows make `broker_reconciled=False`, which
   keeps `RiskEngine` closed. No actor, reason, request ID, evidence JSON, or
   cipher is read.
2. `StartupGuardReceipt` is bound to exact config and secrets object
   identities, one exact runtime role, and the private canonical launch-chain
   sentinel. `_build_guarded_container` atomically consumes it under its own
   lock before calling any container builder. Wrong-role attempts reject
   without consuming; exact sequential/concurrent reuse rejects with the
   stable consumed code; a failed build remains consumed. Only a private
   consumed-chain context traverses construction, and neither that context nor
   the receipt is stored. `ApplicationContainer` retains only frozen
   `StartupPostureEvidence`.
3. `RoutePolicy` permits `lease_free_bounded_read=True` only for the exact
   tuple `GET /security/posture`, `AuthLevel.SESSION`, `session_read`, default
   body limit, principal concurrency scope, reject behavior, and no authority
   flags. Lowercase methods, aliases, trailing/double slashes, parameterized
   paths, alternate auth/limits/scopes, and every mutation/idempotency flag
   reject at construction. The default remains `False` for all other routes.
4. `RoutePolicyRegistry` separately inventories actual effective
   `APIRoute` handlers. It normalizes repeated/trailing slashes and parameter
   names/converters, expands FastAPI's lazy included-router contexts, and
   rejects duplicate effective method/path pairs during startup validation.
   Distinct `GET`/`HEAD`/`OPTIONS` methods, one multi-method declaration with
   unique methods, and static mounts remain valid.

The receipt boundary is Python private-API, sentinel, identity, and call-graph
enforcement. It is intentionally not described as cryptographic isolation.
Task 11 static caller restrictions remain out of scope.

### Exact RED evidence

The combined review probes were written before production changes:

```text
uv run pytest -q \
  tests/test_security_posture.py::test_reconciliation_posture_matches_authoritative_safe_column_gate \
  tests/test_bootstrap.py::test_startup_guard_receipt_is_role_bound_and_wrong_role_does_not_consume \
  tests/test_bootstrap.py::test_startup_guard_receipt_is_consumed_before_sequential_reuse \
  tests/test_bootstrap.py::test_startup_guard_receipt_has_exactly_one_concurrent_consumer \
  tests/test_bootstrap.py::test_failed_guarded_construction_still_consumes_receipt \
  tests/test_bootstrap.py::test_application_container_retains_evidence_not_reusable_receipt \
  tests/test_route_policy.py::test_lease_free_capability_is_confined_to_exact_posture_policy \
  tests/test_route_policy.py::test_exact_security_posture_policy_can_be_lease_free \
  tests/test_route_policy.py::test_duplicate_effective_api_handlers_fail_inventory_validation \
  tests/test_route_policy.py::test_including_same_router_twice_is_a_duplicate_effective_handler \
  tests/test_route_policy.py::test_duplicate_handler_normalization_preserves_unique_http_methods \
  tests/test_route_policy.py::test_dynamic_shadow_posture_handler_fails_at_app_startup
```

Result: exit `1`, with `30 failed` and `9 passed` across the 39 selected
cases. Eight reconciliation cases initially failed in test setup because the
fixed-time mock expected a callable and received a `datetime`; that
test-only fixture was corrected before production edits and is not counted as
authority evidence.

The corrected reconciliation differential then produced the intended RED:

```text
uv run pytest -q \
  tests/test_security_posture.py::test_reconciliation_posture_matches_authoritative_safe_column_gate
```

Result: `3 failed, 5 passed`. Future-start, future-completion, and
timestamp-order corruption passed the old snapshot's weaker
generation/status check while both the gate and posture rejected them.

The remaining combined RED failures reproduced absent receipt role/one-shot
enforcement, reusable receipt retention, thirteen lease-free tuple escapes,
and four duplicate-handler/inventory gaps.

### Focused GREEN evidence

The final focused plus snapshot/risk/auth/API/operations adjacency gate was:

```text
uv run pytest -o addopts='' -q \
  tests/test_security_posture.py \
  tests/test_bootstrap.py \
  tests/test_route_policy.py \
  tests/test_transport_boundary.py \
  tests/test_launch.py \
  tests/test_startup_reconciliation.py \
  tests/test_reconciliation_service.py \
  tests/test_execution_risk_snapshot.py \
  tests/test_risk_engine.py \
  tests/test_execution.py \
  tests/test_auth.py \
  tests/test_api.py \
  tests/test_ops.py
```

Result: `712 passed, 1 warning in 38.65s`.

An earlier run of this exact adjacency set found one compatibility regression:
`1 failed, 711 passed, 1 warning`. An injected `OperationsService` posture
reader correctly has no private startup-evidence attribute. The constructor
was corrected to preserve that test-injection seam as explicit unknown
startup evidence; the exact failing test then passed and the complete
712-test gate above was rerun green.

Additional checks before the sole full suite:

```text
uv run python -m compileall -q src/trading_assistant \
  tests/test_bootstrap.py tests/test_route_policy.py \
  tests/test_security_posture.py tests/test_transport_boundary.py
PASS

git diff --check
PASS
```

### Sole full suite and static gate

After final focused green and diff review, exactly one no-argument full suite
was run:

```text
uv run pytest
3323 passed, 1 skipped, 1 warning in 252.16s
```

It was not rerun. The one warning remains the documented third-party
`websockets.legacy` deprecation.

The required static gate then passed:

```text
uv run python scripts/check_release_safety.py
release static checks: PASS
```

### Authority, read-only, and no-I/O proof

The fixed-clock differential seeds adversarial reconciliation rows with
encrypted narrative markers, patches `SensitiveDataCipher.decrypt` to raise,
and proves gate, posture, snapshot `broker_reconciled`, and final risk approval
all agree. Decryption calls remain exactly zero.

Round-1 repeated/concurrent route tests remain in the final focused/full
gates. They patch broker reads/mutations, provider mutators, notifier,
Keychain, startup encryption inspection, envelope scanning, and cipher
decryption to raise. Before/after snapshots preserve every mapped table except
ordinary `session_read` `RateWindow` accounting; `ConcurrencyLease` remains
exactly unchanged.

Receipt tests use temp SQLite/fakes and prove:

- wrong-role rejection followed by exactly one correct-role success;
- one sequential success and stable reuse rejection;
- 32 simultaneous exact consumers produce one build and 31 stable consumed
  rejections;
- construction failure consumes before the failing builder and cannot retry;
- launch-chain tampering and fabricated receipts reject before composition;
- public/test composition remains startup-unknown; and
- canonical private app composition receives the same immutable evidence but
  stores no reusable receipt.

No service, daemon, MCP server, real Keychain/credential, ignored
`trading_assistant.db`, broker/provider/notifier/network endpoint, decryption,
trade, reconciliation, reset, prune, sweep, reserve, notification, or push
action was performed.

### Fix-round implementation files

- `src/trading_assistant/app/main.py`
- `src/trading_assistant/app/policy.py`
- `src/trading_assistant/bootstrap.py`
- `src/trading_assistant/operations/security_posture.py`
- `src/trading_assistant/operations/service.py`
- `src/trading_assistant/ops/serve.py`
- `src/trading_assistant/orders/snapshot.py`
- `tests/test_bootstrap.py`
- `tests/test_launch.py`
- `tests/test_route_policy.py`
- `tests/test_security_posture.py`
- `tests/test_transport_boundary.py`

### Fix-round review package

- Base:
  `d0d4fa6ba7b97766f808a95ae996cff228be1875`
- Implementation:
  `87aa612a382d29e95e44bcf57728637d8cf84b5a`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-d0d4fa6..87aa612.diff`
- Diff size:
  1,600 lines / 57,453 bytes

The bounded package was reviewed against all four findings, production
callers, execution-authority parity, selected reconciliation columns,
receipt lifetime/identity/role/concurrency, route-policy aliases, effective
handler collisions, narrative decryption, and Task 11 scope. No open
implementation finding remains at this gate.

### Residual concerns after fix round 2

- Quote posture intentionally remains
  `unknown/quote_evidence_unavailable`.
- Receipt sealing is normal Python private-API/call-graph enforcement, not
  cryptographic isolation. Task 11 may add static caller restrictions; none
  were added here.
- Duplicate-handler classification runs at FastAPI startup after canonical
  route registration. Mutating the route graph after startup is unsupported
  and outside this production composition path.
- The pre-existing `websockets.legacy` warning remains unrelated and
  non-blocking.

These residuals are conservative evidence and composition limits, not trading
permissions.

---

## Fix round 3 amendment

### Outcome

The late-startup duplicate-handler finding was fixed in
`951e276ec10c54896824ea2441086c57b0d544ba`.

Route inventory validation no longer participates in the ordered startup
callback list. It now runs after the complete original FastAPI/router lifespan
has entered and immediately before the application can serve a request.
Task 10 posture behavior, authentication, rate limiting, read-only boundaries,
and permanent `can_trade=False` remain unchanged.

### Architecture decision

`install_route_inventory_lifespan()` is called once at the end of canonical
`create_app()` composition, after router inclusion, route declarations, and
middleware registration. It captures the complete original
`app.router.lifespan_context` and installs this outer context:

1. enter the original app/router lifespan;
2. allow every original startup callback and nested lifespan startup to
   complete;
3. call `validate_route_inventory()` against the final effective route graph;
4. yield the original lifespan state unchanged only if validation succeeds;
5. delegate shutdown and exception handling to the original context.

Validation failure occurs inside the original context, so its `__aexit__`
path still runs. Startup and shutdown exceptions are not caught, translated,
or replaced. A second installation leaves the exact installed wrapper
unchanged; if some caller has displaced it, installation rejects rather than
nests another validator.

The former `app.router.add_event_handler("startup", ...)` validator was
removed. Consequently, a later startup callback that calls
`add_api_route()`, includes an `APIRouter`, or appends directly to
`app.routes` completes before the inventory is checked.

### Exact RED evidence

The complete round-3 probes were added before production changes:

```text
uv run pytest -q \
  tests/test_route_policy.py::test_later_startup_callback_cannot_shadow_posture_handler \
  tests/test_route_policy.py::test_later_startup_direct_route_list_mutation_cannot_bypass_inventory \
  tests/test_route_policy.py::test_unique_route_added_by_later_startup_callback_is_served \
  tests/test_route_policy.py::test_route_inventory_lifespan_is_installed_once_and_preserves_state \
  tests/test_route_policy.py::test_inventory_failure_runs_original_lifespan_cleanup \
  tests/test_route_policy.py::test_original_lifespan_exceptions_propagate_unchanged
```

Result: exit `1`, `8 failed`.

The direct and router-inclusion posture-shadow cases both failed with
`DID NOT RAISE RuntimeError`, reproducing the review finding. The other six
parameterized cases failed because the required final-lifespan installer did
not exist; they encoded direct routes-list mutation, unique late-route
acceptance, one-time installation, nested lifespan state, cleanup, and exact
exception propagation.

After the implementation, the same selection passed:

```text
........ [100%]
8 passed
```

### Focused GREEN evidence

The final route/policy/auth/API/lifespan and security adjacency gate was:

```text
uv run pytest -q \
  tests/test_route_policy.py \
  tests/test_auth.py \
  tests/test_api.py \
  tests/test_security.py \
  tests/test_security_headers.py \
  tests/test_security_posture.py \
  tests/test_transport_boundary.py
```

Result: exit `0`; all `478` collected tests passed with the one existing
`websockets.legacy` warning.

The focused tests prove:

- later direct and included-router posture shadows fail TestClient startup
  before any request or shadow-handler invocation;
- direct `app.routes` mutation cannot bypass final inventory validation;
- one unique, pre-classified late route starts and serves normally;
- app and included-router lifespan state is preserved;
- app and included-router startup/shutdown each run exactly once;
- validation failure runs original cleanup; and
- original startup and shutdown exception objects propagate unchanged.

### Sole full suite and static gate

After focused green and final diff review, exactly one no-argument full suite
was run:

```text
uv run pytest
3331 passed, 1 skipped, 1 warning in 252.09s
```

It was not rerun. The warning is the pre-existing third-party
`websockets.legacy` deprecation.

The required static gate then passed:

```text
uv run python scripts/check_release_safety.py
release static checks: PASS
```

`git diff --check` also passed before the implementation commit.

### Fix-round implementation files

- `src/trading_assistant/app/main.py`
- `src/trading_assistant/app/policy.py`
- `tests/test_route_policy.py`

### Fix-round review package

- Base:
  `e14b1c0d4145326d8762e8a1fa32dd8b781fb3c3`
- Implementation:
  `951e276ec10c54896824ea2441086c57b0d544ba`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-e14b1c0..951e276.diff`
- Diff size:
  395 lines / 11,150 bytes
- Diff SHA-256:
  `3c86ff94c0b20963e7e7d9495d7464b27f9b9b44170b6d4c42e86bdcff1ef391`

The bounded package was reviewed against the one finding, final route
composition order, FastAPI included-router lifespan merging, direct route
registration/list mutation, state propagation, cleanup, exception identity,
and Task 11 scope. No open implementation finding remains at this gate.

### Residual concerns after fix round 3

- The route graph is intentionally treated as immutable once serving begins.
  Mutation after the lifespan wrapper yields is unsupported and is not
  reclassified continuously; canonical production composition exposes no such
  mutation path.
- Quote posture intentionally remains
  `unknown/quote_evidence_unavailable`.
- Startup receipt sealing remains ordinary Python private-API/call-graph
  enforcement, not cryptographic isolation.
- The pre-existing `websockets.legacy` warning remains unrelated and
  non-blocking.

These residuals are conservative composition/evidence limits, not trading
permissions. No Task 11, push, service/daemon/MCP start, real resource,
ignored runtime database, Keychain/credential, network, broker, provider,
notifier, decryption, reconciliation, or trading action occurred.
