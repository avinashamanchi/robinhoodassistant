# Task 5 Report: Shared policy-budget runtime composition

## Status

Completed on `codex/safety-foundation` from required HEAD `fa18386`.

Commit subject:

```text
refactor(runtime): share policy budget services
```

## Implementation

- `ApplicationContainer` now owns exactly one `DurableRateLimiter`, one
  `ConcurrencyLeaseService`, and one `ProviderBudgetService`.
- `_build_container()` constructs those services consecutively from the exact
  shared `session_factory`, before broker/provider composition, and passes the
  same instances to app state, `OperationsService`, `RuleWorker`, and daemon
  `Monitor` consumers without activating later route-policy behavior.
- The limiter, lease service, and provider-budget service expose read-only
  `session_factory` identity properties.
- Strict provider-budget configuration is converted without dropping any hard
  ceiling: calls, input tokens, output tokens, reservation TTL, and the complete
  validated price metadata mapping are passed to `ProviderBudgetService`.
- `build_llm_backend()` now requires keyword-only `provider_budget` and
  `category`, accepts exactly `chat`, `analysis`, `untrusted`, or `backtest`,
  resolves the estimator before raw construction, and wraps the selected raw
  delegate exactly once.
- Disabled `backtest` construction returns a no-delegate backend whose
  `create()` raises `ProviderBudgetExceeded`; category and feature-flag checks
  happen before estimator resolution or raw provider construction.
- Agent and Analyst no longer have independent literal attempt defaults.
  Container-built Agent uses `max_chat_tool_turns`; all production Analyst
  instances use `max_structured_attempts`.
- Agent tool turns and Analyst structured attempts propagate stable request
  IDs into every decorated provider attempt. Planning propagates its boundary
  request ID; standalone analyst/backtest calls generate a non-empty ID.
- Automatic app planning now requires the shared `ApplicationContainer`, so
  explicit runtime secrets cannot recreate an unbudgeted planning backend.

## Production caller inventory

- `src/trading_assistant/app/main.py::_build_agent`
  - category: `chat`
  - budget: `container.provider_budget`
- `src/trading_assistant/app/main.py::_create_app` automatic planning
  - category: `analysis`
  - budget: `container.provider_budget`
- `src/trading_assistant/daemon/main.py::_build_monitor` shadow analysis
  - category: `analysis`
  - budget: `container.provider_budget`
- `src/trading_assistant/validate_analyst.py::_build_analyst`
  - category: `analysis`
  - budget: the process's single durable service built from the validation
    database runtime
- The current backtest module accepts an injected `Analyst` and contains no
  direct `build_llm_backend()` caller. The factory's `backtest` construction
  boundary is covered for both disabled zero-construction and enabled
  exactly-once wrapping.
- No MCP or other production source file calls `build_llm_backend()`.

## Strict TDD evidence

### Initial container/factory RED

Command:

```bash
uv run pytest tests/test_bootstrap.py tests/test_factory.py -v
```

Result:

```text
16 failed, 19 passed, 1 warning
```

The failures were the intended missing Task 5 contracts: absent container/app
service identities, unsupported required factory keywords, missing caller
categories, missing validation helper, and no disabled-backtest backend.

### Request-ID and configured-ceiling RED

Command:

```bash
uv run pytest tests/test_agent.py tests/test_analyst.py tests/test_analyst_v2.py tests/test_planning.py::test_planning_passes_boundary_request_id_to_each_structured_attempt -v
```

Result:

```text
20 failed, 4 passed
```

The failures showed Agent omitted the request ID, Analyst had no configurable
attempt ceiling, and Planning omitted its boundary request ID.

### Invalid-category edge RED

Command:

```bash
uv run pytest 'tests/test_factory.py::test_llm_factory_rejects_non_allowlisted_category_before_construction[category4]' -v
```

Result: one expected failure because an unhashable non-string category escaped
through an incidental `TypeError`. The minimal fix made all invalid category
shapes use the stable fail-closed `ValueError` before estimator/raw
construction.

## GREEN verification

Task brief command:

```bash
uv run pytest tests/test_bootstrap.py tests/test_factory.py tests/test_llm_backends.py -v
```

Result:

```text
54 passed, 1 warning in 4.95s
```

The broader impacted set also exited `0`:

```bash
uv run pytest tests/test_llm_budget.py tests/test_durable_limits.py \
  tests/test_agent.py tests/test_analyst.py tests/test_analyst_v2.py \
  tests/test_llm_runner.py tests/test_news.py tests/test_launch_features.py \
  tests/test_planning.py tests/test_monitor.py tests/test_plans_api.py \
  tests/test_task9_round2.py -q
```

Final full suite, run once after focused GREEN and self-review:

```bash
uv run pytest
```

Result:

```text
1807 passed, 1 skipped, 1 warning in 246.26s (0:04:06)
```

The warning is the pre-existing `websockets.legacy` deprecation warning.

Additional verification:

```text
git diff --check
```

Exited `0` with no output.

## Changed files

Production:

- `src/trading_assistant/bootstrap.py`
- `src/trading_assistant/app/limits.py`
- `src/trading_assistant/app/main.py`
- `src/trading_assistant/app/agent.py`
- `src/trading_assistant/llm/factory.py`
- `src/trading_assistant/llm/budget.py`
- `src/trading_assistant/analyst/analyst.py`
- `src/trading_assistant/analyst/planning.py`
- `src/trading_assistant/daemon/main.py`
- `src/trading_assistant/daemon/monitor.py`
- `src/trading_assistant/operations/service.py`
- `src/trading_assistant/rules/worker.py`
- `src/trading_assistant/validate_analyst.py`

Tests:

- `tests/test_bootstrap.py`
- `tests/test_factory.py`
- `tests/test_llm_backends.py`
- `tests/test_llm_budget.py`
- `tests/test_agent.py`
- `tests/test_analyst.py`
- `tests/test_analyst_v2.py`
- `tests/test_planning.py`
- `tests/test_launch_features.py`
- `tests/test_llm_runner.py`
- `tests/test_news.py`
- `tests/test_plans_api.py`
- `tests/test_task9_round2.py`

## Self-review

- Every production factory caller supplies the exact shared budget and literal
  allowlisted category; there is no optional/unbudgeted factory fallback.
- Unknown, blank, non-string, disabled-backtest, missing-estimator, fallback,
  and budget-denial paths all stop before raw provider construction.
- Enabled categories select one raw delegate and place exactly one
  `BudgetedLLMBackend` around it.
- App chat and app planning share the same budget object; daemon consumers
  share the daemon container's same limiter, lease, and budget objects.
- The config conversion preserves all four `BudgetLimits` values and passes the
  original validated price mapping.
- No second chat-turn or structured-attempt ceiling remains in production
  constructors or module constants.
- No Task 6 route policy or Task 9 operational behavior was activated.
- No production app/daemon process, real broker/provider/LLM call,
  notification, breaker reset, or order action was performed.

## Concerns

None.

## Fix round 1

### Status

Completed the focused corrections from `task-5-review-1.md`. This section
supersedes the original report's statements that validation used `analysis`
and that standalone Analyst/backtest calls could generate request IDs.

### Corrections

- Removed Analyst's `uuid4()` fallback. Omitted, `None`, blank, and
  whitespace-only request IDs now fail before either Analyst method invokes
  its backend. A supplied ID is normalized once and reused unchanged across
  every configured structured retry.
- Added a required, validated `run_id` to `AnalystStrategy`,
  `run_llm_backtest()`, and `analyst_accuracy()`.
- Each triggered, uncached decision now uses
  `backtest:{run_id}:{symbol}:{timestamp}` when it fits the durable
  64-character request-ID field. Longer material is represented by a stable
  `backtest:`-prefixed SHA-256 form capped at 64 characters.
- Cache hits make no additional provider attempt. Full-model spot checks reuse
  the exact parent decision request ID. Replaying the same run ID, symbol, and
  timestamp reproduces the same request ID.
- `validate_analyst._build_analyst()` now selects `category="backtest"` with
  the exact supplied `ProviderBudgetService`.
- Validation's default-off path returns the factory's disabled no-delegate
  backend before estimator or raw SDK construction. Explicit enablement
  constructs one raw delegate and wraps it in exactly one
  `BudgetedLLMBackend`.
- Validation derives a deterministic, bounded `validation:` run ID from the
  sorted symbols, holdout window, analyst version, and LLM run configuration,
  and passes it to `analyst_accuracy()`.

### Caller inventory

- HTTP planning and daemon shadow paths retain their existing caller-supplied
  request identities through `PlanningService`.
- `AnalystStrategy` is the sole production adapter that directly invokes
  `Analyst.analyze()` for backtests; it now supplies the deterministic decision
  ID to both the selected analyst and optional spot-check analyst.
- `run_llm_backtest()` requires and forwards an explicit run ID.
- `analyst_accuracy()` requires a run ID before replay and forwards it to every
  per-symbol strategy.
- `validate_analyst.run()` is the sole production `analyst_accuracy()` caller;
  it supplies the deterministic validation run ID.
- `validate_analyst._build_analyst()` remains the sole validation factory
  caller and now uses the shared budget with category `backtest`.
- All affected test callers were migrated to explicit request or run IDs;
  the missing-ID cases are intentional zero-call regression tests.

### Strict TDD evidence

Initial provenance/composition RED:

```bash
uv run pytest -q \
  tests/test_analyst.py::test_analyst_rejects_missing_request_id_before_backend \
  tests/test_llm_runner.py::test_strategy_reuses_deterministic_request_id_across_cache_and_replay \
  tests/test_llm_runner.py::test_strategy_bounds_request_id_and_rejects_blank_run_before_replay \
  tests/test_bootstrap.py::test_validation_analyst_default_off_uses_disabled_backtest_without_construction \
  tests/test_bootstrap.py::test_validation_analyst_enabled_wraps_backtest_exactly_once
```

Result:

```text
8 failed, 6 passed, 1 warning
```

The failures showed omitted/`None` IDs reaching Analyst's backend, no replay
`run_id` contract, validation constructing an analysis delegate while
backtests were disabled, and the enabled validator wrapper carrying the wrong
category.

Entry-point/validator identity RED:

```bash
uv run pytest -q \
  tests/test_llm_runner.py::test_run_llm_backtest_rejects_blank_run_id_before_replay \
  tests/test_launch_features.py::test_accuracy_rejects_blank_run_id_before_replay \
  tests/test_bootstrap.py::test_validation_run_passes_deterministic_bounded_identity
```

Result:

```text
3 failed, 1 warning
```

The two replay APIs rejected the new keyword as unsupported, and validation
called `analyst_accuracy()` without a run ID.

### GREEN verification

Focused affected suite:

```bash
uv run pytest -q \
  tests/test_analyst.py tests/test_analyst_v2.py tests/test_news.py \
  tests/test_llm_runner.py tests/test_launch_features.py \
  tests/test_bootstrap.py tests/test_factory.py tests/test_llm_budget.py
```

Result:

```text
127 passed, 1 warning
```

Final full suite, run once after focused GREEN and self-review:

```bash
uv run pytest -q
```

Result:

```text
1823 passed, 1 skipped, 1 warning
```

The warning remains the pre-existing `websockets.legacy` deprecation warning.
`git diff --check` also exited `0` with no output.

### Changed files

Production:

- `src/trading_assistant/analyst/analyst.py`
- `src/trading_assistant/backtest/llm_runner.py`
- `src/trading_assistant/analyst/accuracy.py`
- `src/trading_assistant/validate_analyst.py`

Tests:

- `tests/test_analyst.py`
- `tests/test_analyst_v2.py`
- `tests/test_news.py`
- `tests/test_llm_runner.py`
- `tests/test_launch_features.py`
- `tests/test_bootstrap.py`

### Self-review

- Mutating validator category back to `analysis`, constructing a disabled raw
  delegate, wrapping an enabled delegate twice, dropping run validation,
  changing replay IDs, duplicating cache calls, or changing the spot-check ID
  is covered by the focused regressions.
- All production `build_llm_backend()` callers still require the shared
  provider budget and an exact allowlisted category.
- The fix does not alter HTTP/planning/shadow identity generation, provider
  denial ordering, configured structured-attempt ceilings, or any Task 6/9
  behavior.
- No production app/daemon process, broker/provider/LLM call, notification,
  breaker reset, or order action was performed.

### Concerns

None.
