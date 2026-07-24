# Task 5 Report: Validate rules and lease OCO groups

Status: DONE

## Files and architecture

- Added `src/trading_assistant/rules/models.py` with strict, frozen Pydantic
  `RuleCommand`, discriminated `RuleCondition` variants, `RuleAction`,
  `RuleKind`, `RuleState`, and `RuleOutcome`. Every model uses
  `extra="forbid"`; actions enforce exact `qty` xor `notional` and exact
  limit-price shape.
- Added `src/trading_assistant/rules/repository.py` with one CAS lease per
  `RuleGroup`, owner/version guarded release and terminal claims, persisted
  trailing high-water marks, dynamic unresolved-order latching, and atomic
  winner/sibling state transitions.
- Added `src/trading_assistant/rules/application.py` as the sole runtime rule
  persistence/firing boundary. It revalidates commands, rejects every
  `pre_approved=true` command, performs provider reads and risk checks before
  the write transaction, then commits proposal creation, the group terminal
  state, winner state, and sibling cancellation atomically.
- Added `src/trading_assistant/rules/worker.py`. `RuleWorker.tick()` leases
  groups, caches one evaluation/risk quote per ticker, rejects stale data,
  evaluates only typed conditions, persists trailing HWM, creates proposals,
  and never approves or broker-submits.
- Added `RuleGroup`, `Rule.group_id`, `Rule.payload_version`, and
  `Proposal.source_rule_group_id` persistence in
  `src/trading_assistant/db/models.py`.
- Replaced Monitor rule claims/execution with direct `RuleWorker` delegation.
  The compatibility `auto_execute` argument is ignored and held false.
- Routed conditional-rule tools and plan decomposition through typed
  application-boundary validation. Plan-created rules are human-gated and
  share one stable plan group.
- Made order submission set `reconciliation_required` in the same durable
  claim transaction. Only `ReconciliationService` clears it after no linked
  `SUBMITTING`/`ACCEPTANCE_UNKNOWN` order remains.
- Reused the worker quote during risk snapshot assembly, so a firing still
  performs one quote read per ticker and no broker I/O occurs in the atomic
  SQLite write.
- Set the configuration default for bracket preference to `false`, matching
  the committed global constraint.

## TDD red evidence

Production code was untouched before both red checkpoints.

1. Exact brief red command:

   `uv run pytest tests/test_rule_models.py tests/test_rule_leases.py -v`

   Result: collection failed with 2 expected errors:
   `trading_assistant.rules` did not exist and `RuleGroup` was absent.

2. Migration/delegation/planning red command:

   `uv run pytest tests/test_migrations.py::test_rule_lease_upgrade_from_0003_maps_every_repository_legacy_shape tests/test_migrations.py::test_rule_lease_upgrade_aborts_on_unknown_active_shape tests/test_monitor.py::test_monitor_tick_delegates_only_to_rule_worker tests/test_planning.py::test_approve_decomposes_into_human_gated_typed_rules tests/test_plan_rules.py::test_oco_cancels_siblings_atomically -v`

   Result: 5 failed for the missing immutable `0004` revision, missing Monitor
   worker injection/delegation, legacy `pre_approved=true` plan rows, and the
   missing typed service/group interface. One SQL fixture bind error discovered
   in the initial attempt was corrected and rerun; the corrected unknown-active
   case then failed specifically because revision `0004` did not exist.

## Focused verification

- `uv run pytest tests/test_rule_models.py tests/test_rule_leases.py -v`
  -> `30 passed in 0.43s`.
- `uv run pytest tests/test_migrations.py -v`
  -> `7 passed in 0.50s`.
- Exact brief regression command:

  `uv run pytest tests/test_rule_models.py tests/test_rule_leases.py tests/test_rules_engine.py tests/test_plan_rules.py tests/test_monitor.py -v`

  -> `58 passed in 1.21s` after the final lease-latch fix.
- Adjacent Task 1-4 regression command:

  `uv run pytest tests/test_planning.py tests/test_service.py tests/test_hardening.py tests/test_security.py tests/test_db_models.py tests/test_reconciliation_service.py tests/test_order_submission.py tests/test_order_application.py tests/test_launch_features.py -v`

  -> `81 passed, 1 warning in 9.38s`.
- Quote/config/submission regression command:

  `uv run pytest tests/test_config.py tests/test_order_submission.py tests/test_service.py -v`

  -> `28 passed in 0.61s`.
- `uv run python -m compileall -q src tests`
  -> exit 0.
- `git diff --check`
  -> exit 0.

## Full-suite verification

The repository full suite was run exactly once, after focused tests and
self-review fixes were green:

`uv run pytest`

Result: `444 passed, 1 skipped, 2 warnings in 116.23s (0:01:56)`.

## Migration coverage

- Added immutable revision `20260724_0004` with
  `down_revision = "20260724_0003"`; revisions `0001`-`0003` were not edited.
- The migration validates every resumable (`active` or `processing`) row before
  SQLite DDL and aborts on unknown kind, condition, action, invalid numeric
  values, invalid deadline, or condition-kind mismatch.
- It maps all repository-produced legacy conditions:
  `price_below`, `price_above`, `trailing_stop_pct`, and time `{}` plus the
  persisted deadline.
- It normalizes legacy actions by adding `order_type="market"` while validating
  side, exact quantity shape, positive values, and limit-price shape.
- It creates one `legacy-plan-{plan_id}` group per plan and stable
  `legacy-rule-{id}` groups for standalone rows.
- It preserves `plan_id`, fraction, HWM, deadline, preapproval, state, and source
  JSON for unknown inactive historical rows (`payload_version=0`).
- It backfills historical proposal source groups from legacy `rule-{id}` client
  order IDs and initializes reconciliation latches for linked historical
  `SUBMITTING`/`ACCEPTANCE_UNKNOWN` orders.
- Tests exercise fresh head upgrade, populated `0003 -> 0004`, all four known
  legacy shapes, inactive unknown preservation, trace backfill, and
  unknown-active abort before DDL.

## Concurrency and crash evidence

- Two owners cannot lease the same group concurrently.
- An expired lease can be recovered only when no unresolved-acceptance latch or
  linked unresolved order exists.
- Lease release and terminal claims require the exact owner and version.
- The proposal, terminal group/winner, and sibling cancellation commit in one
  transaction; stale leases create no proposal.
- A two-thread sibling trigger records exactly one proposal, one triggered
  winner, one canceled sibling, and zero broker submissions.
- Injected crashes immediately before and after the proposal transaction both
  produce at most one proposal after restart/recovery.
- An acceptance-unknown response sets the group latch, never resubmits, and is
  cleared only after client-ID reconciliation resolves broker truth.

## Self-review findings and fixes

- Found that proposal-time risk assembly fetched a second quote; added snapshot
  quote overrides and reference-price reuse so RuleWorker obtains one quote per
  ticker.
- Found notification errors could append a second error outcome after an atomic
  commit; isolated notification failure from the committed worker outcome.
- Found command instances could theoretically be mutated after construction;
  froze all typed models and revalidate model dumps at the application boundary.
- Added defense-in-depth unresolved-order discovery directly to lease
  acquisition, not only submission-time latch setting.
- A focused test then exposed that the failed lease CAS rolled back the newly
  discovered latch. The root cause was the rollback sharing the latch/CAS
  transaction; the no-lease path now commits the latch. The exact 58-test Task 5
  suite passed after this fix.
- Added migration proposal trace backfill, inactive unknown-history
  preservation, historical unresolved-order latch initialization, finite
  numeric checks, and deadline parsing.
- Confirmed Monitor/RuleWorker contain no approval or submission call, no
  runtime producer writes raw Rule JSON, broker/provider reads precede SQLite
  writes, `auto_execute=false`, bracket preference defaults false, and unknown
  broker acceptance is never retried.

## Remaining concerns

- No open Task 5 correctness concerns.
- The full suite retains two pre-existing dependency deprecation warnings:
  `websockets.legacy` and Starlette/httpx compatibility.
