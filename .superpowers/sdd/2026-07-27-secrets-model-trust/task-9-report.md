# Task 9 Report — Signed Explicit Candidate Queue

## Outcome

Task 9 is implemented in commit `9a9327f`.

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
`candidate_queue_receipts` and extends the protected mutation-operation
constraint with `candidate_queue`.

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
