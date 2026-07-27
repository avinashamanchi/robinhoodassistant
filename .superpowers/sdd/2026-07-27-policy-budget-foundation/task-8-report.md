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

## Fix Round 1

### Status

Addressed every finding in `task-8-review-1.md` with RED-first tests. The
follow-up commit subject is:

```text
fix(llm): canonicalize durable request identity
```

### Canonical identity contracts

- Added one shared `canonical_request_id()` boundary. It requires a string,
  NFC-normalizes, trims surrounding whitespace, rejects blank values, enforces
  the durable 64-character maximum, and admits only
  `A-Z a-z 0-9 . _ : -`. It never truncates or hashes caller-supplied IDs.
- Agent, Planning, Analyst, `BudgetedLLMBackend`, and
  `ProviderBudgetService` all use that boundary before delegate or durable
  work. The canonical value is reused for audit, provider delegation,
  reservation, and reconciliation.
- Stored provider reservations are required to already equal their canonical
  form. Corrupt noncanonical rows fail closed during reconciliation/status
  validation.
- Equivalent canonical request IDs still consume one distinct reservation and
  one daily call for every provider attempt. Canonicalization does not
  deduplicate or weaken accounting.
- Shadow symbols are NFC-normalized, trimmed, uppercased, limited to 16
  characters, and restricted to `A-Z 0-9 . _ : / -`. Analyst versions are
  NFC-normalized, trimmed, lowercased, limited to 16 characters, and restricted
  to `a-z 0-9 . _ : -`.
- Shadow canonicalizes its configured version and each screened symbol once,
  then reuses those exact values for completed lookup, request hash material,
  report/version persistence, shadow calls, and comparisons.
- The Task 5 aware-datetime-to-fixed-microsecond-UTC-`Z` implementation moved
  to the shared identity module. Backtest decision IDs still use it unchanged;
  validation now rejects naive holdout timestamps and passes canonical UTC
  datetimes downstream.
- Validation canonicalizes and deduplicates symbols, canonicalizes the analyst
  version, and derives one stable bounded ID for equivalent instants and
  normalized inputs. Provider, selected model, symbol, version, or timestamp
  changes remain identity-significant.

### Runtime construction audit

- Removed the unreachable local `AnthropicBackend` from `app/agent.py`.
- Added an AST release check that rejects Anthropic, Gemini, or Groq backend
  construction anywhere in runtime code except `llm/factory.py`.
- Production search confirms the three raw constructor calls remain confined
  to that factory.

### Strict TDD evidence

RED was captured before production changes with:

```bash
.venv/bin/pytest -q \
  tests/test_agent.py tests/test_analyst.py tests/test_planning.py \
  tests/test_llm_budget.py tests/test_launch_features.py \
  tests/test_bootstrap.py tests/test_release_static.py
```

The command exited `1` with 63 intended failures. They showed noncanonical IDs
reaching all five boundaries, shadow version/symbol drift across restart and
persistence, raw/naive validation timestamps, and missing static enforcement.

Final focused verification:

```bash
.venv/bin/pytest -o addopts='' -q \
  tests/test_agent.py tests/test_analyst.py tests/test_analyst_v2.py \
  tests/test_planning.py tests/test_launch_features.py \
  tests/test_bootstrap.py tests/test_llm_runner.py \
  tests/test_backtest_engine.py tests/test_backtest_evaluate.py \
  tests/test_llm_budget.py tests/test_llm_budget_review.py \
  tests/test_llm_budget_review_2.py tests/test_llm_budget_review_3.py \
  tests/test_llm_backends.py tests/test_factory.py \
  tests/test_release_static.py tests/test_route_policy.py \
  tests/test_durable_limits.py
```

Result:

```text
562 passed, 1 warning in 96.15s
```

Full suite:

```bash
.venv/bin/pytest -o addopts='' -q
```

Result:

```text
2020 passed, 1 skipped, 1 warning in 268.52s
```

The warning is the pre-existing `websockets.legacy` deprecation warning.

Additional final checks all exited `0`:

```bash
.venv/bin/python scripts/check_release_safety.py
.venv/bin/python -m compileall -q src tests scripts
git diff --check
```

### Changed files in Fix Round 1

Production and release checks:

- `scripts/check_release_safety.py`
- `src/trading_assistant/identity.py`
- `src/trading_assistant/app/agent.py`
- `src/trading_assistant/analyst/analyst.py`
- `src/trading_assistant/analyst/planning.py`
- `src/trading_assistant/analyst/shadow.py`
- `src/trading_assistant/backtest/llm_runner.py`
- `src/trading_assistant/llm/base.py`
- `src/trading_assistant/llm/budget.py`
- `src/trading_assistant/validate_analyst.py`

Tests:

- `tests/test_agent.py`
- `tests/test_analyst.py`
- `tests/test_bootstrap.py`
- `tests/test_launch_features.py`
- `tests/test_llm_budget.py`
- `tests/test_llm_budget_review_2.py`
- `tests/test_planning.py`
- `tests/test_release_static.py`

### Self-review

- Request IDs are canonical and bounded before every relevant delegate/store
  boundary, and lower layers never manufacture replacement IDs.
- Multi-turn chat, tool turns, structured repair, planning repair, spot-check,
  and backtest attempts retain one parent identity while provider attempts are
  reserved and reconciled independently.
- Shadow and validation IDs are stable across equivalent normalized input and
  restart, non-secret, bounded, and distinct for logical symbol/version/day/time
  changes.
- The Task 5 response-cache still hashes the exact prompt-visible payload and
  still deliberately excludes only `recent_bars`; that function was not
  changed.
- Task 6/7 route-policy and durable-limit regressions are included in the
  focused verification.
- No Task 9 behavior was implemented. No provider, network, live broker,
  notification, order, or daemon action was invoked.

### Concerns

None.

## Fix Round 2

### Status

Addressed every finding in `task-8-review-2.md` with RED-first tests. This
section supersedes Fix Round 1 statements that identifiers are
NFC-normalized: raw non-ASCII is now rejected before normalization or case
conversion can occur.

Commit subject:

```text
fix(identity): reject ambiguous raw identifiers
```

### Raw-input contract

- The shared identity boundary first requires a string and scans the original
  input without changing it.
- Every non-ASCII code point and every ASCII control (`0x00-0x1f`, `0x7f`) is
  rejected before trimming or case conversion.
- Only outer ASCII SPACE (`0x20`) is eligible for trimming. Tabs, newlines,
  NBSP, and other Unicode whitespace are never treated as equivalent spaces.
- Request IDs remain case-preserving. Symbols uppercase and analyst versions
  lowercase only after the raw input has passed the ASCII/control scan.
- Kelvin sign, sharp-S, and dotless-I therefore cannot normalize or
  case-expand into allowed ASCII identifiers.
- The existing canonical length and character policies remain unchanged after
  safe outer-space trimming.

### HTTP request identity

- Removed the separate `X-Request-ID` regex from the HTTP middleware.
- Incoming headers now pass through `canonical_request_id()` before request
  state is set.
- Exact and outer-ASCII-space-equivalent valid headers produce the same
  response, audit, and provider-attempt identity.
- Invalid or missing headers generate one UUID fallback in the middleware.
  Response hardening, API errors, audit recording, and provider delegation all
  reuse that state value rather than generating replacements.
- API tests use a real Agent with a capture-only backend and the local mock
  service; no provider, network, or live broker call is made.

### Task 10 scope correction

- Removed Fix Round 1's partial raw-backend-construction check from
  `scripts/check_release_safety.py`.
- Removed its partial negative fixture from `tests/test_release_static.py`.
- The complete construction gate remains deferred to Task 10.
- The unreachable local `AnthropicBackend` remains deleted. A scoped Task 8
  module assertion and source search confirm the app agent module does not
  expose or define it.

### Strict TDD evidence

RED was captured before production changes:

```bash
.venv/bin/pytest -o addopts='' -q \
  tests/test_identity.py \
  tests/test_api.py::test_http_request_id_canonicalizes_once_for_response_audit_and_provider \
  tests/test_api.py::test_invalid_http_request_id_generates_one_fallback_per_request \
  tests/test_agent.py::test_agent_module_does_not_expose_a_raw_anthropic_backend
```

Result:

```text
21 failed, 9 passed, 1 warning in 1.50s
```

The 21 failures directly exposed unsafe pre-validation trimming,
normalization/case expansion, and the split HTTP regex path. The passing cases
were preservation evidence for already-rejected raw characters, one generated
fallback per invalid request, and the already-deleted local backend.

The same new contract plus the existing release gate passed after the minimal
implementation:

```text
38 passed, 1 warning in 2.73s
```

Focused identity, Agent/Analyst/Planning, shadow, validation/bootstrap, HTTP
security, provider budget, backtest, Task 6/7, and release-gate verification:

```text
826 passed, 1 warning in 111.88s
```

Full suite:

```bash
.venv/bin/pytest -o addopts='' -q
```

Result:

```text
2049 passed, 1 skipped, 1 warning in 269.31s
```

The skip and `websockets.legacy` deprecation warning are pre-existing.

Final static, compilation, and diff checks:

```bash
.venv/bin/python scripts/check_release_safety.py
.venv/bin/python -m compileall -q src tests scripts
git diff --check
```

All exited `0`; the existing release script printed
`release static checks: PASS`.

Scoped source searches returned no Task 10 partial gate and no local app
backend definition:

```bash
rg -n "_check_llm_backend_construction|raw LLM backend construction" \
  scripts tests/test_release_static.py
rg -n "class AnthropicBackend" src/trading_assistant/app
```

### Changed files in Fix Round 2

Production:

- `src/trading_assistant/identity.py`
- `src/trading_assistant/app/security.py`

Task 10 deferral:

- `scripts/check_release_safety.py`
- `tests/test_release_static.py`

Tests:

- `tests/test_identity.py`
- `tests/test_agent.py`
- `tests/test_api.py`

Provenance:

- `.superpowers/sdd/2026-07-27-policy-budget-foundation/task-8-report.md`

### Self-review

- Unsafe raw input cannot become valid through trimming, Unicode
  normalization, or Unicode case conversion.
- Valid outer ASCII spaces canonicalize consistently at direct and HTTP
  boundaries.
- Every HTTP request has one state identity reused by the response, audit, and
  provider path; invalid headers consume exactly one UUID generation.
- Request-ID length/character policy, per-attempt budget reservations, shadow
  and validation identity, Task 5 response-cache hashing, and Task 6/7 safety
  behavior remain covered.
- No Task 9 or Task 10 runtime behavior was implemented.
- No external provider, network, live broker, notification, order, or daemon
  action was performed.

### Concerns

None.
