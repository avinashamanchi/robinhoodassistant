### Task 9: Make chat read-only and queue only signed explicit candidates

## Scope

General chat may read data and draft signed candidates. It cannot mutate
orders or rules. Only the explicit, authenticated candidate queue endpoints may
consume a signed candidate, refresh broker truth, run full deterministic risk,
and persist a reviewable order or standing rule.

MCP keeps its existing explicit, authenticated, non-executing contract. This
task does not start any runtime, contact a provider, approve/cancel/submit an
order, reset a breaker, or enable live/autonomous trading.

## Files

- Create `src/trading_assistant/security/candidates.py`.
- Create migration `20260728_0016_candidate_queue_receipts.py`.
- Modify agent, app, policy, limits, bootstrap, and database models.
- Add `tests/test_candidate_boundary.py`.
- Update agent, API, route-policy, migration, MCP, and submission tests.

## Candidate and signing boundary

- Strict frozen Pydantic models: `OrderCandidate`, `RuleCandidate`,
  `SignedCandidate`, and `AgentReply(reply, candidates<=4)`.
- Exactly one of quantity/notional; exact limit-price consistency; finite,
  positive, bounded decimals.
- Model-supplied decimal strings are canonical fixed-point. Trusted typed
  broker `Decimal` values may contain insignificant trailing zeroes and are
  normalized before canonical signing.
- Tickers are canonical and allowlisted. Quotes are fresh server quotes.
  Timestamps are aware UTC. Envelope TTL is at most five minutes.
- Nonce, signature, and binding are strict 32-byte unpadded base64url.
- HMAC-SHA256 covers canonical JSON. Signing, opaque session binding, and
  receipt metadata use domain-separated subkeys derived from
  `RuntimeSecrets.candidate_signing_key`; verification uses `compare_digest`.
- Session binding includes actor, current session identity, and authentication
  epoch inside the HMAC. Envelopes and receipts contain no raw token or raw
  database session ID.
- `RuleCandidate` intentionally has no `proposal_ttl_minutes`. Triggered
  proposals use the relevant asset-class risk configuration TTL.

## Chat boundary

- `READ_ONLY_TOOL_SPECS` is one immutable literal tuple.
- Allowed names are read-only market/account/order/rule queries plus
  `draft_order_candidate` and `draft_rule_candidate`.
- `propose_order`, `create_conditional_rule`, and `cancel_rule` are absent.
  Unknown names return only `unknown_tool`.
- Draft arguments omit reference price, quote time, actor, binding, issue/expiry
  time, nonce, and signature. Drafts perform only bounded quote reads and
  signing and never write the database.
- The signed envelope is collected locally for `AgentReply`; the model receives
  only a stable drafted status, not the signed bearer envelope.
- Prose alone never creates a candidate.
- `/chat` is marked broker-read and remains rate-limited before model/broker
  work.
- `provider_budget.max_chat_tool_turns` is also the hard total tool-call budget
  across all model turns. Every broker-backed dispatch consumes one durable
  `broker_read` unit under the exact authenticated-session limit principal
  before broker work. Budget exhaustion, rate denial, or store failure stops
  remaining dispatches and prevents another provider call. Reaching the exact
  aggregate cap after an otherwise valid tool response also terminates locally;
  it never buys one more provider turn.

## Explicit queue boundary

- `POST /candidates/order/queue` and `POST /candidates/rule/queue` require
  session authentication, CSRF, a canonical idempotency key, mutation audit,
  broker-read policy, and principal-scoped concurrency policy.
- Candidate queue routes retain their route lease but do not claim the generic
  mutation interlock. `CandidateQueueReceipt` is their sole durable
  idempotency and crash-recovery authority.
- A new attempt validates signature, kind, actor, current-session binding,
  envelope time, execution-grade quote freshness, allowlist, and static cap
  before reserving a receipt or consuming the nonce.
- After reservation, provider reads occur outside every SQLite write
  transaction. The queue obtains complete broker truth through
  `PortfolioSnapshotService.assemble_for_confirmation` and runs the full risk
  engine without degraded fallback.
- Order queueing persists only `PROPOSED` or `REJECTED`. It cannot approve,
  submit, cancel, or execute.
- Rule queueing persists exactly one ACTIVE `activation="immediate"` standing
  rule with `pre_approved=False`. A trigger still creates only a pending
  proposal using the asset-class risk TTL; it never auto-executes.
- Rule risk rejection, warnings, breaker trips, and audit outcomes persist
  durably.

## Durable idempotency and recovery

- `CandidateQueueReceipt` stores only hashes, lifecycle metadata, original
  request ID, safe outcome/status, and target ID. It never stores candidate
  thesis, operator narrative, raw idempotency key, session token, or signature.
- Receipt identity is current-session hash + kind + idempotency-key hash and is
  bound to candidate, nonce, actor, and reason hashes.
- Same key + same candidate + same reason replays/recoverably completes the
  original result. Same key with changed candidate or reason is an idempotency
  conflict. The same signed nonce under another key is `candidate_replayed`.
- Receipt reservation and nonce insertion are atomic under
  `BEGIN IMMEDIATE` and unique constraints.
- New writes use `reserved -> completed`: the target and completed receipt
  commit in the same transaction. `target_persisted` remains accepted only as
  a compatibility/recovery state for receipts written before fix round 2.
- Order idempotency and rule group keys are deterministic secret-HMAC-derived
  values. Recovery never searches for a target from a visible raw nonce hash.
  It trusts only receipt state and validates exact order fields or exactly one
  matching rule target.
- `target_persisted -> completed` compatibility recovery validates pristine
  initial lifecycle fingerprints or a legally reachable forward lifecycle,
  plus all immutable target fields. A completed same-request replay validates
  immutable provenance, transitive order reachability under the existing
  `OrderStateMachine`, and consistent rule/group lifecycle combinations.
  Initial orders have no broker-order, approval, submission, reconciliation,
  cancellation, version, or fill markers. Initial active rule groups have the
  expected zero version, no terminal owner, no lease, and no reconciliation
  residue. Rule recovery reconstructs the canonical `RuleCommand` and compares
  ticker, group, kind, canonical condition/action JSON, sizing/limit fields,
  activation, preapproval, terminal behavior, fraction/HWM/deadline, plan
  ownership, payload version, and exactly one rule.
- Completed and target-persisted receipts may replay after envelope expiry.
  Reserved receipts must revalidate issue/expiry/quote age before resuming; a
  stale retry becomes a terminal exact-status receipt and creates no target.
- Expiry is exclusive: `observed >= expires_at` is expired.
- Terminal retries replay the original HTTP status.

## Required evidence

- RED captured before implementation: focused candidate tests failed because
  `trading_assistant.security.candidates` did not exist.
- Candidate tests cover strict/extra/duplicate input, decimal/base64
  canonicalization, future/quote timestamps, reauthentication/actor/kind
  mismatch, stale quote before envelope expiry, reason binding, rate denial,
  fresh full-risk snapshot, rule safety evidence, crash windows, target
  integrity, exact terminal status, atomic target/completed receipt rollback,
  legal-forward and backward lifecycle replay, same-key concurrency, and nonce
  replay.
- Agent tests prove immutable read-only specs, no mutable names, stable unknown
  errors, prose-only behavior, local candidate collection, bearer-envelope
  withholding, four-candidate cap before a fifth quote read, and exact aggregate
  tool-cap termination without an extra provider call.
- API/policy tests prove CSRF/idempotency, broker-read classification, duplicate
  JSON rejection, pre-work rate denial, and zero approve/submit/cancel.
- Migration tests prove head 0016, metadata-only receipt schema, constraints,
  downgrade refusal with durable receipts, and prior lock-failure behavior.
- MCP and existing equity/crypto/rule/submission tests remain green.
- Run focused tests first, adversarial concurrency repeats, then exactly one
  full `uv run pytest` for this implementation round.
- Run `scripts/check_release_safety.py` because Task 9 changes agent, route,
  migration, and safety-enforced surfaces.
- Commit implementation/tests first. Then create the review diff and update the
  plan, task report, brief, and progress evidence in a second docs-only commit.
