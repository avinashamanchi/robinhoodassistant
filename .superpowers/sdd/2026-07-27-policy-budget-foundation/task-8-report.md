# Task 8 Report: Stable identity for every LLM provider attempt

## Status

Completed on `codex/safety-foundation` from required HEAD `33926dd`.

Commit subject:

```text
refactor(llm): identify every provider attempt
```

## Existing Task 5 provenance preserved

Task 5 had already implemented most of Task 8:

- Agent tool turns passed one parent request ID to each backend call.
- Analyst structured repair attempts reused one normalized parent request ID.
- Planning forwarded its HTTP/audit request ID to `Analyst.analyze_plan()`.
- The budget decorator created and reconciled one durable reservation per
  provider attempt, without deduplicating attempts that shared a request ID.
- Backtest decisions used the canonical 52-character `backtest:` plus full
  SHA-256 URL-safe Base64 identity derived from normalized run, symbol, and UTC
  timestamp material.
- Backtest response-cache identity combined that decision ID with the exact
  prompt-visible feature hash while deliberately excluding `recent_bars`.

Those implementations were inspected and retained rather than duplicated or
weakened.

## Missing call sites and boundary gaps found

- `ShadowRunner` generated one random UUID before iterating a daily batch. The
  ID changed after restart and was shared by different symbols.
- `Agent.chat()` rejected a blank request ID but passed an unnormalized
  non-blank value to the backend while downstream audit boundaries normalized
  it.
- `Analyst.analyze()`, `Analyst.analyze_plan()`, and
  `BudgetedLLMBackend.create()` used optional defaults, so omission reached
  runtime validation instead of being rejected by the required API contract.

## Implementation

- Agent actor, reason, and request ID are normalized once at the chat boundary.
  Every provider turn and mutating tool/audit path receives that same normalized
  parent identity.
- Both Analyst public methods now require keyword-only `request_id`. `None`,
  blank, and whitespace-only values still fail before backend use.
- `BudgetedLLMBackend.create()` now requires an explicit request ID while
  preserving fail-closed blank validation before budget-store or delegate use.
- Daily shadow analysis now derives one bounded 50-character `shadow:` ID per
  logical persisted call identity. Canonical sorted JSON contains only UTC
  scheduled date, analyst version, and normalized symbol; full SHA-256 is
  encoded as unpadded URL-safe Base64. Equivalent restarts reproduce the ID,
  while symbol changes produce distinct IDs.
- Planning repair coverage uses the real Analyst and budget decorator. Two
  attempts retain one parent ID but produce two distinct settled reservations
  and consume two daily calls.
- Backtest production code was intentionally unchanged. Capture coverage now
  additionally proves symbol and timestamp changes produce distinct canonical
  IDs, while the existing cache-fingerprint regressions remain green.

## Strict TDD evidence

### RED

After adding capture and required-boundary tests:

```bash
uv run pytest -q \
  tests/test_agent.py::test_agent_uses_one_normalized_request_id_for_provider_turns_and_audit \
  tests/test_analyst.py::test_analyst_requires_explicit_request_id_keyword \
  tests/test_llm_budget.py::test_budgeted_backend_requires_explicit_request_id_before_store_or_delegate \
  tests/test_launch_features.py::test_shadow_request_identity_is_stable_per_persisted_daily_call \
  tests/test_planning.py::test_planning_repair_attempts_share_parent_id_but_reserve_separately \
  tests/test_llm_runner.py::test_decision_request_id_normalizes_symbol_run_and_equivalent_offset
```

Result:

```text
5 failed, 2 passed
```

The five intended failures proved the Agent normalization gap, both optional
Analyst signatures, the optional budget-wrapper signature, and random shadow
identity. The two passing tests were deliberate Task 5 preservation evidence:
planning already charged repairs separately under one parent ID, and canonical
backtest identity was already distinct under normalized symbol/time changes.

### Targeted GREEN

The same targeted command passed all seven collected cases after the minimal
implementation.

### Focused regression suite

```bash
uv run pytest -o addopts='' -q \
  tests/test_agent.py tests/test_analyst.py tests/test_analyst_v2.py \
  tests/test_planning.py tests/test_llm_runner.py tests/test_llm_budget.py \
  tests/test_launch_features.py tests/test_news.py tests/test_plans_api.py
```

Result:

```text
192 passed, 1 warning in 33.83s
```

### Full suite

```bash
uv run pytest -o addopts='' -q
```

Result:

```text
1941 passed, 1 skipped, 1 warning in 269.02s (0:04:29)
```

The warning is the pre-existing `websockets.legacy` deprecation warning.

### Compilation and diff

```bash
uv run python -m compileall -q src tests
git diff --check
```

Both exited `0` with no output.

## Changed files

Production:

- `src/trading_assistant/app/agent.py`
- `src/trading_assistant/analyst/analyst.py`
- `src/trading_assistant/analyst/shadow.py`
- `src/trading_assistant/llm/base.py`

Tests:

- `tests/test_agent.py`
- `tests/test_analyst.py`
- `tests/test_planning.py`
- `tests/test_llm_runner.py`
- `tests/test_llm_budget.py`
- `tests/test_launch_features.py`

Provenance:

- `.superpowers/sdd/2026-07-27-policy-budget-foundation/task-8-report.md`

## Self-review

- Every Agent provider turn, including post-tool turns, receives the same
  normalized HTTP/audit identity.
- Analyst analysis and plan repair cannot omit request identity, and no
  lower-level Analyst/provider method creates a replacement random parent ID.
- Shared request identity does not collapse provider attempts: each repair has
  its own reservation lifecycle and charge.
- Shadow IDs are stable across equivalent restart input, distinct across
  symbols and days, bounded below the durable 64-character field, and expose no
  raw run material.
- Task 5 backtest canonicalization and the exact prompt-visible response-cache
  hash were not modified.
- Task 6 and Task 7 safety behavior remains covered by the full suite.
- No Task 9 lease, backtest gate, panic, or scheduled-provider rate-limit
  behavior was implemented.
- No provider, network, broker, notification, order, or daemon process was
  invoked; tests used captures, fakes, and the mock broker only.

## Concerns

None.
