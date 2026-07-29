# Plan 3 Tasks 3–4 evidence

## Scope

Implemented only the Operations proof/decision hierarchy and its browser-side
posture, provider-budget, signed-candidate, rate-limit, and bounded-refresh
behavior. No runtime database, Keychain, credential, service, provider, broker,
daemon, breaker, order, notification, browser, or network action occurred.

The governing specification overrides the stale plan IDs. The implementation
preserves `chat-form`, `chat-log`, and `risk-log`, adds
`assistant-candidates`, and consumes positions only from the coherent
`/account` snapshot. It does not request `/positions` or invent unavailable
pending-order source, reasoning, or quote fields.

## Frontend-design direction

- Subject: one local operator separating broker proof, human decisions, paid
  model capacity, and non-executing drafts.
- Hierarchy: environment and critical evidence first, pending decisions second,
  proof rail third, research/candidates afterward.
- Safety semantics: purple remains navigation/action, while verified, caution,
  stale, blocked, and unknown states are named and server-derived.
- Candidate boundary: signed model output remains immutable research until one
  explicit, reasoned queue action creates a proposal or rule.

## TDD

RED:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security.py -k "operations or posture or candidate or stale or abort or rate_limit_metadata or model_budget or refresh_lifecycle" -v
14 failed, 9 passed, 116 deselected in 1.58s
```

The failures were the intended missing Operations landmarks, posture/candidate
modules, rate metadata, model-budget gate, and visibility/abort lifecycle.

Task 3 GREEN:

```text
uv run pytest tests/test_frontend_ui.py -v
26 passed in 0.08s

uv run pytest tests/test_security.py -k "operations or dialog or dom" -v
13 passed, 100 deselected in 0.91s
```

Task 4 focused GREEN:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security.py -k "operations or posture or candidate or stale or abort or rate_limit_metadata or model_budget or refresh_lifecycle" -v
23 passed, 116 deselected in 1.28s

uv run pytest tests/test_security.py -k "posture or candidate or rate_limit_metadata or model_budget or abort or refresh_lifecycle" -v
13 passed, 98 deselected in 0.98s
```

Final focused UI/security matrix:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py -v
145 passed in 8.13s
```

Relevant existing posture/candidate API boundaries:

```text
uv run pytest tests/test_candidate_boundary.py::test_http_queue_requires_csrf_idempotency_and_never_approves_or_submits tests/test_candidate_boundary.py::test_candidate_queue_http_rate_denial_is_fail_closed tests/test_candidate_boundary.py::test_http_terminal_rule_rejection_replays_original_403 tests/test_candidate_boundary.py::test_duplicate_json_and_extra_fields_fail_at_candidate_http_boundary tests/test_security_posture.py::test_security_posture_reports_evidence_not_permission tests/test_security_posture.py::test_posture_models_are_frozen_extra_forbid_and_cannot_authorize tests/test_security_posture.py::test_posture_models_reject_coercive_or_boolean_numeric_scalars tests/test_security_posture.py::test_posture_models_are_strict_and_can_trade_is_exact_false_bool -v
12 passed in 1.55s
```

## Static verification

```text
node --check src/trading_assistant/app/static/js/auth.js
node --check src/trading_assistant/app/static/js/index.js
node --check src/trading_assistant/app/static/js/posture.js
node --check src/trading_assistant/app/static/js/candidates.js
PASS

uv run python -m compileall -q src/trading_assistant/app tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py
PASS

uv run python scripts/check_release_safety.py
release static checks: PASS

git diff --check
PASS
```

The static gate initially identified the internal property comparison
`options.onReceipt ===` as an inline-handler-shaped source. Renaming the
internal callback to `receiptHandler` removed the forbidden source pattern
without weakening the gate.

## Preservation and bounded review

- Existing operational IDs, dialogs, approval/rejection friction, breaker
  generation proof, panic confirmation, CSRF/auth flow, and safety copy remain.
- Consequential controls retain the shared 44px minimum; compact/mobile place
  the proof rail after pending decisions and before research.
- Posture rendering never derives `canTrade`; malformed or failed reports clear
  old posture, budget, and environment values and include stable request IDs.
- Runtime tenure is check-aware: `held` is verified, `released` is caution, and
  `fenced` is blocked.
- Unknown, blocked, exhausted, or incomplete provider evidence disables model
  chat before `/chat` network I/O while broker reads remain available.
- Candidate text uses DOM text sinks only. Queueing posts the unchanged full
  envelope plus a nonblank reason through shared CSRF/idempotency handling.
- Order success requires `proposed`; rule success requires `queued`.
  Rejected/unknown receipts, expiry, replay, risk rejection, and 429 state
  invalidate only the governed action and never render an execution claim.
- Each resource owns one abort controller and 10-second timeout. One visible-
  only 30-second cadence is cleared and all active requests are aborted on
  `pagehide`; there is no retry loop.

## Implementation commits

- `095880b feat(ui): recompose operations around proof`
- `9161a4e feat(ui): render bounded security and candidate state`

## Residuals

- Per instruction, no full suite, browser/runtime verification, service start,
  or network call was performed.
- Concurrent backtest, migration, and sensitive-field work in the shared
  worktree was not edited, staged, or included.
- No push was performed.
