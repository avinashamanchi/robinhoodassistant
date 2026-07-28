### Task 9: Make chat read-only and queue only signed explicit candidates

## Scope

General chat may read data and draft signed candidates. It cannot mutate
orders or rules. Only the explicit, authenticated candidate queue endpoints may
consume a signed candidate, refresh broker truth, run full deterministic risk,
and persist a reviewable order or standing rule.

MCP keeps its existing explicit, authenticated, non-executing contract. This
task does not start any runtime, contact a provider, approve/cancel/submit any
real/runtime order, reset a breaker, or enable live/autonomous trading.
Lifecycle tests may exercise synthetic state transitions in temporary SQLite
through deterministic fakes, with zero broker submission/cancellation calls.

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
- Candidate-origin orders retain exactly one canonical proposal. Proposal
  source fields and plan generation are empty/zero, TTL comes from the current
  asset-class risk configuration, creation equals order creation, expiry is
  exactly the configured interval later, and the candidate-origin audit binds
  the receipt reason metadata-HMAC. Replay never decrypts proposal reasoning
  or approval prose; it compares ordinary metadata and encrypted-envelope
  digests inside the row-bound encrypted audit proof. Missing, duplicate,
  altered, or cryptographically inconsistent proposal state fails closed.
- Forward order replay requires exact repository-produced lifecycle proof,
  not only a path in the status graph or a broad metadata whitelist.
  Approval, submission, reconciliation, cancellation, broker identity, error
  state, version, proposal, and authoritative fills are captured in the same
  transaction as each mutation and must match the latest proof.
- Candidate rule lifecycle validation uses the same proof authority. Lease
  ownership and chronology must match a real claim/release. Terminal and
  reconciliation states require the exact linked order/proposal persisted by
  the worker, and direct/panic cancellation proofs are written only after
  their target mutations.
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

## Review fix round 3 evidence contract

- Capture RED for proposal deletion/TTL/timestamp/reason tampering, impossible
  approval/submission/fill metadata, terminal version one, invalid active
  version/lease combinations, invalid reconciliation residue, and canceled
  terminal ownership in both completed and compatibility receipt states.
- Drive accepted order progress through `OrderApplicationService` and
  `OrderRepository`; drive accepted rule progress through real lease, release,
  worker trigger/failure, direct cancellation, and linked-order latch paths.
- Run focused candidate/order/rule/submission and agent/API/MCP/policy tests,
  then exactly one no-argument full suite and the release static gate.
- Use temporary SQLite and deterministic fakes only. Do not start services,
  contact providers, read Keychain content, touch the ignored runtime database,
  trade, reset a breaker, push, or begin Task 10.

## Review fix round 4 evidence contract

- Capture RED in both completed and compatibility receipt modes for forged
  approval metadata, direct cancellation, changed broker identity,
  unevidenced overfill, malformed lease chronology, missing/tampered
  worker-linked proposal provenance, and replay-time sensitive prose reads.
- Drive accepted approval, submission, reconciliation, terminal order,
  worker-trigger, direct cancellation, lease, and panic-cancellation paths
  through the real application/repository authorities.
- Use one shared durable proof validator whose evidence is written atomically
  at transition sites. Do not replace the prior broad whitelist with another
  broad whitelist.
- Run focused candidate/order/rule/reconciliation/API/MCP/sensitive-field
  tests, adversarial repeats, exactly one no-argument full suite, and the
  release static gate.
- Use temporary SQLite and deterministic fakes only. Do not start services,
  contact providers, read Keychain content, touch the ignored runtime database,
  trade, reset a breaker, push, or begin Task 10.

## Review fix round 5 evidence contract

- Every reconciliation write that changes an order's fill snapshot must
  produce one complete parent-order proof in the same transaction. This covers
  insert, promotion, supersession, deletion, and reconciliation-state changes,
  including a terminal order excluded from status sync and a failure in a
  later reconciliation phase.
- Parent proof creation is coalesced per affected order. If that proof cannot
  flush, the fill, cursor, and audit writes roll back together. A same-
  transaction authoritative order proof may subsume the dedicated fill-batch
  proof because it snapshots the same final fill set.
- `lease_group` rejects non-datetime, naive, invalid, and durable-state-
  backdated samples before a reconciliation latch, lease, or audit can be
  written. Valid time is normalized to UTC, must be at or after both group
  timestamps, and must produce an expiry strictly later than the sample.
- Rule persistence accepts an injected aware timestamp so candidate and
  fixed-clock tests create the group, rule, and audit evidence on the same
  chronology. A worker sample overtaken while waiting for the serialized
  writer lock is treated as a lost lease and causes no evaluation.
- Plan fill reconciliation changes a group's reconciliation latch only through
  the shared proof-producing mutation helper. Proof failure must roll the group
  write back, and every changed group must retain an exact latest proof.
- Run RED selectors for all three defects, the broad Task 9/reconciliation/
  rule/planning focused set, repeated adversarial selectors, exactly one
  no-argument full suite, then compile/diff and release-static checks. If the
  one full suite exposes a fixture defect, correct and verify it with focused
  tests only; do not conceal the result or run a second full suite.
- Use temporary SQLite and deterministic fakes only. Do not start services,
  contact providers, read Keychain content, touch the ignored runtime database,
  trade, reset a breaker, push, or begin Task 10.

## Review fix round 6 evidence contract

- `RuleRepository.lease_group` accepts only a datetime whose `utcoffset()` is
  exactly zero. Positive, negative, absent, invalid, or exception-raising
  offsets fail closed before the repository opens a database session and
  therefore cannot mutate a group or write audit/lifecycle proof.
- Strict caller validation is separate from persisted timestamp
  normalization. A SQLite-naive group timestamp is interpreted as persisted
  UTC internally; a valid aware persisted timestamp may be converted to UTC.
  Neither behavior permits a non-UTC caller sample.
- Preserve exact UTC equality, fixed-clock worker/daemon behavior, and the
  monotonic group-created/group-updated chronology from fix round 5.
- Capture RED for the nonzero/malformed cases, pass focused rule/candidate/
  worker/daemon tests, then run exactly one no-argument full suite and the
  release static gate.
- Use temporary SQLite and deterministic fakes only. Do not start services,
  contact providers, read Keychain content, touch the ignored runtime database,
  trade, reset a breaker, push, or begin Task 10.

## Review fix round 7 evidence contract

- The caller-side lease offset must have exact type `datetime.timedelta`.
  Subclasses are rejected before any database session opens, even if they lie
  through equality, inequality, component attributes, `total_seconds()`, or
  exception-raising comparisons.
- An exact base offset must have zero days, seconds, and microseconds and equal
  `timedelta(0)`. Offset lookup or verification exceptions fail closed behind
  the stable UTC validation error.
- Standard `timezone.utc` and `ZoneInfo("UTC")` remain accepted. Persisted
  SQLite-naive timestamp normalization stays a separate internal trust path,
  and fixed-clock exact equality remains valid.
- Capture RED for the deceptive subclasses, pass focused lease/rule/worker/
  candidate tests, then run exactly one no-argument full suite, the release
  static gate, and explicit clean-worktree checks.
- Use temporary SQLite and deterministic fakes only. Do not start services,
  contact providers, read Keychain content, touch the ignored runtime database,
  trade, reset a breaker, push, or begin Task 10.
