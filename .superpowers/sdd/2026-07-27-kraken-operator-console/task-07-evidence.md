# Plan 3 Task 7 evidence

## Scope

Implemented only the authorized login, shared browser modal, Operations/Plans
dialog integration, page semantics, responsive/print CSS, and frontend/security
tests. No backend, route, configuration, database, migration, credential,
Keychain, TLS, provider, broker, daemon, breaker, order, browser, service, or
network operation was performed.

Concurrent release and CI work was preserved and excluded from this task's
staging allowlist.

## TDD

RED:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security.py \
  -k "accessibility or responsive or login or shared_modal or user_dismissal \
  or plan_mutation_modals or coercive_confidence or review_dialogs \
  or print_output or dynamic_tables" -v

17 failed, 14 passed, 142 deselected in 1.29s
```

The intended failures proved:

- authenticated and login `main` landmarks were unnamed and dialogs lacked
  descriptions;
- login omitted the explicit local HTTPS boundary and bounded retry timing;
- modal focus/dismissal behavior was duplicated and did not block user
  dismissal while mutations were in flight;
- plan summary confidence accepted coercive values such as `null`, `false`,
  an empty string, arrays, and out-of-range numbers;
- mobile navigation targets were 40px, the environment strip was not sticky,
  table cues were inconsistent, and print had no permanent paper/simulated
  labels.

GREEN:

```text
31 passed, 142 deselected in 0.96s
```

Final authorized regression matrix:

```text
uv run pytest \
  tests/test_frontend_ui.py \
  tests/test_security.py \
  tests/test_security_headers.py -v

181 passed in 9.78s
```

## Security and accessibility behavior

- `auth.js` owns `openModal` and `closeModal`, focus containment, Escape/native
  cancel/backdrop behavior, opener restoration, and verified programmatic
  closure.
- Approval, rejection, panic, plan approval, and plan cancellation pass
  fail-closed `canDismiss` callbacks. Their explicit cancel controls are
  disabled while the corresponding mutation is in flight.
- Existing approval proof, review-token, plan identity, CSRF, recent-auth, and
  idempotency flows remain intact.
- Login and reauthentication clear the password input before request network
  I/O and clear their local secret and serialized body variables in `finally`.
- Login displays only bounded integer `Retry-After` values up to 900 seconds;
  invalid or excessive values receive stable generic retry copy.
- Current malformed plan-list refreshes clear selected evidence and both plan
  mutation authorities. Confidence must be a finite JavaScript number in the
  inclusive range `[0, 1]`.
- Every dialog has a title and description association; password errors are
  associated with their fields; live regions remain bounded.
- Consequential controls remain at least 44px, mobile data stays in internally
  scrollable tables with visible cues, and print output carries permanent
  Alpaca paper and simulated-evidence labels.

## Static verification

```text
node --check src/trading_assistant/app/static/js/auth.js
node --check src/trading_assistant/app/static/js/login.js
node --check src/trading_assistant/app/static/js/index.js
node --check src/trading_assistant/app/static/js/plans.js
PASS

uv run python -m compileall -q \
  src/trading_assistant/app \
  tests/test_frontend_ui.py \
  tests/test_security.py \
  tests/test_security_headers.py
PASS

uv run python scripts/check_release_safety.py
release static checks: PASS

git diff --check -- <Task 7 allowlist>
PASS
```

The first static-gate run identified the new modal state's `.on…` property
names as prohibited inline-handler syntax. Internal listener fields were
renamed and option callbacks were destructured; the behavioral tests stayed
green and the release static gate then passed.

## Caveats

This task intentionally performed no real-browser inspection; that belongs to
Plan 3 Task 8's isolated mock-data HTTPS run. It does not establish Alpaca
paper readiness, broker truth, daemon health, breaker state, or profitability.
