# Task 9 Report — Signed Explicit Candidate Queue

## Outcome

Task 9 was implemented in `9a9327f`. Review fix round 1 is implemented in
`9197ba8`. Review fix round 2 is implemented in `a092790`. Review fix round 3
is implemented in `8bb5ab5`. Review fix round 4 is implemented in `b6af5fe`.
Review fix round 5 is implemented in `a8dca94`. Review fix round 6 is
implemented in `7d5aeb0`. Review fix round 7 is implemented in `1ad2970`.

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

New target writes use:

```text
reserved -> completed
```

Reservation and nonce insertion are one `BEGIN IMMEDIATE` transaction. Each
new target and its completed receipt commit in one `BEGIN IMMEDIATE`
transaction. A pre-commit crash rolls back both; a post-commit crash observes a
completed receipt. `target_persisted` remains only as a compatibility recovery
state for receipts written by the earlier Task 9 implementation. Order/group
recovery keys are derived with a secret metadata-HMAC subkey, not a visible
nonce hash. Recovery trusts receipt state, validates the exact order or exactly
one rule target, and treats `reserved + target` as inconsistent.

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

## Review fix round 2

All three Important findings were addressed in implementation commit
`a092790`:

- A response that dispatches exactly the configured aggregate chat tool-call
  cap now returns the stable local `tool_call_budget_exhausted` result after
  servicing those allowed calls. It does not make another provider call.
- `OrderStateMachine.is_reachable()` computes transitive reachability over the
  existing legal transition graph. Completed receipt replay rejects the
  deserialization-only `APPROVED` state and all other unreachable/backward
  states while permitting legal forward progression.
- Candidate-created rules accept only lifecycle combinations reachable from
  the initial ACTIVE immediate/nonpreapproved state. ACTIVE, TRIGGERED, FAILED,
  and CANCELED combinations validate rule/group agreement, terminal ownership,
  lease pairing, reconciliation residue, and required version progression.
  PENDING, PROCESSING, mismatched terminal states, and terminal states without
  a version transition fail closed.
- New order/rule target persistence writes the target and `completed` receipt
  in the same `BEGIN IMMEDIATE` transaction. The test-only
  `before_target_commit` crash point proves rollback leaves one reserved
  receipt and no target; `after_target_commit` proves target plus completed
  receipt are already durable and replay exactly once.
- Compatibility-only `target_persisted` recovery validates the same immutable
  order/rule provenance, exact canonical rule payload, exactly one rule,
  pristine initial order broker/approval/submission/fill markers, pristine
  initial rule-group terminal/version/lease/reconciliation fields, and legal
  forward lifecycle progression.

### Fix-round-2 RED evidence

Production code was unchanged when this focused selector was run:

```text
uv run pytest -q \
  tests/test_agent.py::test_agent_exact_tool_budget_returns_without_second_provider_call \
  tests/test_order_state_machine.py::test_reachability_uses_the_legal_transition_graph_transitively \
  tests/test_candidate_boundary.py::test_order_crash_windows_recover_original_target_without_duplication \
  tests/test_candidate_boundary.py::test_order_crash_before_target_commit_rolls_back_target_and_completion \
  tests/test_candidate_boundary.py::test_rule_target_commit_crash_recovers_unique_group_and_rule \
  tests/test_candidate_boundary.py::test_rule_crash_before_target_commit_rolls_back_group_rule_and_completion \
  tests/test_candidate_boundary.py::test_completed_order_receipt_rejects_unreachable_legacy_approved_state \
  tests/test_candidate_boundary.py::test_completed_rule_receipt_rejects_backward_or_inconsistent_states \
  tests/test_candidate_boundary.py::test_target_persisted_order_recovery_accepts_legal_forward_progression \
  tests/test_candidate_boundary.py::test_target_persisted_initial_order_rejects_lifecycle_marker_tamper \
  tests/test_candidate_boundary.py::test_target_persisted_rule_recovery_accepts_legal_forward_progression \
  tests/test_candidate_boundary.py::test_rule_target_persisted_recovery_rejects_any_immutable_drift
21 failed, 18 passed
```

The failures were the intended behaviors: one extra provider call at the exact
cap, missing graph reachability, split target/completion commits, absent
pre-commit crash rollback, acceptance of backward/inconsistent completed
states, rejection of legal `target_persisted` forward states, and acceptance
of unexpected initial lifecycle markers.

One additional RED regression was captured after the first green slice:

```text
uv run pytest -q \
  tests/test_candidate_boundary.py::test_completed_rule_receipt_rejects_terminal_state_without_version_progress
1 failed
```

It proved that a terminal rule/group state with the initial group version was
still accepted. Requiring a durable version transition made it green.

### Fix-round-2 focused and concurrency evidence

```text
uv run pytest -q \
  tests/test_agent.py::test_agent_exact_tool_budget_returns_without_second_provider_call \
  tests/test_order_state_machine.py::test_reachability_uses_the_legal_transition_graph_transitively \
  tests/test_candidate_boundary.py::test_order_crash_windows_recover_original_target_without_duplication \
  tests/test_candidate_boundary.py::test_order_crash_before_target_commit_rolls_back_target_and_completion \
  tests/test_candidate_boundary.py::test_rule_target_commit_crash_recovers_unique_group_and_rule \
  tests/test_candidate_boundary.py::test_rule_crash_before_target_commit_rolls_back_group_rule_and_completion \
  tests/test_candidate_boundary.py::test_completed_order_receipt_rejects_unreachable_legacy_approved_state \
  tests/test_candidate_boundary.py::test_completed_rule_receipt_rejects_backward_or_inconsistent_states \
  tests/test_candidate_boundary.py::test_target_persisted_order_recovery_accepts_legal_forward_progression \
  tests/test_candidate_boundary.py::test_target_persisted_initial_order_rejects_lifecycle_marker_tamper \
  tests/test_candidate_boundary.py::test_target_persisted_rule_recovery_accepts_legal_forward_progression \
  tests/test_candidate_boundary.py::test_rule_target_persisted_recovery_rejects_any_immutable_drift
39 passed

uv run pytest tests/test_agent.py tests/test_candidate_boundary.py \
  tests/test_order_state_machine.py tests/test_route_policy.py \
  tests/test_api.py tests/test_mcp_tools.py tests/test_migrations.py \
  tests/test_order_submission.py tests/test_submission_barrier.py
591 passed, 1 warning in 91.09s

for run_index in {1..10}; do uv run pytest -q \
  tests/test_candidate_boundary.py::test_same_key_retries_and_concurrent_retries_return_exactly_one_target \
  tests/test_candidate_boundary.py::test_candidate_route_lease_still_rejects_concurrent_execution \
  tests/test_agent.py::test_concurrent_chats_share_atomic_broker_read_capacity \
  || exit 1; done
10 runs / 100 passed

git diff --check
PASS
```

### Fix-round-2 full and release gates

Exactly one no-argument full suite was run for this implementation fix round:

```text
uv run pytest
3115 passed, 1 skipped, 1 warning in 244.63s

uv run python scripts/check_release_safety.py
release static checks: PASS
```

The warning remains the existing third-party `websockets.legacy` deprecation
warning. The full suite was not rerun.

### Fix-round-2 review package

- Base: `826953c`
- Implementation: `a092790`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-826953c..a092790.diff`
- Diff size: 775 lines / 29,617 bytes

## Review fix round 3

Both Important findings from the adversarial review of `a092790` were
addressed in implementation commit `8bb5ab5`:

- Order receipt replay now requires exactly one canonical candidate-origin
  `Proposal`. Its source rule IDs are absent, plan generation is zero, TTL
  equals the relevant asset-class risk configuration, `created_at` equals the
  order creation time, and `expires_at` is exactly the configured interval
  later. The encrypted proposal reasoning is authenticated by the sensitive
  store and compared, through the signer metadata-HMAC, with the receipt-bound
  operator reason. No plaintext narrative is returned or persisted in receipt
  metadata.
- Forward order replay now validates state-specific repository invariants in
  addition to `OrderStateMachine` reachability. Approval identity/time,
  submission attempt/time, broker identity, acceptance/reconciliation state,
  version, and fill identity/quantity must be mutually producible. Accepted
  fill totals use only trusted identities; superseded legacy rows remain
  non-authoritative.
- Candidate rule lifecycle validation now matches traced repository paths.
  ACTIVE version zero has no lease; ACTIVE positive versions require a paired
  lease unless a release advanced the version to at least two. Worker
  TRIGGERED/FAILED states require the terminal rule and version at least two.
  Direct CANCELED state requires no terminal winner and version at least one.
  The linked-order reconciliation latch is accepted only for TRIGGERED.
- Legal tests use `OrderApplicationService`, `OrderRepository`,
  `RuleRepository`, `RuleWorker`, and the real cancellation application path.
  Direct database probes cover completed and compatibility
  `target_persisted` receipts.

### Fix-round-3 RED evidence

The first focused selector was run before the proposal/order/rule replay
implementation changed:

```text
uv run pytest tests/test_candidate_boundary.py -q -k \
  'order_receipt_rejects_missing_or_tampered_canonical_proposal or \
   order_receipt_rejects_impossible_advanced_lifecycle_metadata or \
   rule_receipt_rejects_worker_impossible_terminal_version_one or \
   rule_receipt_rejects_active_version_one_without_lease'
31 failed, 1 passed
```

The failures proved that completed and compatibility receipts accepted missing
or altered proposal TTL/timestamps/provenance/reasoning, impossible approval
and submission metadata, terminal rule version one, and ACTIVE version one
without a lease. After the first implementation slice, the same selector
passed 32 tests.

Two follow-up RED slices closed fill and reconciliation combinations exposed
while tracing the real repositories:

```text
uv run pytest tests/test_candidate_boundary.py -q -k \
  'accepted_fill_with_wrong_identity or filled_quantity_inconsistent'
4 failed

uv run pytest tests/test_candidate_boundary.py -q -k \
  'terminal_reconciliation_state_worker_cannot_make or \
   canceled_candidate_with_terminal_winner'
6 failed
```

The first proved accepted replay still trusted wrong-symbol/side fills and a
short final quantity. The second proved FAILED/CANCELED reconciliation residue
and a canceled terminal winner were still accepted. The unchanged selectors
passed 4 and 6 tests respectively after the minimal fixes.

### Fix-round-3 focused verification

```text
uv run pytest -o addopts='' tests/test_candidate_boundary.py
160 passed in 6.64s

uv run pytest -o addopts='' \
  tests/test_candidate_boundary.py tests/test_order_application.py \
  tests/test_order_state_machine.py tests/test_order_submission.py \
  tests/test_rule_leases.py tests/test_rule_models.py \
  tests/test_rules_engine.py tests/test_submission_barrier.py
317 passed in 41.50s

uv run pytest -o addopts='' \
  tests/test_agent.py tests/test_api.py tests/test_mcp_tools.py \
  tests/test_route_policy.py
238 passed, 1 warning in 18.66s

git diff --check
PASS

uv run python -m compileall -q \
  src/trading_assistant/security/candidates.py \
  tests/test_candidate_boundary.py
PASS
```

The candidate suite includes its permanent eight independent four-thread
same-key concurrency races. The warning is the existing third-party
`websockets.legacy` deprecation warning.

### Fix-round-3 full and release gates

Exactly one no-argument full suite was run for this implementation fix round:

```text
uv run pytest
3184 passed, 1 skipped, 1 warning in 242.38s

uv run python scripts/check_release_safety.py
release static checks: PASS
```

The full suite was not rerun.

### Fix-round-3 review package

- Base: `3ca5ed5`
- Implementation: `8bb5ab5`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-3ca5ed5..8bb5ab5.diff`
- Diff size: 1,281 lines / 46,092 bytes

## Review fix round 4

All three Important findings and the Minor finding from the adversarial review
of `8bb5ab5` were addressed in implementation commit `b6af5fe`:

- `db/lifecycle_proofs.py` is now the shared replay authority. Order, proposal,
  fill, rule, and rule-group lifecycle state is snapshotted into the existing
  row-bound encrypted `AuditEvent.detail_json` in the same transaction as each
  authoritative mutation. Replay requires an exact match to the latest proof;
  order replay additionally requires transitive reachability through
  `OrderStateMachine`.
- Approval, submission, reconciliation, direct cancellation, panic
  cancellation, rule claim/release/terminal writes, and legacy
  `approve_proposed` now emit proof only after their target mutation. A forged
  actor/time/version, direct `PROPOSED -> CANCELED`, changed broker identity,
  or direct overfill cannot acquire repository provenance.
- Real broker reconciliation to `CANCELED`, `REJECTED`, or `EXPIRED` remains
  replayable with `acceptance_state=submitted` and version four because its
  exact repository-produced proof, not a broad state whitelist, is the
  authority.
- Rule replay requires normalized nonempty lease ownership, exact paired lease
  chronology, an exact group/rule proof, and—for terminal or reconciliation
  states—the single worker-linked order and canonical `Proposal` with matching
  source group/rule IDs and action fields.
- Proposal replay no longer decrypts `Proposal.reasoning` or
  `Order.approval_reason`. Proof contains ordinary lifecycle fields and SHA-256
  digests of the encrypted envelopes only. The candidate-origin audit also
  carries the existing receipt-bound reason metadata-HMAC. The proof payload is
  itself authenticated and row-bound by the existing AES-GCM sensitive-field
  store. The threat model detects direct target-row edits and mismatched
  repository state while the audit store and encryption key remain trusted; it
  does not claim protection from an actor who can rewrite both target and audit
  rows using valid encryption keys. No schema migration was required.

### Fix-round-4 RED evidence

The first focused selector ran before production implementation changed:

```text
uv run pytest -q tests/test_candidate_boundary.py -k \
  'forged_approval_with_pending_reason or \
   direct_unevidenced_cancellation or \
   broker_identity_changed_after_submission or \
   unevidenced_trusted_overfill or \
   real_reconciled_terminal_with_submitted_acceptance or \
   never_decrypts_proposal_reasoning or \
   malformed_active_lease_chronology or \
   terminal_reconciliation_without_linked_proposal or \
   tampered_worker_linkage'
27 failed
```

The failures reproduced every listed false accept, the real reconciled
terminal false reject, missing worker linkage validation, and replay-time
sensitive prose reads in both completed and compatibility receipt modes.

Tracing the legacy approval and panic repositories exposed two more missing
authoritative write sites. Their legal-path tests were captured RED before
those sites changed:

```text
uv run pytest -q tests/test_candidate_boundary.py -k \
  'real_panic_cancellation or legacy_atomic_approval_provenance'
4 failed
```

Both unchanged selectors passed after the minimal transition-site proof writes.

### Fix-round-4 focused and adversarial evidence

```text
uv run pytest tests/test_candidate_boundary.py \
  tests/test_order_application.py tests/test_order_submission.py \
  tests/test_reconciliation_service.py tests/test_rule_leases.py \
  tests/test_rules_engine.py tests/test_submission_barrier.py \
  tests/test_api.py tests/test_mcp_tools.py \
  tests/test_sensitive_write_sites.py tests/test_sensitive_crypto.py \
  tests/test_sensitive_migration.py tests/test_order_state_machine.py \
  tests/test_startup_reconciliation.py
672 passed, 1 warning in 64.85s

for repeat in 1 2 3 4 5
do
  uv run pytest tests/test_candidate_boundary.py -k \
    'forged_approval_with_pending_reason or \
     direct_unevidenced_cancellation or \
     broker_identity_changed_after_submission or \
     unevidenced_trusted_overfill or \
     real_reconciled_terminal_with_submitted_acceptance or \
     never_decrypts_proposal_reasoning or \
     malformed_active_lease_chronology or \
     terminal_reconciliation_without_linked_proposal or \
     tampered_worker_linkage or real_panic_cancellation or \
     legacy_atomic_approval_provenance' || exit 1
done
5 runs / 155 passed

uv run pytest tests/test_sensitive_crypto.py \
  tests/test_sensitive_migration.py tests/test_sensitive_write_sites.py \
  tests/test_order_state_machine.py tests/test_startup_reconciliation.py
177 passed in 4.68s

uv run python -m py_compile \
  src/trading_assistant/db/lifecycle_proofs.py \
  src/trading_assistant/db/models.py \
  src/trading_assistant/orders/repository.py \
  src/trading_assistant/orders/reconciliation.py \
  src/trading_assistant/rules/application.py \
  src/trading_assistant/rules/repository.py \
  src/trading_assistant/security/candidates.py \
  src/trading_assistant/service.py tests/test_candidate_boundary.py
PASS

git diff --check
PASS
```

The warning is the existing third-party `websockets.legacy` deprecation
warning. `uv run ruff check ...` was attempted but Ruff is not installed in
this environment, so no Ruff result is claimed.

### Fix-round-4 full and release gates

Exactly one no-argument full suite was run for this implementation fix round:

```text
uv run pytest
3215 passed, 1 skipped, 1 warning in 251.36s

uv run python scripts/check_release_safety.py
release static checks: PASS
```

The full suite was not rerun.

### Fix-round-4 review package

- Base: `0aac06b`
- Implementation: `b6af5fe`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-0aac06b..b6af5fe.diff`
- Diff size: 1,835 lines / 62,269 bytes

## Review fix round 5

Both Important proof-path findings and the Minor plan-reconciliation finding
were addressed in implementation commit `a8dca94`:

- Reconciliation's transaction-local flush listener now collects every parent
  order affected by a `Fill` insert, promotion, supersession, deletion,
  reassignment, or reconciliation-state change. The final audit phase writes
  one `order.fill_reconcile` proof per affected parent unless a simultaneous
  authoritative order audit already snapshots the same final fill set.
  Fill/cursor/proof failure rolls the entire transaction back.
- Terminal late fills now refresh the candidate order proof even when the
  terminal order is excluded from status sync. That proof remains committed
  if a later reconciliation phase crashes or a later status dependency fails,
  so completed and compatibility `target_persisted` receipt replay sees the
  exact new fill set.
- `RuleRepository.lease_group` validates an aware datetime before opening a
  mutation path, normalizes it to UTC, requires it to be at or after both
  durable group timestamps, and requires expiry strictly after the sample.
  Invalid/backdated time can neither set the unresolved-acceptance latch nor
  create proof. A worker whose pre-lock sample is overtaken by another
  serialized writer treats the typed chronology rejection as a lost lease and
  evaluates nothing.
- Rule persistence accepts an optional aware timestamp and uses it for the
  new group, rule, and creation audits. Candidate queueing passes its trusted
  queue time, and fixed-clock tests can create a chronologically valid group
  without weakening the repository check.
- Plan fill reconciliation now changes
  `RuleGroup.reconciliation_required` through a shared proof-producing helper.
  Both the untrusted-fill latch and the terminal-late-residual path therefore
  retain exact latest group proof. Audit failure rolls the group changes back.
- No model or migration changed. The one edit in
  `tests/test_release_gate_branches.py` only injects that test's existing
  fixed clock into rule creation; no Task 10 production interface or behavior
  was implemented.

### Fix-round-5 RED evidence

An initial multi-file command mistakenly supplied more than one `-k` option,
so pytest honored only the last planning selector. It still produced the
expected planning RED, and each intended selector was then rerun explicitly
before production changed:

```text
uv run pytest tests/test_candidate_boundary.py -k \
  'terminal_late_fill_refreshes_one_complete_parent_order_proof or \
   late_fill_parent_proof_survives_later_reconciliation_phase_failure or \
   rule_receipt_backdated_lease_is_rejected_before_mutation_or_proof'
8 failed, 191 deselected in 0.86s

uv run pytest tests/test_rule_leases.py -k \
  'lease_group_rejects_non_aware_or_invalid_now_before_mutation or \
   lease_group_rejects_now_before_group_creation or \
   backdated_lease_cannot_set_reconciliation_latch_before_rejection or \
   lease_group_accepts_equal_time_and_normalizes_offset_to_utc'
5 failed, 14 deselected in 0.24s

uv run pytest tests/test_planning.py -k \
  'untrusted_plan_fill_refreshes_every_changed_group_proof_atomically'
1 failed, 64 deselected in 0.22s

uv run pytest tests/test_reconciliation_service.py -k \
  'each_fill_mutation_batch_refreshes_one_complete_parent_order_proof'
4 failed, 106 deselected in 0.80s
```

The failures proved that every tested fill mutation had zero parent batch
proofs, lease time was neither validated nor normalized before mutation, and
plan reconciliation emitted no group proof.

### Fix-round-5 focused and adversarial evidence

The corrected new selectors passed 8 candidate cases, 5 lease cases, 4 fill
mutation classes, and the plan proof case. Two broad focused runs then exposed
test-clock assumptions: one acceptance-recovery test reused a timestamp older
than the reconciliation write, and one worker/cancel race sampled time before
waiting for the serialized writer. The first now leases at the durable
recovery timestamp; the second exercises the production fail-closed lost-lease
behavior.

Final focused evidence:

```text
uv run pytest tests/test_candidate_boundary.py \
  tests/test_order_application.py tests/test_order_submission.py \
  tests/test_reconciliation_service.py tests/test_rule_leases.py \
  tests/test_rules_engine.py tests/test_submission_barrier.py \
  tests/test_api.py tests/test_mcp_tools.py \
  tests/test_sensitive_write_sites.py tests/test_sensitive_crypto.py \
  tests/test_sensitive_migration.py tests/test_order_state_machine.py \
  tests/test_startup_reconciliation.py tests/test_planning.py
754 passed, 1 warning in 70.04s

uv run pytest tests/test_monitor.py tests/test_plan_rules.py \
  tests/test_rule_models.py
78 passed, 1 warning in 2.35s

for repeat in 1 2 3 4 5; do
  uv run pytest -q tests/test_candidate_boundary.py \
    tests/test_reconciliation_service.py tests/test_rule_leases.py \
    tests/test_planning.py -k \
    'terminal_late_fill_refreshes_one_complete_parent_order_proof or
     late_fill_parent_proof_survives_later_reconciliation_phase_failure or
     rule_receipt_backdated_lease_is_rejected_before_mutation_or_proof or
     each_fill_mutation_batch_refreshes_one_complete_parent_order_proof or
     lease_group_rejects_non_aware_or_invalid_now_before_mutation or
     lease_group_rejects_now_before_group_creation or
     backdated_lease_cannot_set_reconciliation_latch_before_rejection or
     lease_group_accepts_equal_time_and_normalizes_offset_to_utc or
     untrusted_plan_fill_refreshes_every_changed_group_proof_atomically or
     worker_and_plan_cancellation_commit_one_coherent_group_state' || exit 1
done
95 passed

uv run pytest \
  tests/test_reconciliation_service.py::test_fill_and_cursor_mutations_roll_back_when_audit_flush_fails
2 passed, 1 warning

uv run pytest \
  tests/test_planning.py::test_untrusted_plan_fill_refreshes_every_changed_group_proof_atomically
2 passed

uv run python -m py_compile \
  src/trading_assistant/orders/reconciliation.py \
  src/trading_assistant/rules/application.py \
  src/trading_assistant/rules/repository.py \
  src/trading_assistant/rules/worker.py \
  src/trading_assistant/security/candidates.py \
  tests/test_candidate_boundary.py tests/test_planning.py \
  tests/test_reconciliation_service.py \
  tests/test_release_gate_branches.py tests/test_rule_leases.py
PASS

git diff --check
PASS
```

The warning is the existing third-party `websockets.legacy` deprecation
warning.

### Fix-round-5 full and release gates

Exactly one no-argument full suite was run:

```text
uv run pytest
3231 passed, 4 failed, 1 skipped, 1 warning in 249.02s
```

All four failures were in `tests/test_release_gate_branches.py`. Its
`_group_with_rule` helper used fixed `NOW=2026-07-26T18:00:00Z` for leasing but
created the group through wall-clock defaults on July 28. The new chronology
check correctly rejected that impossible ordering. The helper now passes its
fixed `NOW` through the injectable rule-persistence clock.

Post-correction focused confirmation:

```text
uv run pytest tests/test_release_gate_branches.py -k \
  'rule_loader_refuses_unknown_persisted_payload_version or
   stale_rule_release_is_a_noop or
   terminal_claim_refuses_nonterminal_state_and_wrong_winner or
   terminal_claim_rolls_back_if_audit_cannot_commit'
4 passed, 32 deselected

uv run pytest tests/test_rules_engine.py tests/test_rule_leases.py -k \
  'create_rule or lease_group'
4 passed, 19 deselected

uv run pytest tests/test_release_gate_branches.py \
  tests/test_rule_leases.py tests/test_rules_engine.py tests/test_monitor.py
96 passed, 1 warning in 4.32s

uv run python scripts/check_release_safety.py
release static checks: PASS
```

The full suite was not rerun because the round explicitly permits exactly one
full run. This report therefore does not claim a post-fix green no-argument
suite.

### Fix-round-5 review package

- Base: `b7c5be6`
- Implementation: `a8dca94`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-b7c5be6..a8dca94.diff`
- Diff size: 1,317 lines / 45,746 bytes

## Review fix round 6

The one strict-UTC lease-boundary finding was addressed in implementation
commit `7d5aeb0`:

- `RuleRepository.lease_group` now accepts only caller samples whose
  `utcoffset()` is exactly `timedelta(0)`. Positive, negative, absent, and
  exception-raising offsets produce one stable fail-closed `ValueError`.
- Caller validation still runs before `self.session_factory()` is invoked.
  Rejected samples therefore cannot read or mutate a group, write an audit, or
  create lifecycle proof.
- Persisted timestamp handling uses a separate internal normalizer. A
  SQLite-naive value is interpreted as persisted UTC, while a valid aware
  persisted value is converted to UTC. This compatibility does not weaken the
  strict caller boundary.
- Exact aware-UTC equality remains accepted. The round-5 created/updated
  monotonic chronology, worker lost-lease handling, and fixed-clock behavior
  are unchanged.
- No model, migration, route, candidate, broker, provider, daemon, or Task 10
  production surface changed.

### Fix-round-6 RED evidence

The focused regressions were run before the implementation edit:

```text
uv run pytest -q \
  tests/test_rule_leases.py::test_lease_group_rejects_non_utc_now_before_db_open_or_mutation \
  tests/test_rule_leases.py::test_lease_group_accepts_utc_equal_time \
  tests/test_rule_leases.py::test_persisted_rule_group_timestamps_normalize_as_utc_internally
4 failed, 1 passed
```

Both valid nonzero offsets acquired a lease, the malformed offset exposed the
old permissive validation contract, and the separate persisted-timestamp
normalizer did not exist.

### Fix-round-6 focused verification

```text
uv run pytest -q \
  tests/test_rule_leases.py::test_lease_group_rejects_non_utc_now_before_db_open_or_mutation \
  tests/test_rule_leases.py::test_lease_group_accepts_utc_equal_time \
  tests/test_rule_leases.py::test_persisted_rule_group_timestamps_normalize_as_utc_internally \
  tests/test_rule_leases.py::test_lease_group_rejects_non_aware_or_invalid_now_before_mutation \
  tests/test_rule_leases.py::test_lease_group_rejects_now_before_group_creation
8 passed

uv run pytest -q tests/test_rule_models.py tests/test_rule_leases.py \
  tests/test_rules_engine.py tests/test_plan_rules.py \
  tests/test_candidate_boundary.py tests/test_planning.py \
  tests/test_monitor.py tests/test_release_gate_branches.py
406 passed, 1 warning

git diff --check
PASS
```

The warning is the existing third-party `websockets.legacy` deprecation
warning.

### Fix-round-6 full and release gates

Exactly one no-argument full suite was run for this round:

```text
uv run pytest
3239 passed, 1 skipped, 1 warning in 248.98s

uv run python scripts/check_release_safety.py
release static checks: PASS
```

This fresh green no-argument run closes the fix-round-5 post-correction
full-suite caveat. It was not retried or rerun.

### Fix-round-6 review package

- Base: `d1fb3c5`
- Implementation: `7d5aeb0`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-d1fb3c5..7d5aeb0.diff`
- Diff size: 191 lines / 6,564 bytes

## Review fix round 7

The deceptive-`timedelta` lease-offset finding was addressed in implementation
commit `1ad2970`:

- The caller offset must have exact type `datetime.timedelta`; every subclass
  is rejected before `self.session_factory()` can be invoked.
- Exact base offsets must have zero days, seconds, and microseconds and compare
  exactly equal to `timedelta(0)`. Offset retrieval and verification exceptions
  converge on the stable fail-closed UTC validation error.
- Regression offsets independently lie through equality/inequality, component
  attributes, and `total_seconds()`. A fourth raises if comparison occurs.
  Every case is rejected with zero repository session access and no group,
  audit, or lifecycle-proof mutation.
- Standard `timezone.utc` and `ZoneInfo("UTC")` remain accepted at the exact
  durable group timestamp. Persisted-naive normalization remains isolated in
  the internal persisted timestamp normalizer.
- No model, migration, route, candidate, broker, provider, daemon, or Task 10
  production surface changed.

### Fix-round-7 RED evidence

The expanded no-I/O boundary regression was run before production changed:

```text
uv run pytest -q \
  tests/test_rule_leases.py::test_lease_group_rejects_non_utc_now_before_db_open_or_mutation \
  tests/test_rule_leases.py::test_lease_group_accepts_utc_equal_time
4 failed, 5 passed
```

Three nonzero subclasses opened the repository path because their overloaded
inequality returned false. The fourth leaked its comparison `RuntimeError`
instead of the stable fail-closed validation error. Both real UTC
implementations passed.

### Fix-round-7 focused verification

```text
uv run pytest -q \
  tests/test_rule_leases.py::test_lease_group_rejects_non_utc_now_before_db_open_or_mutation \
  tests/test_rule_leases.py::test_lease_group_accepts_utc_equal_time \
  tests/test_rule_leases.py::test_persisted_rule_group_timestamps_normalize_as_utc_internally \
  tests/test_rule_leases.py::test_lease_group_rejects_non_aware_or_invalid_now_before_mutation \
  tests/test_rule_leases.py::test_lease_group_rejects_now_before_group_creation
13 passed

git diff --check
PASS

uv run pytest -q tests/test_rule_models.py tests/test_rule_leases.py \
  tests/test_rules_engine.py tests/test_plan_rules.py \
  tests/test_candidate_boundary.py tests/test_planning.py \
  tests/test_monitor.py tests/test_release_gate_branches.py
411 passed, 1 warning
```

The warning is the existing third-party `websockets.legacy` deprecation
warning.

### Fix-round-7 full, static, and clean gates

Exactly one no-argument full suite was run for this round:

```text
uv run pytest
3244 passed, 1 skipped, 1 warning in 249.32s

uv run python scripts/check_release_safety.py
release static checks: PASS

git status --porcelain
(no output after the implementation commit)
```

The full suite was not retried or rerun. A final clean-worktree check is also
required after the evidence commit.

### Fix-round-7 review package

- Base: `91cc8d6`
- Implementation: `1ad2970`
- Diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-91cc8d6..1ad2970.diff`
- Diff size: 165 lines / 4,738 bytes

## Safety statement

All tests used temporary SQLite databases and deterministic fakes. No app,
daemon, MCP server, broker runtime, provider client, notification client, or
network stream was started. No real credential or Keychain content was read,
used, logged, or stored. The ignored runtime `trading_assistant.db` was not
touched. No breaker was reset. No real/runtime order was approved, submitted,
canceled, or executed. Focused tests exercised synthetic approval/submission
state transitions and synthetic rule cancellation only in temporary SQLite
through deterministic fakes; they made zero broker submission/cancellation
calls. No live/autonomous trading was enabled. Nothing was pushed.

The implementation remains Alpaca PAPER-only and preserves manual approval,
execution-time broker truth, risk authority, circuit breakers, and existing
equity/crypto behavior.

## Remaining concerns

- Ruff is unavailable in the current environment, so no Ruff-specific result
  is claimed.
- The schema retains `target_persisted` for fail-closed recovery of receipts
  written before fix round 2. New code never writes that intermediate state;
  removing it later requires operational proof that no compatible rows remain.
- Encrypted-field rotation changes ciphertext envelopes. Because lifecycle
  proof deliberately binds ciphertext digests and rotation does not rewrite
  domain lifecycle proof, a post-rotation candidate replay conservatively fails
  closed rather than trusting changed ciphertext. A separately authorized proof
  refresh would be needed if replay availability across rotation is required.
- The fix-round-4 review package is prepared but has not yet received a fresh
  independent review, so this report claims implementation-gate completion,
  not independent-review closure.
- The fix-round-5 through fix-round-7 review packages have not yet received a
  fresh independent review. The green no-argument suites are not independent
  review.
- Candidate queueing intentionally does not make profitability claims and does
  not weaken the separate recent-auth approval boundary.
