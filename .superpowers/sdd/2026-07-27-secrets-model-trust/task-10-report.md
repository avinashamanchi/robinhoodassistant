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
