# Task 9 Report — Signed Explicit Candidate Queue

## Outcome

Task 9 was implemented in `9a9327f`. Review fix round 1 is implemented in
`9197ba8`.

General chat now has one immutable read-only tool registry plus two
non-persisting draft tools. It cannot propose, create, cancel, approve, submit,
or execute. Drafts return strict signed candidates locally through
`AgentReply`; the model sees only a stable drafted status and never receives the
signed bearer envelope.

The explicit candidate queue is CSRF- and idempotency-protected. It validates
the current actor/session/authentication epoch, signed envelope, TTL, signed
quote freshness, allowlist, and static cap; refreshes complete broker truth;
runs the full deterministic risk engine; and persists only:

- an order in `PROPOSED` or `REJECTED`; or
- one ACTIVE `activation="immediate"`, non-preapproved standing rule.

A triggered candidate-created rule was proved to create only a pending
proposal. No queue path can approve, submit, cancel, execute, or auto-execute.

## State-integrity design

Migration `20260728_0016` adds metadata-only
`candidate_queue_receipts`. Candidate queue routes retain the generic
principal-scoped route lease but do not claim a generic mutation interlock;
the receipt lifecycle is their sole durable recovery authority.

Receipt identity binds opaque current-session hash, kind, hashed idempotency
key, candidate hash, nonce hash, actor hash, and operator-reason hash. It stores
the original request ID, lifecycle, safe outcome/status, and target ID only. It
does not store raw session tokens, database session IDs, idempotency keys,
candidate thesis, operator narrative, or signatures.

The lifecycle is:

```text
reserved -> target_persisted -> completed
```

Reservation and nonce insertion are one `BEGIN IMMEDIATE` transaction. Target
and `target_persisted` receipt state commit atomically. Order/group recovery
keys are derived with a secret metadata-HMAC subkey, not a visible nonce hash.
Recovery trusts receipt state, validates the exact order or exactly one rule
target, and treats `reserved + target` as inconsistent.

New attempts validate time/freshness/static policy before nonce consumption.
Reserved retries revalidate and safely terminalize if the candidate or quote
has expired. Completed or target-persisted same-key retries replay the original
result after expiry. Changed candidate or reason under the same key conflicts;
the nonce under another key replays as `candidate_replayed`. Terminal receipts
replay the original HTTP status.

## Cryptographic and schema boundary

- Strict frozen Pydantic models reject extra fields.
- Exactly one size form and exact limit-price consistency are enforced.
- Model strings require canonical fixed-point decimals.
- Trusted typed broker `Decimal` values normalize insignificant trailing zeroes
  before canonical signing.
- All times are aware UTC and envelope TTL is at most five minutes.
- Nonce, signature, and session binding are strict unpadded 32-byte base64url.
- HMAC-SHA256 uses domain-separated signing/session/metadata subkeys derived
  from `RuntimeSecrets.candidate_signing_key`.
- Signature and binding comparisons use `hmac.compare_digest`.
- `RuleCandidate` omits the unenforceable `proposal_ttl_minutes`; triggered
  proposals use the asset-class risk configuration TTL.

## RED evidence

The new focused boundary test file was run before implementation:

```text
uv run pytest tests/test_candidate_boundary.py -q
30 failed
```

The failures were the intended missing-module boundary:
`trading_assistant.security.candidates` did not exist.

## Focused verification

Commands and results:

```text
uv run pytest tests/test_candidate_boundary.py -q
49 passed

uv run pytest tests/test_agent.py -q
21 passed

uv run pytest tests/test_migrations.py -q
157 passed

uv run pytest tests/test_db_models.py tests/test_rule_models.py \
  tests/test_plan_rules.py tests/test_monitor.py -q
84 passed, 1 warning

uv run pytest tests/test_candidate_boundary.py tests/test_agent.py \
  tests/test_route_policy.py tests/test_api.py tests/test_mcp_tools.py \
  tests/test_submission_barrier.py tests/test_order_submission.py \
  tests/test_asset_class.py tests/test_risk_engine.py \
  tests/test_killswitch.py tests/test_breakers.py \
  tests/test_rule_models.py tests/test_plan_rules.py \
  tests/test_monitor.py tests/test_db_models.py -q
500 passed, 1 warning
```

The same-key concurrency test is permanently parameterized for eight
independent four-thread races. The final candidate run passed all eight races.

Additional checks:

```text
uv run python -m compileall -q src/trading_assistant \
  migrations/versions tests/test_candidate_boundary.py tests/test_agent.py \
  tests/test_api.py tests/test_migrations.py tests/test_route_policy.py
PASS

git diff --check
PASS

uv run python scripts/check_release_safety.py
release static checks: PASS
```

`uv run ruff check ...` could not run because `ruff` is not installed in this
project environment. The project has no configured Ruff section. Compile,
focused tests, full tests, release-static checks, and diff checks all passed.

## Full-suite verification

Exactly one full suite was run for this implementation round:

```text
uv run pytest
3063 passed, 1 skipped, 1 warning in 236.12s
```

The single warning is the existing `websockets.legacy` deprecation warning.
The full suite was not rerun.

## Review package

- Base: `4f2a866`
- Implementation: `9a9327f`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-4f2a866..9a9327f.diff`
- Diff size: 5,285 lines / 188,100 bytes

## Review fix round 1

All four Important findings and the Minor expiry-boundary finding were
addressed in implementation commit `9197ba8`:

- A single chat can dispatch at most
  `security.provider_budget.max_chat_tool_turns` tool blocks in total across
  all model turns. Each actual broker-backed tool consumes one durable
  `broker_read` unit under the same canonical authenticated-session principal
  used by route policy. Exhaustion, denial, and store failure return stable
  bounded results for the current response, stop remaining dispatches, and
  prevent another provider turn.
- Candidate queue policies are explicitly receipt-managed. They still require
  CSRF, idempotency, mutation audit, rate policy, broker-read classification,
  and a principal-scoped route lease, but they do not inspect, claim, settle,
  or widen the generic mutation interlock. The unused `candidate_queue`
  interlock operation and migration constraint widening were removed.
- `target_persisted -> completed` validates the exact initial target state.
  Completed same-request replay validates immutable provenance but no longer
  rejects legal order/rule lifecycle progression.
- Rule recovery reconstructs the canonical `RuleCommand` and compares the
  deterministic group key, exactly one target rule, payload version, ticker,
  kind, canonical condition/action JSON, size and limit fields, activation,
  preapproval, terminal behavior, fraction, HWM, deadline, and plan ownership.
  Immutable drift fails both recovery and completed replay.
- Candidate expiry now treats `observed >= expires_at` as expired.

### Fix-round RED evidence

The first focused command contained one mistyped migration-test selector and
collected no tests. The selector was corrected before implementation. The
corrected RED run produced:

```text
46 failed, 8 passed in 2.49s
```

Those failures reproduced the missing Agent limiter interface, unbounded
fan-out, lifecycle-sensitive completed replay, incomplete rule provenance
checks, generic candidate interlock, retained migration widening, and exact
expiry boundary.

### Fix-round focused and concurrency evidence

```text
uv run pytest tests/test_agent.py
28 passed in 1.88s

focused receipt/replay/interlock selection
23 passed in 1.00s

focused route-policy and migration selection
3 passed in 0.38s

uv run pytest tests/test_agent.py tests/test_candidate_boundary.py \
  tests/test_route_policy.py tests/test_api.py tests/test_mcp_tools.py \
  tests/test_order_submission.py tests/test_submission_barrier.py
374 passed, 1 warning in 56.33s

uv run pytest tests/test_config.py tests/test_bootstrap.py \
  tests/test_migrations.py
282 passed, 1 warning in 22.83s

10 repeated runs of concurrent chat rate limiting, eight four-thread \
same-key receipt races, and candidate route-lease contention
100 passed

uv run pytest \
  tests/test_candidate_boundary.py::test_completed_rule_receipt_replays_after_trigger_lifecycle_progression
2 passed in 0.30s

uv run python -m compileall -q src/trading_assistant \
  migrations/versions tests/test_agent.py tests/test_api.py \
  tests/test_candidate_boundary.py tests/test_migrations.py \
  tests/test_route_policy.py
PASS

git diff --check
PASS
```

One initial broad focused run exposed an expected API context assertion that
needed the new canonical `limit_principal`; the corrected broad run above
passed. The first adversarial repeat exposed a test-only scheduling assumption
that one concurrent chat must finish before the other. The assertion was
changed to accept both valid interleavings while preserving the exact durable
capacity and zero-extra-call checks; the fresh 100-case repeat then passed.

### Fix-round full and release gates

Exactly one no-argument full suite was run for this fix round:

```text
uv run pytest
3095 passed, 1 skipped, 1 warning in 238.86s

uv run python scripts/check_release_safety.py
release static checks: PASS
```

The warning remains the existing `websockets.legacy` deprecation warning. The
full suite was not rerun.

### Fix-round review package

- Base: `352e9e6`
- Implementation: `9197ba8`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-352e9e6..9197ba8.diff`
- Diff size: 1,636 lines / 56,923 bytes

## Safety statement

All tests used temporary SQLite databases and deterministic fakes. No app,
daemon, MCP server, broker runtime, provider client, notification client, or
network stream was started. No real credential or Keychain content was read,
used, logged, or stored. The ignored runtime `trading_assistant.db` was not
touched. No breaker was reset. No order was approved, submitted, canceled, or
executed. No live/autonomous trading was enabled. Nothing was pushed.

The implementation remains Alpaca PAPER-only and preserves manual approval,
execution-time broker truth, risk authority, circuit breakers, and existing
equity/crypto behavior.

## Remaining concerns

- Ruff is unavailable in the current environment, so no Ruff-specific result
  is claimed.
- Candidate queueing intentionally does not make profitability claims and does
  not weaken the separate recent-auth approval boundary.
