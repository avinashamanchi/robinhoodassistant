# Task 4 Report: Reserve and settle every LLM provider attempt

## Status

Implemented on `codex/safety-foundation` from required HEAD `21549b8`.
Intended commit subject: `feat(llm): reserve durable provider budgets`.

## Files

- Added `src/trading_assistant/llm/budget.py`
  - immutable budget limits/reservation/status interfaces
  - explicit UTF-8 byte upper-bound estimator
  - fail-closed durable reserve/start/settle/unknown/release/status service
  - SQLite `BEGIN IMMEDIATE` authority for every state mutation
- Modified `src/trading_assistant/llm/base.py`
  - `BudgetedLLMBackend`
  - preserves missing provider usage as unknown instead of fabricated zero usage
- Modified `src/trading_assistant/llm/factory.py`
  - explicit Anthropic/Gemini/Groq estimator registry and resolver
  - missing registration denies before raw backend construction
- Modified raw Anthropic, Gemini, and Groq adapters
  - accept and ignore optional `request_id`
  - preserve SDK request fields
  - normalized missing usage remains missing
- Added `tests/test_llm_budget.py`
- Modified `tests/test_llm_backends.py`

## RED / GREEN

Initial RED:

- `uv run pytest tests/test_llm_budget.py -v`
  - collection error: `BudgetedLLMBackend` did not exist
- `uv run pytest tests/test_llm_backends.py -k 'accepts_request_id' -v`
  - 3 failures: all raw adapters rejected `request_id`

Additional RED cycles caught and corrected:

- unknown/released rows incorrectly populated `settled_at`: 2 failures
- missing OpenAI/Gemini/Groq usage was normalized to fake zero usage: 3 failures
- reconciliation denial used the generic unavailable error: 3 failures
- first-call denial persisted an empty budget-day row: 1 failure

Focused GREEN:

- `uv run pytest tests/test_llm_budget.py tests/test_llm_backends.py -v`
  - `55 passed`

## Required behavioral evidence

- Parallel ceilings: the calls/input/output race test passed 20/20 repeated
  runs, covering 60 parameterized two-writer races without overspend.
- Restart durability: settlement counters were observed through a new service
  instance backed by the same real SQLite database.
- UTC behavior: midnight creates a new day, non-UTC timestamps normalize to
  UTC, and unresolved prior-day reconciliation still blocks later reserves.
- Cost behavior: Decimal USD estimates use only matching provider/model
  metadata effective on the budget day; future-dated metadata yields zero.
- Store failure: a real held SQLite write lock normalized to
  `ProviderBudgetUnavailable`, with zero delegate calls and zero reservations.
- State safety: only expired `reserved` rows release; `started`, `unknown`, and
  `settled` rows remain charged; overrun actuals are charged above ceilings and
  force durable reconciliation.

## Full suite

- `uv run pytest`
- `1674 passed, 1 skipped, 1 warning in 241.56s`
- Warning is the pre-existing `websockets.legacy` deprecation warning.

## Self-review

- Reservation day totals and reservation insertion are atomic under one
  `BEGIN IMMEDIATE` transaction.
- `mark_started` commits before delegate invocation.
- Exceptions and missing/malformed usage remain fully charged as `unknown`.
- Normal settlement refunds only unused input/output reservations.
- Overruns charge actual usage without ceiling truncation, set
  `provider_usage_over_reservation`, and block subsequent UTC days.
- Cleanup is transactionally inside `reserve` and cannot release any attempt
  that reached `started`.
- Missing estimator and budget/store denial paths make no provider call; the
  factory checks estimator registration before raw provider construction.
- USD remains diagnostic only; calls and token counters are the hard authority.

## Concerns / handoff

- No unresolved Task 4 concern.
- Task 5 still owns shared-service construction, wrapping each selected backend
  exactly once, passing stable request IDs, and applying configured chat and
  structured-attempt ceilings. This task deliberately does not compose or wrap
  factory outputs.
