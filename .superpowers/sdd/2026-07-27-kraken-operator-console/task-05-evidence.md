# Plan 3 Task 5 evidence

## Scope

Implemented only the Plans browser workspace on top of backend evidence
contract `ac80f66`. No backend, planning service, database, migration,
credential, runtime, broker/provider, daemon, breaker, order, notification,
browser, or network operation was performed.

The implementation preserves every pre-existing Plans route, form/control ID,
dialog, recent-auth flow, CSRF/idempotency path, approval token, and separate
cancellation flow. Opaque source references remain text and list responses
remain narrative-free.

## Frontend-design direction

- Subject: a local operator tracing one paper research plan from a summary-only
  queue to the exact persisted evidence used for review.
- Hierarchy: actions and local filter, saved plan queue, then selected evidence.
- Identity: original Trading Assistant dark console with the persistent
  `Research / paper-only` and `ALPACA PAPER` boundaries.
- Safety semantics: model confidence is neutral evidence; actual persisted
  statuses are shown verbatim with neutral/caution styling.

## TDD

RED:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security.py -k "plans_page or plans_approval or plans_css or plan_budget or saved_plan_queue or plan_detail_renders" -v
7 failed, 137 deselected in 0.81s
```

The failures were the intended missing three-pane layout, approval boundary,
provider budget gate, summary metadata, and persisted evidence/clearing
behavior.

Focused GREEN:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security.py -k "plans_page or plans_approval or plans_css or plan_budget or saved_plan_queue or plan_detail_renders" -v
7 passed, 137 deselected in 0.67s

uv run pytest tests/test_frontend_ui.py tests/test_security.py -k "plan or plans" -v
26 passed, 118 deselected in 1.04s
```

Final authorized matrix:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py tests/test_plans_api.py -v
193 passed in 13.89s
```

## Static verification

```text
node --check src/trading_assistant/app/static/js/plans.js
PASS

uv run python -m compileall -q src/trading_assistant/app tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py tests/test_plans_api.py
PASS

uv run python scripts/check_release_safety.py
release static checks: PASS

git diff --check
PASS
```

The existing-ID comparison reported no removed Plans IDs.

## Contract and safety review

- Unknown, blocked, exhausted, incomplete, or failed provider evidence clears
  prior budget values and disables only Analyze and Generate proposals before
  their API calls. Request IDs and integer retry/reset timing are rendered.
- The deterministic screener and saved-plan review/cancel controls stay
  available when paid model calls are blocked.
- Queue rows consume only list metadata: plan ID, symbol, action, confidence,
  as-of/freshness, actual status, and paper-only state. Extra narrative fields
  are ignored.
- Detail uses DOM text sinks for persisted thesis, scenarios, concepts, notes,
  invalidation, entry/exit plan, and deterministic sizing.
- Every unavailable evidence field renders `Not recorded`. Source references
  render only when the server marks them `references_only`, under
  `References only`, and never become links or source facts.
- Selecting a row performs only a detail GET. Approval remains an explicit
  reasoned dialog bound to exact plan ID, symbol, action, and `review_token`;
  cancellation remains separate.
- Detail identity mismatch or fetch failure clears old thesis and source
  references before rendering the request-scoped failure.

## Implementation commit

- `c5415cb feat(ui): turn plans into evidence workspace`

## Residuals

- Per instruction, no full suite, browser/runtime verification, service start,
  or network call was performed.
- Concurrent backtest commits `2d0ec64` and `00a38ee` were not edited or
  included in the Task 5 implementation commit.
- No push was performed.
