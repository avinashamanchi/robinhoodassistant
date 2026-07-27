# Alpaca Operations Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a distinctive, broker-truth-first operator cockpit, verify its paper-Alpaca launch path, and publish the verified branch without weakening any execution guardrail.

**Architecture:** Reuse the existing FastAPI/static-JavaScript application and expose one authenticated read-only account endpoint backed by `TradingService`. Extend the current abortable refresh model for account truth, reshape the HTML/CSS into a proof tape plus account masthead, and keep all mutations on their existing audited server paths.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, pytest, vanilla ES modules, HTML, CSS, Alpaca paper API, Git/GitHub.

## Global Constraints

- Alpaca remains paper-only; no live-mode config or environment confirmation is added.
- The LLM cannot execute an order; exact human approval and execution-time risk checks remain mandatory.
- Unknown, stale, provider-failed, or unreconciled state renders blocked or unavailable, never safe.
- No secret, SQLite database, migration backup, lock file, or browser session artifact enters Git.
- No inline scripts, inline styles, unsafe DOM insertion, remote font, or weakened CSP.
- Responsive to 360 CSS pixels, visible keyboard focus, and reduced-motion support are mandatory.

---

### Task 1: Expose authenticated account truth

**Files:**
- Modify: `tests/test_api.py`
- Modify: `src/trading_assistant/app/main.py`

**Interfaces:**
- Consumes: `TradingService.get_account_summary() -> dict[str, Any]`
- Produces: authenticated `GET /account -> {"equity": str, "buying_power": str, "cash": str, "positions": list}`

- [ ] **Step 1: Write the failing endpoint tests**

```python
def test_account_returns_broker_summary(client):
    response = client[0].get("/account")
    assert response.status_code == 200
    assert response.json()["equity"] == "10000"


def test_account_maps_broker_failure_to_stable_dependency_error(client):
    client[1].broker.fail_account = True
    response = client[0].get("/account")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dependency_unavailable"
```

- [ ] **Step 2: Run the two tests and verify route absence/failure**

Run: `.venv/bin/pytest tests/test_api.py -k 'account_returns_broker_summary or account_maps_broker_failure' -q`

Expected: FAIL because `GET /account` is not implemented.

- [ ] **Step 3: Add the minimal authenticated route**

```python
@app.get("/account")
def account(principal: SessionPrincipal = Depends(current_principal)):
    try:
        return service.get_account_summary()
    except RequiredDependencyUnavailable:
        raise _dependency_unavailable() from None
```

- [ ] **Step 4: Run focused API tests**

Run: `.venv/bin/pytest tests/test_api.py -k 'account or positions_and_log' -q`

Expected: PASS.

### Task 2: Make preflight attribution truthful

**Files:**
- Modify: `tests/test_launch.py`
- Modify: `src/trading_assistant/preflight.py`

**Interfaces:**
- Consumes: broker account, clock, and quote calls
- Produces: three independent `Result` values without leaking provider text

- [ ] **Step 1: Write failing attribution tests**

```python
def test_preflight_quote_failure_does_not_fail_auth_or_clock(monkeypatch):
    monkeypatch.setattr(fake_broker, "get_quote", lambda _: raise_(ValueError()))
    auth, clock, data = preflight._alpaca(secrets)
    assert auth.status == preflight.PASS
    assert clock.status == preflight.PASS
    assert data.status == preflight.FAIL
```

Add equivalent tests for account and clock failure so each check proves its
own state independently.

- [ ] **Step 2: Run the tests and verify the current all-or-nothing result fails**

Run: `.venv/bin/pytest tests/test_launch.py -k 'preflight and (quote or account or clock)' -q`

Expected: FAIL because one exception currently marks all three checks failed.

- [ ] **Step 3: Split `_alpaca` into independently guarded calls**

Create the clients once, then wrap account, clock, and quote operations in
separate `try/except` blocks. Return fixed `dependency_failed` details only.

- [ ] **Step 4: Run focused launch tests**

Run: `.venv/bin/pytest tests/test_launch.py -k preflight -q`

Expected: PASS.

### Task 3: Add account refresh behavior and cockpit markup

**Files:**
- Modify: `tests/test_release_static.py`
- Modify: `src/trading_assistant/app/static/index.html`
- Modify: `src/trading_assistant/app/static/js/index.js`

**Interfaces:**
- Consumes: `GET /account`, `/health`, `/pending`, `/positions`
- Produces: proof-tape fields and account masthead with fail-closed refresh semantics

- [ ] **Step 1: Add failing static/behavior contracts**

Assert that `index.html` contains unique IDs for `proof-broker`,
`proof-market`, `proof-data`, `proof-daemon`, `proof-reconciliation`,
`account-equity`, `account-buying-power`, `account-cash`, and
`account-exposure`; and that `index.js` includes account in the abortable
refresh state and fetches `/account`.

- [ ] **Step 2: Run release-static tests and verify missing elements fail**

Run: `.venv/bin/pytest tests/test_release_static.py -q`

Expected: FAIL on the new cockpit contracts.

- [ ] **Step 3: Implement semantic cockpit HTML**

Add a horizontally scrollable proof tape below the header and an account
masthead before the two-column console. Keep all existing dialog IDs and
mutation controls unchanged.

- [ ] **Step 4: Implement `refreshAccount()`**

Use `beginOperationalRefresh("account", ...)`, clear all values to
`Unavailable` on failure, render decimal strings with `Intl.NumberFormat`,
and update proof-tape summaries only from current successful payloads.

- [ ] **Step 5: Run release-static and API tests**

Run: `.venv/bin/pytest tests/test_release_static.py tests/test_security_headers.py tests/test_api.py -q`

Expected: PASS.

### Task 4: Apply the flight-deck visual system

**Files:**
- Modify: `src/trading_assistant/app/static/css/console.css`
- Modify: `src/trading_assistant/app/static/index.html`
- Modify: `src/trading_assistant/app/static/plans.html`
- Modify: `src/trading_assistant/app/static/backtests.html`
- Modify: `src/trading_assistant/app/static/login.html`

**Interfaces:**
- Consumes: existing semantic classes and new proof/account classes
- Produces: coherent desktop/mobile presentation without changing action behavior

- [ ] **Step 1: Add failing accessibility/static checks**

Extend `tests/test_release_static.py` to require one `main` landmark per page,
visible text for every status cell, no unsafe inline attributes, and the shared
stylesheet on every page.

- [ ] **Step 2: Run the tests and verify the new shared-shell contracts fail**

Run: `.venv/bin/pytest tests/test_release_static.py -q`

Expected: FAIL until every page uses the cockpit shell contracts.

- [ ] **Step 3: Implement tokens, proof tape, account masthead, and ledger**

Derive all colors and type decisions from the design spec. Keep the signature
element to the proof tape; remove decorative rules or shadows that do not
encode hierarchy.

- [ ] **Step 4: Add responsive and reduced-motion rules**

At 360 CSS pixels, stack the truth rail above content, preserve horizontal
table/tape scrolling, and prevent action buttons from overflowing. Disable
load transitions under `prefers-reduced-motion: reduce`.

- [ ] **Step 5: Run static/security tests**

Run: `.venv/bin/pytest tests/test_release_static.py tests/test_security_headers.py tests/test_security.py -q`

Expected: PASS.

### Task 5: Keep local runtime state out of publication

**Files:**
- Modify: `.gitignore`
- Modify: `tests/test_release_static.py`

**Interfaces:**
- Consumes: runtime names `*.db.*.pre-migration.bak` and `*.db.submission.lock*`
- Produces: Git ignore rules for private operational artifacts

- [ ] **Step 1: Add a failing ignore-contract test**

```python
def test_private_runtime_artifacts_are_gitignored():
    rules = Path(".gitignore").read_text()
    assert "*.db.*.pre-migration.bak" in rules
    assert "*.db.submission.lock*" in rules
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/pytest tests/test_release_static.py -k runtime_artifacts -q`

Expected: FAIL because the patterns are absent.

- [ ] **Step 3: Add exact ignore patterns**

Append the two patterns without broadening ignores to source, migrations, or
documentation.

- [ ] **Step 4: Verify ignored state**

Run: `git status --short --ignored | rg 'trading_(assistant|runtime).*\\.(bak|lock)'`

Expected: every runtime backup/lock line begins `!!`.

### Task 6: Verify, launch safely, and publish

**Files:**
- No source files unless verification finds a defect

**Interfaces:**
- Consumes: completed branch, paper credentials from the existing untracked environment, copied/migrated runtime ledger
- Produces: fresh test evidence, a running local console, pushed branch, and draft PR

- [ ] **Step 1: Run focused checks**

Run: `.venv/bin/pytest tests/test_api.py tests/test_launch.py tests/test_release_static.py tests/test_security_headers.py tests/test_security.py -q`

Expected: PASS.

- [ ] **Step 2: Run the complete suite**

Run: `.venv/bin/pytest -q`

Expected: zero failures.

- [ ] **Step 3: Run static release gates**

Run: `.venv/bin/python scripts/check_release_safety.py`

Expected: exit 0.

- [ ] **Step 4: Run credentialed paper preflight**

Source the existing untracked environment, point `DATABASE_URL` at the migrated
runtime copy, and run `.venv/bin/python -m trading_assistant.preflight`.

Expected: either `READY`, or a precise fail-closed report. Do not reset a
breaker or claim readiness to manufacture a green result.

- [ ] **Step 5: Visually verify the authenticated console**

Start the app on an unused loopback port, log in through the local session
flow, inspect desktop and 360-pixel layouts, check browser errors, and capture
the final authenticated paper-console screenshot.

- [ ] **Step 6: Commit and publish**

Inspect `git diff` and `git status`, stage only intended source/docs/tests,
commit tersely, push `codex/safety-foundation` to `origin`, and open a draft PR
against `main`. Never force push.

## Plan self-review

- Spec coverage: account truth, proof tape, styling, preflight attribution,
  private runtime state, launch evidence, and publication each map to a task.
- Placeholder scan: no TBD, TODO, “implement later,” or undefined production
  interface remains.
- Type consistency: `/account` returns the existing account-summary decimal
  strings; frontend IDs and refresh-state key names are consistent across
  Tasks 3 and 4.
