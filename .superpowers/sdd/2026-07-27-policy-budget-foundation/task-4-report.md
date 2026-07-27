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
  metadata effective on the budget day; future-dated metadata is unavailable.
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

## Fix round 1

### Review items resolved

- Added `src/trading_assistant/llm/payloads.py` with shared pure validation and
  provider-bound Anthropic, Gemini, and Groq builders. Raw adapters and their
  explicit estimators consume the same transformations, so estimation cannot
  drift from request construction. Each estimator serializes a conservative
  envelope with `allow_nan=False`.
- Payload root and nested JSON shape, finite numbers, message/tool semantics,
  and optional non-empty `tool_choice` are validated before estimation,
  reservation, SDK client construction, or delegate invocation. Provider-bound
  transformed envelopes are also checked, including Gemini tool-result JSON.
- Usage is accepted only when both fields are exact non-negative integers.
  Missing, partial, boolean, string, fractional, negative, and property-raising
  usage remains fully charged as `unknown`; OpenAI/Groq and Gemini adapters no
  longer synthesize zero for partial or malformed usage.
- Delegate failures preserve the original exception if `mark_unknown` also
  fails. Usage-access failures attempt unknown marking and never settle/refund.
- ORM metadata now mirrors Task 2's day-counter and reservation constraints.
  Reserve, settle, release, and status validate every durable row they load;
  corrupted counters, reservation states, actuals, and underflow fail closed.
- Price metadata is validated eagerly for valid keys/models/effective dates,
  unique dated records, and finite non-negative Decimal rates. Missing
  applicable reviewed pricing returns `estimated_usd=None`; exact zero requires
  valid applicable rates and zero token usage.
- Modified `src/trading_assistant/db/models.py`,
  `src/trading_assistant/llm/{base,budget,factory,anthropic_backend,gemini_backend,groq_backend}.py`,
  `tests/test_llm_backends.py`, and `tests/test_llm_budget.py`; added
  `tests/test_llm_budget_review.py`.

### Strict TDD evidence

- Initial review RED:
  `uv run pytest tests/test_llm_budget_review.py
  tests/test_llm_budget.py::test_estimated_usd_is_unavailable_before_configured_effective_date -v`
  collected 68 tests: 65 failed, 3 passed.
- Additional boundary REDs independently exposed non-finite transformed Gemini
  payload acceptance, falsey non-mapping price metadata, duplicate dated
  prices, and non-expired corrupt reservations before their minimal fixes.
- Focused GREEN:
  `uv run pytest tests/test_llm_budget_review.py tests/test_llm_budget.py
  tests/test_llm_backends.py tests/test_db_models.py -q` passed all 132 tests.
- Parallel reservation race: the three calls/input/output ceiling cases passed
  20/20 repetitions, covering 60 real-SQLite concurrent-writer races.
- Restart, UTC rollover/normalization, prior-day reconciliation, Decimal cost,
  missing-price, malformed usage, zero-write denial, overrun, refund, and
  durable corruption paths all passed in focused tests.
- Single final full suite: `uv run pytest` produced
  `1746 passed, 1 skipped, 1 warning in 242.51s`; the warning is the same
  pre-existing `websockets.legacy` deprecation warning.

### Fix-round self-review and concerns

- Reserve still inserts its day totals and reservation row atomically under
  `BEGIN IMMEDIATE`; cleanup runs in that transaction and only releases expired
  unstarted `reserved` rows.
- `mark_started` commits before the delegate; started, unknown, settled, and
  overrun attempts cannot be released. Normal settlement refunds only unused
  token reservations, while actual overruns are fully charged and block later
  reserves through durable reconciliation.
- Budget, estimator, payload, and store denials cause no raw provider call.
  Factory work remains the minimal explicit estimator resolver only; Task 5
  shared-container and wrapper composition remains out of scope.
- No unresolved Task 4 fix-round concern.

## Fix round 2

### Review items resolved

- `tool_choice` now accepts only `None`, `auto`, or `any`. Any non-`None`
  choice requires at least one tool, and all invalid cases fail before
  estimation, durable-store access, SDK client use, or delegate invocation.
- Shared provider builders explicitly translate `auto`/`any` as Anthropic
  `auto`/`any`, Gemini `AUTO`/`ANY`, and Groq `auto`/`required`. The Gemini
  adapter consumes the builder-selected mode instead of hard-coding `ANY`.
- Every provider/day is reconciled against all of its reservation rows:
  `reserved`, `started`, and `unknown` charge one call plus reserved tokens;
  `settled` charges one call plus exact actual tokens; `released` charges zero.
  Missing days, orphaned reservations, mismatched provider/day keys, and any
  non-exact aggregate fail closed.
- Reservation validation now includes created/expiry/start/settle chronology,
  UTC budget-day agreement, and state-specific timestamp/actual presence.
- Reserve-time cleanup validates the selected provider before releases and
  affected provider aggregates afterward, then validates the freshly inserted
  day/reservation before commit. Explicit global cleanup validates all
  providers before mutation and each affected provider after mutation.
- Start, settle, and unknown transitions load and validate reservation/day
  pre-state under `BEGIN IMMEDIATE`, mutate only a legal state, flush, validate
  post-state, and then commit. Settlement cannot conceal a pre-existing
  undercount by adding larger actual usage. Status validates the complete
  provider aggregate before returning.
- Modified `src/trading_assistant/llm/budget.py`,
  `src/trading_assistant/llm/payloads.py`, and
  `src/trading_assistant/llm/gemini_backend.py`; added
  `tests/test_llm_budget_review_2.py`.

### Strict TDD evidence

- Initial RED: `uv run pytest tests/test_llm_budget_review_2.py -v` collected
  30 tests and produced `26 failed, 4 passed`. Invalid choices reached the
  delegate/store, Anthropic and Gemini omitted `auto`, and relational
  corruption authorized or mutated.
- Adapter seam RED:
  `uv run pytest
  tests/test_llm_budget_review_2.py::test_gemini_adapter_preserves_translated_tool_choice -v`
  produced `1 failed, 1 passed`, proving Gemini `auto` was sent as `ANY`.
- Focused GREEN:
  `uv run pytest tests/test_llm_budget_review_2.py
  tests/test_llm_budget_review.py tests/test_llm_budget.py
  tests/test_llm_backends.py tests/test_db_models.py -v` produced
  `164 passed`.
- Parallel reservation race: calls/input/output ceilings passed 20/20 repeated
  runs, covering 60 real-SQLite concurrent-writer cases with exact aggregates
  and no overspend.
- Single final full suite: `uv run pytest` produced
  `1778 passed, 1 skipped, 1 warning in 242.01s`; the warning remains the
  pre-existing `websockets.legacy` deprecation warning.

### Fix-round self-review and concerns

- All budget mutations remain inside `BEGIN IMMEDIATE`; aggregate validation,
  cleanup, transition arithmetic, and post-state validation are atomic.
- Relational corruption blocks reserve, start, settle, unknown, release, and
  status without clamping or partial mutation. Overruns remain fully charged
  and continue to set durable reconciliation.
- Reserve and transition checks scan only the target provider; explicit global
  cleanup scans all providers because all are in mutation scope.
- No migration, real provider call, process, notification, broker action, or
  Task 5 composition was introduced.
- No unresolved Task 4 fix-round concern.

## Fix round 3

### Review items resolved

- Provider/day aggregate validation now derives overrun presence from every
  settled reservation whose input or output actual exceeds its reservation.
  Any overrun requires `reconciliation_required=True` and exactly
  `provider_usage_over_reservation`; no overrun requires a false flag and empty
  code because no other reconciliation code is currently supported.
- The bidirectional reconciliation relation is enforced by the same aggregate
  validator already used before reserve authorization, every transition,
  cleanup, and status. Cleared flags/codes, changed codes, and stale
  reconciliation without an overrun all fail closed.
- Each provider estimator now serializes both valid provider-specific `auto`
  and `any` envelopes when tools exist and returns the maximum UTF-8 byte
  count. Tool-free payloads are serialized once with no explicit choice.
- Added `tests/test_llm_budget_review_3.py`; modified only
  `src/trading_assistant/llm/budget.py` for production behavior.

### Strict TDD evidence

- RED: `uv run pytest tests/test_llm_budget_review_3.py -v` collected 11 tests
  and produced `5 failed, 6 passed`. Anthropic/Gemini `auto` exceeded the
  estimator by one byte, while cleared-both, changed, and stale reconciliation
  states escaped validation.
- Estimator expectations are complete literal Anthropic, Gemini, and Groq
  provider envelopes for both valid modes. No production builder or helper is
  used to derive expected bytes.
- Focused GREEN:
  `uv run pytest tests/test_llm_budget_review_3.py
  tests/test_llm_budget_review_2.py tests/test_llm_budget_review.py
  tests/test_llm_budget.py tests/test_llm_backends.py tests/test_db_models.py -v`
  produced `175 passed`.
- Parallel reservation race: calls/input/output ceilings passed 20/20 repeated
  runs, covering 60 real-SQLite concurrent-writer cases without overspend.
- Single final full suite: `uv run pytest` produced
  `1789 passed, 1 skipped, 1 warning in 242.07s`; the warning remains the
  pre-existing `websockets.legacy` deprecation warning.

### Fix-round self-review and concerns

- Multiple settled reservations are folded with logical overrun existence:
  any input or output overrun requires the exact durable marker.
- Legitimate overrun settlement still charges exact actual usage before the
  post-state reconciliation check and continues to block later reservations.
- Estimation compares actual valid translated modes instead of relying on a
  generic forced choice; Groq `required`, Anthropic `auto`, and Gemini `AUTO`
  are each covered.
- No migration, provider call, process, notification, broker action, factory
  composition, or other Task 5 scope was introduced.
- No unresolved Task 4 fix-round concern.
