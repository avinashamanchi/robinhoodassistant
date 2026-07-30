# Task 9 Report: Gate backtests, panic, and daemon provider reads

## Status

Completed on `codex/safety-foundation` from required inherited HEAD
`ba4c2b2`.

Commit subject:

```text
feat(runtime): bound backtests panic and provider reads
```

## Inherited patch audit

The replacement implementation began with 16 modified, uncommitted files from
an unresponsive worker. The complete diff was read before changes, then traced
through the Task 6 route-policy lease, mutation-interlock, and panic-receipt
code.

The inherited implementation already provided:

- the durable global `backtest:global` lease with a 1,500-second TTL;
- the configured 1,200-second monotonic deadline and cooperative stop event;
- stable `409 backtest_busy`, `422 backtest_bounds_exceeded`, and
  `504 backtest_timed_out` responses;
- persisted `timed_out` backtest rows and audit evidence;
- bar, strategy, primary optional-LLM, and spot-check cancellation checks;
- the `panic:alpaca-paper` account lease with a 90-second TTL;
- Task 6 durable panic receipts fenced by lease owner and generation;
- one-call panic coalescing and bounded follower waits;
- per-attempt durable scheduled market-data allowances using principal
  `provider:alpaca:market-data`;
- direct, unthrottled execution-time quote reads.

The initial focused baseline passed 48 tests, but the audit found three
load-bearing gaps not exposed by those tests:

1. A real process crash leaves both an expired lease and an exact active Task 6
   interlock. The inherited recovery test seeded only the lease, while the
   production path would remain blocked indefinitely.
2. Cancellation was checked before the whole synthetic data build, but not
   before each symbol data load or between replay data-access calls.
3. A denied or unavailable durable scheduled-read allowance made zero broker
   calls, but did not enter the durable asset-class data-breaker path.

## Implementation

### Backtests

- Raw request bounds are checked before lease acquisition and runner/data use.
  More than 20 symbols or more than 3,000 inclusive calendar days returns the
  stable bounds error. Exact 20-symbol and 3,000-day requests are allowed.
- The route acquires only `backtest:global`, with the exact 1,500-second TTL,
  before runner construction. A second caller receives `backtest_busy` and
  starts no runner.
- An active backtest interlock may be cleared only in one immediate
  transaction when the lease and interlock have the exact same owner and
  generation, the lease is expired, the operation is `backtest`, the
  interlock is still `active`, and no worker completion is recorded.
  Uncertain, live, mismatched, or non-backtest interlocks remain fail-closed.
- The request passes a 1,200-second monotonic deadline and one
  `threading.Event` through the synchronous runner. No detached worker is
  created.
- Cancellation is checked before each synthetic symbol data load, replay data
  access, bar, strategy call, primary optional LLM attempt, and optional
  spot-check LLM attempt.
- Deadline expiry sets the event, unwinds synchronously, persists one
  `status="timed_out"` run plus a `timed_out` audit event, and returns only
  after exact lease/interlock cleanup.

### Panic

- Panic continues to use Task 6's account-scoped `panic:alpaca-paper` lease and
  exact owner/generation receipt contract; no competing coalescer was added to
  `OperationsService`.
- The owner atomically writes `started` before `service.panic()`. Followers
  accept only the receipt matching the observed lease owner and generation.
- Concurrent callers return the exact same durable JSON result while
  `service.panic()` is invoked once.
- Owner exceptions persist `state="failed"` with `response_json IS NULL`;
  followers receive `503 panic_incomplete` and no exception text.
- Follower polling is bounded by the configured request timeout.

### Scheduled market-data reads

- Only daemon/rule/shadow read-only quote paths use the durable provider-read
  gate. Each retry consumes a fresh allowance before broker I/O.
- Budget denial and policy-store unavailability both cause zero quote calls,
  release the rule-group lease, and durably trip the relevant asset-class data
  breaker.
- Execution-time quote assembly still calls the broker directly once; it is
  neither retried nor converted into permission by the scheduled-read gate.
- No order-submission method is wrapped by the generic retry/limit helper.

## Strict TDD evidence

### RED

The first inherited-patch regressions were added and run before repair:

```bash
uv run pytest \
  tests/test_backtests_api.py::test_expired_backtest_lease_is_reclaimed_with_exact_fence \
  tests/test_backtests_api.py::test_backtest_cancellation_checks_before_each_data_load \
  tests/test_monitor.py::test_scheduled_market_data_denial_makes_zero_broker_calls \
  -v
```

Result: `3 failed in 1.03s`.

Separate fail-closed and replay-data regressions also failed before their
production changes:

```text
store-unavailable scheduled read: 1 failed in 0.23s
cancel before next replay data access: 1 failed in 0.52s
```

### Targeted GREEN

The repaired crash-recovery, per-load cancellation, and denied-read cases
passed `3/3`; both denial/store-unavailable breaker cases passed `2/2`; and
the data/bar/strategy cancellation cases passed `3/3`.

## Required verification

Focused command from the brief:

```bash
uv run pytest tests/test_backtests_api.py tests/test_ops.py \
  tests/test_monitor.py -v
```

Result:

```text
51 passed in 42.84s
```

Exact expanded command from the brief:

```bash
uv run pytest tests/test_backtests_api.py tests/test_ops.py \
  tests/test_monitor.py tests/test_submission_barrier.py \
  tests/stress/test_stress_scenarios.py -v
```

Result:

```text
85 passed in 86.59s
```

Adjacent lease, Task 6 panic, replay, optional-LLM, and execution-read
regression command:

```bash
uv run pytest tests/test_route_policy.py tests/test_durable_limits.py \
  tests/test_backtest_engine.py tests/test_llm_runner.py \
  tests/test_execution_risk_snapshot.py::test_required_execution_quote_failure_raises_typed_dependency \
  -v
```

Result:

```text
173 passed, 1 warning in 40.04s
```

The warning is the existing third-party `websockets.legacy` deprecation
warning.

Compilation:

```bash
uv run python -m compileall -q src tests
```

Result: exit `0`, no output.

## Changed files

Production:

- `src/trading_assistant/app/limits.py`
- `src/trading_assistant/app/main.py`
- `src/trading_assistant/app/policy.py`
- `src/trading_assistant/backtest/engine.py`
- `src/trading_assistant/backtest/evaluate.py`
- `src/trading_assistant/backtest/llm_runner.py`
- `src/trading_assistant/backtest/runner.py`
- `src/trading_assistant/bootstrap.py`
- `src/trading_assistant/daemon/backoff.py`
- `src/trading_assistant/daemon/main.py`
- `src/trading_assistant/rules/worker.py`

Tests:

- `tests/test_backtest_engine.py`
- `tests/test_backtests_api.py`
- `tests/test_execution_risk_snapshot.py`
- `tests/test_llm_runner.py`
- `tests/test_monitor.py`
- `tests/test_ops.py`

Provenance:

- `.superpowers/sdd/2026-07-27-policy-budget-foundation/task-9-report.md`

## Self-review

- Every Task 9 exact value is explicit and covered: 1,500-second global
  backtest lease, 1,200-second maximum runtime, 20 symbols, 3,000 inclusive
  calendar days, 90-second panic lease/receipt, and the exact scheduled-read
  principal.
- Backtest crash recovery is exact-fence only. Uncertain work is not
  auto-cleared, and a replacement cannot reclaim a live or mismatched lease.
- The runner is synchronous and cooperative. Timeout persistence occurs before
  lease release, and no replay/provider work continues after the API timeout
  response.
- Panic preserves Task 6's owner/generation receipt authority and invokes the
  emergency service once for concurrent callers.
- Scheduled quote denial and store failure are fail-closed, make no quote call,
  and enter a durable data-breaker path.
- Submission-time quote reads remain outside the scheduled limiter and generic
  retry helper.
- No ledger, migration, release-gate, runbook, or later-task file was edited.
- Verification used fakes, captures, temporary SQLite databases, and mock
  brokers only. No provider, external broker, notification, order
  submit/cancel, breaker reset, app start, or daemon start occurred.

## Residual concern

None within Task 9 scope. The scheduled market-data breaker intentionally
requires manual, separately authorized recovery; this task adds no reset path.

## Fix Round 1

### Reviewer findings addressed

1. `BacktestRunner` now checks the monotonic deadline again after
   `persist_report()`. If persistence crosses the deadline, it atomically
   changes that exact persisted run and its exact audit event to `timed_out`
   in one immediate transaction before the API policy releases the durable
   lease. It does not create a duplicate run or success audit.
2. API `start_date` and `end_date` now reach source construction and
   walk-forward evaluation. Synthetic replay creates one daily bar per
   requested inclusive calendar day, and evaluation filters its timeline to
   the same inclusive bounds before deriving development/holdout windows.
   Engine views remain causally bounded at each replay timestamp.
3. The actual `_build_monitor` shadow quote closure now treats scheduled-read
   denial and limiter-store unavailability as stale-data failures and trips
   the shared durable asset-class data breaker before returning `None`.
   Both paths make zero `broker.get_quote()` calls. Execution-time quote reads
   were not changed.

### TDD evidence

Focused RED command, run before production repair:

```bash
uv run pytest \
  tests/test_backtests_api.py::test_backtest_bounds_allow_exact_symbol_and_inclusive_day_ceilings \
  tests/test_backtests_api.py::test_backtest_deadline_crossing_during_persistence_reconciles_same_run \
  tests/test_backtest_engine.py::test_synthetic_source_uses_requested_dates_inclusively \
  tests/test_backtest_engine.py::test_replay_date_window_is_inclusive_without_future_bars \
  tests/test_monitor.py::test_daemon_shadow_quote_denial_uses_durable_data_breaker \
  -v
```

RED result:

```text
5 failed, 1 passed in 1.49s
```

The failures demonstrated the intended gaps: API dates were not forwarded,
source construction did not accept the date window, persistence crossing the
deadline returned success, and the two real daemon shadow-denial variants did
not persist a data breaker. The pre-existing engine window seam passed.

Focused GREEN command (unchanged from RED):

```bash
uv run pytest \
  tests/test_backtests_api.py::test_backtest_bounds_allow_exact_symbol_and_inclusive_day_ceilings \
  tests/test_backtests_api.py::test_backtest_deadline_crossing_during_persistence_reconciles_same_run \
  tests/test_backtest_engine.py::test_synthetic_source_uses_requested_dates_inclusively \
  tests/test_backtest_engine.py::test_replay_date_window_is_inclusive_without_future_bars \
  tests/test_monitor.py::test_daemon_shadow_quote_denial_uses_durable_data_breaker \
  -v
```

GREEN result:

```text
6 passed in 1.13s
```

Direct walk-forward inclusive-window coverage:

```bash
uv run pytest \
  tests/test_backtest_evaluate.py::test_walk_forward_honors_requested_window_inclusively \
  -v
```

Result:

```text
1 passed in 0.46s
```

### Covering verification

Current-state backtest, monitor, and ops covering command:

```bash
uv run pytest tests/test_backtests_api.py tests/test_backtest_engine.py \
  tests/test_backtest_evaluate.py tests/test_monitor.py tests/test_ops.py -v
```

Result:

```text
75 passed in 84.94s
```

Exact expanded Task 9 command from the brief:

```bash
uv run pytest tests/test_backtests_api.py tests/test_ops.py \
  tests/test_monitor.py tests/test_submission_barrier.py \
  tests/stress/test_stress_scenarios.py -v
```

Result:

```text
88 passed in 85.83s
```

Daemon-budget composition and execution-time quote regression command:

```bash
uv run pytest \
  tests/test_bootstrap.py::test_daemon_shadow_uses_shared_analysis_budget_and_attempt_ceiling \
  tests/test_execution_risk_snapshot.py::test_required_execution_quote_failure_raises_typed_dependency \
  -v
```

Result:

```text
3 passed, 1 warning in 1.12s
```

The warning is the existing third-party `websockets.legacy` deprecation
warning.

Final static checks:

```bash
uv run python -m compileall -q src tests
git diff --check
```

Result: both exited `0` with no output.

### Fix Round 1 changed files

Production:

- `src/trading_assistant/app/main.py`
- `src/trading_assistant/backtest/evaluate.py`
- `src/trading_assistant/backtest/runner.py`
- `src/trading_assistant/daemon/backoff.py`
- `src/trading_assistant/daemon/main.py`
- `src/trading_assistant/rules/worker.py`

Tests:

- `tests/test_backtest_engine.py`
- `tests/test_backtest_evaluate.py`
- `tests/test_backtests_api.py`
- `tests/test_monitor.py`

Provenance:

- `.superpowers/sdd/2026-07-27-policy-budget-foundation/task-9-report.md`

### Fix Round 1 self-review

- The post-persistence timeout path updates the same `BacktestRun` and exact
  `backtest.run` audit identified by run ID and request ID. The run/audit
  transition is one transaction and occurs before control returns to the
  lease-owning API policy.
- Date bounds are validated before source access, govern generated bar count,
  and constrain walk-forward windows inclusively. A 1-day request now replays
  one daily bar; a 3,000-day request replays 3,000 daily bars per symbol.
- Cancellation checks remain before source/data access, every replay bar and
  strategy call, and the optional LLM call. The new post-persistence check
  closes the final success-return gap without detached work.
- The daemon shadow quote closure uses the same durable stale-data breaker
  transition as rule-worker scheduled reads. Denial/store-unavailable paths
  call the limiter once and the broker zero times.
- No execution submission quote path, order submission/cancellation, panic
  receipt, breaker reset, app/daemon startup, external provider, broker, or
  notification behavior was invoked or changed.
- No ledger, migration, runbook, release-gate, or later-task file was edited.

### Fix Round 1 residual concern

None within the reviewer findings. As before, durable data-breaker recovery is
intentionally outside Task 9 and requires separately authorized manual action.
