# Task 10 Brief: Authenticated redacted security posture

## Scope

Implement only Plan 2 Task 10 from
`docs/superpowers/plans/2026-07-27-secrets-model-trust.md`.

The deliverable is an authenticated, session-rate-limited,
read-only `GET /security/posture` endpoint. Its response is typed,
redacted evidence and never an authority signal:

- `SecurityPostureReport.can_trade` is `Literal[False]`;
- no order, risk, rule, broker, daemon, reconciliation, notifier, or provider
  path consumes posture;
- no posture read resets, approves, submits, cancels, reconciles, sweeps,
  prunes, reserves, notifies, or performs outbound I/O.

Task 11 is explicitly out of scope.

## Immutable startup evidence

Add a frozen `StartupPostureEvidence` containing only:

- an aware UTC observation timestamp;
- frozen stable structural check names/status/detail codes;
- the provider classification `macos_keychain`;
- a stable secret-load status and optional successful-load timestamp.

It contains no secret values, presence bits, account/key IDs, filesystem
paths, certificate names, URLs, exception text, or narrative.

`ops.serve` must perform one production composition chain:

1. load config;
2. instantiate one explicit `MacOSKeychainSecretProvider`;
3. call `load_role_secrets("app", ...)` exactly once;
4. run the startup guard against that exact `RuntimeSecrets` object;
5. seal the returned structural checks plus provider/load evidence in an
   opaque `StartupGuardReceipt` bound by object identity to the exact config
   and secrets;
6. build one container through the private guarded composition entrypoint;
7. call the private guarded app entrypoint with that exact
   container/receipt.

The posture route never retains or calls the provider. An injected unit app
without startup evidence reports startup-derived checks as `unknown`; it does
not infer a pass from config. Public `build_container` and `create_app`
cannot accept startup evidence or a receipt, and a guarded container is
rejected by public app composition.

## Posture response

`PostureCheck` and `SecurityPostureReport` are Pydantic v2 models with
`extra="forbid"` and `frozen=True`. Check names, statuses, and emitted detail
codes use enums/literals. Dynamic scopes are bounded structural identifiers,
not arbitrary narrative.

The only optional evidence fields are explicit typed scalars:

- counts and generations;
- request/provider usage and remaining budgets;
- reset, evidence, start, completion, update, and expiry timestamps;
- age and configured freshness threshold seconds;
- encryption schema/progress integers.

There are no arbitrary dictionaries, free-form details, secret/path fields, or
raw/decrypted text fields.

Checks independently cover:

- broker paper mode;
- startup loopback transport and TLS evidence;
- macOS Keychain provider and successful-load time evidence;
- sensitive encryption schema/migration state from safe columns, anchored to
  the successful startup envelope scan without rereading ciphertext;
- request budgets for every registered limit class and their resets;
- selected-provider daily calls/input/output usage, remaining budget, and UTC
  reset;
- webhook disabled;
- Composio disabled;
- quarantine counts;
- circuit breakers by scope and generation only;
- daemon heartbeat freshness;
- startup reconciliation state, generations, and timestamps by direct column
  reads only;
- quote evidence as `unknown/quote_evidence_unavailable`;
- runtime tenure by role, state, generation, and safe timestamps only;
- unsafe local order, fill, rule, and rule-group counts;
- uncertain mutation-interlock count.

## SELECT-only inspection

Add non-mutating inspection APIs:

- `DurableRateLimiter.inspect_pair(...)`;
- `ProviderBudgetService.inspect(...)`;
- `SensitiveEncryptionPostureInspector.inspect(...)`.

Neither API opens `BEGIN IMMEDIATE`, commits, releases, sweeps, latches, or
updates state. Provider inspection must report expired `started` and `unknown`
reservations as unresolved evidence while leaving their rows and budget-day
latch unchanged. `ProviderBudgetService.status()` remains mutating and is
never called by posture.

The posture reader uses only local config, immutable startup evidence, and
local SQLite SELECTs. The encryption reader selects no active key ID,
registered ciphertext field, backup hash, or decrypted value. It never calls
the startup encryption inspector, envelope scanner, or cipher. It never calls
`StartupReconciliationGate.posture()` because that method decrypts a failure
narrative. It selects only reconciliation status, generation counters, and
timestamps, and shares one pure safe-column validator with
`StartupReconciliationGate.is_current()`.

If the durable store is unavailable, every DB-derived check is `unknown` with
a stable code while config/startup checks remain independently reportable.

## Redaction boundary

Serialized posture must never contain:

- exception text;
- filesystem paths or certificate/private-key names;
- secret names, values, presence, account names, or key IDs;
- breaker or reconciliation reasons, actors, request IDs, or evidence JSON;
- hashes, prompts, tool calls, raw external text, or decrypted fields;
- provider API URLs or query strings.

No failure path copies database narrative into `detail_code`; only a fixed
enum member is emitted.

## Route policy

Register exactly:

```python
RoutePolicy(
    "GET",
    "/security/posture",
    AuthLevel.SESSION,
    "session_read",
    lease_free_bounded_read=True,
)
```

The route requires an authenticated operator session. Durable
`session_read` `RateWindow` accounting is the only expected middleware
mutation. The explicit capability bypasses `ConcurrencyLease` acquisition for
this bounded read only; every other route retains the default concurrency
behavior. Direct posture aggregation itself is idempotent and leaves every
domain and policy table byte-for-byte unchanged.

## TDD and verification

Write tests first using temp file-backed SQLite, fixed clocks, and fakes that
raise on any broker/provider/notifier/Keychain access. Preserve the exact RED
command/output in the report.

Focused coverage includes:

- route absence and exact response contract;
- strict frozen/extra-forbid models, exact scalar types, and
  exact-bool `Literal[False]`;
- one startup secret load and zero route-time provider loads;
- sealed receipt provenance, identity binding, public composition rejection,
  and canonical guard-check completeness;
- unavailable Keychain and invalid TLS evidence;
- startup mixed-encryption rejection plus route-time safe-column inspection
  with zero scanner/decrypt calls;
- exhausted request/provider budgets and expired unresolved provider usage;
- stale daemon and reconciliation evidence;
- authoritative reconciliation differential cases for corrupt/future rows;
- fixed-category breaker aggregation with no target serialization;
- complete known order/fill/rule/rule-group state-domain validation;
- tripped breakers and runtime tenure;
- unavailable quote evidence;
- unsafe local state and uncertain interlocks;
- whole-table before/after snapshots;
- repeated/concurrent service and HTTP reads;
- DB failure with independent config/startup evidence;
- redaction markers in every forbidden narrative category;
- route inventory, authentication, and `session_read` rate limiting.

After focused green:

1. review the complete Task 10 diff;
2. run exactly one `uv run pytest`;
3. run `uv run python scripts/check_release_safety.py`;
4. commit production/tests as the implementation commit;
5. generate the bounded review diff;
6. commit this brief, report, review diff, plan checkboxes, and progress ledger
   separately as evidence.

Nothing is pushed. No app, daemon, MCP server, real Keychain, runtime
`trading_assistant.db`, broker/provider/notifier, or network endpoint is
started or accessed.

## Completion checkpoint

- Implementation commit:
  `ab9f82a31b3b0a106b0700070e1c4e6033f358e7`
- Exact focused gate: `135 passed in 12.16s`
- Sole full suite:
  `3262 passed, 1 skipped, 1 warning in 244.20s`
- Release static gate: `release static checks: PASS`
- Review package:
  `review-3a8904a..ab9f82a.diff` (3,314 lines / 112,060 bytes)

All Task 10 checks are complete at the implementation gate. Task 11 remains
untouched.

## Fix round 1 checkpoint

Fresh review findings were addressed in implementation commit
`8e553d4ab1bf20e99bbfa41594f1d8d8733b0c0e`:

- posture alone uses authenticated/rate-limited lease-free bounded-read
  middleware behavior and never mutates `ConcurrencyLease`;
- route-time encryption inspection is safe-column-only and non-decrypting;
- reconciliation authority and posture share one pure validator;
- every counted order/fill/rule/rule-group state domain is validated before
  a clear/unsafe count is emitted;
- startup evidence reaches production only through the sealed private
  receipt/container/app composition chain;
- breakers emit only fixed categories, aggregate counts, and generations;
- posture models reject coercive scalar values and preserve exact
  `bool(False)`.

Verification:

- Initial exact RED:
  `48 failed, 262 passed, 1 warning in 21.19s`
- Final focused gate:
  `314 passed, 1 warning in 20.23s`
- Sole full suite:
  `3297 passed, 1 skipped, 1 warning in 251.65s`
- Static gate:
  `release static checks: PASS`
- Review package:
  `review-c1f9041..8e553d4.diff` (3,403 lines / 116,945 bytes)

No Task 11, service, daemon, MCP, real Keychain/credential, ignored runtime
database, broker/provider/notifier/network, trade, reconciliation, reset, or
push action was performed.
