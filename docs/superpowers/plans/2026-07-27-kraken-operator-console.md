# Kraken-Inspired Operator Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use frontend-design for the visual implementation, then superpowers:subagent-driven-development (recommended) or superpowers:executing-plans task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current light flight-deck skin with an original, data-dense purple-accented dark console that makes paper mode, blockers, freshness, model budgets, security posture, human approval, and simulated evidence impossible to confuse.

**Architecture:** Four static HTML pages share one local CSS system and small ES modules with no build step or remote assets. Existing endpoint contracts and element IDs are preserved where practical. New posture and signed-candidate data are rendered only through DOM text nodes. Failure paths clear stale values and expose stable request IDs instead of leaving old data on screen.

**Tech Stack:** semantic HTML, modern CSS, vanilla ES modules, FastAPI static files, pytest static-contract tests, browser inspection

## Global Constraints

- The governing specification is `docs/superpowers/specs/2026-07-27-loopback-kraken-security-console-design.md`.
- Complete `2026-07-27-policy-budget-foundation.md` and `2026-07-27-secrets-model-trust.md` first.
- Use the Kraken design document only as visual inspiration. Do not copy the Kraken logo, wordmark, proprietary font, screenshots, product copy, or imply affiliation.
- Product name remains **Trading Assistant** and broker label remains **Alpaca paper**.
- Never label paper activity as live, a stale daemon as healthy, a tripped breaker as ready, a proposal as an order, or a backtest as expected performance.
- Preserve every human gate, recent-authentication dialog, exact approval confirmation, breaker generation check, panic confirmation, audit reason, and execution-time risk explanation.
- All external/model text is inserted with `textContent` or `createTextNode`; never `innerHTML`, `insertAdjacentHTML`, inline script, remote image, remote font, or unsafe CSP exception.
- Green is reserved for server-verified good state. Purple is brand/action. Yellow is caution. Red is blocked/destructive.
- Use system fonts and local SVG only.
- UI animations must honor `prefers-reduced-motion`.
- Run focused tests after each task and the full suite before completing this plan.

---

## File map

**Create**

- `DESIGN.md` — repository-local visual contract adapted from the approved design.
- `src/trading_assistant/app/static/img/trading-orbit.svg` — original local product mark.
- `src/trading_assistant/app/static/js/posture.js` — posture/budget normalization and stale-state helpers.
- `src/trading_assistant/app/static/js/candidates.js` — signed-candidate cards and explicit queue actions.
- `tests/test_frontend_ui.py` — semantic, token, CSP, rendering, accessibility, and stale-state contracts.

**Modify**

- `src/trading_assistant/app/static/css/console.css` — full dark visual system and responsive layouts.
- `src/trading_assistant/app/static/index.html` — main operations cockpit.
- `src/trading_assistant/app/static/plans.html` — research/planning workspace.
- `src/trading_assistant/app/static/backtests.html` — simulation evidence workspace.
- `src/trading_assistant/app/static/login.html` — loopback operator entry.
- `src/trading_assistant/app/static/js/index.js` — posture, budget, candidate, stale, and proof rendering.
- `src/trading_assistant/app/static/js/plans.js` — dark workspace states and trust context.
- `src/trading_assistant/app/static/js/backtests.js` — evidence charts and hard budget feedback.
- `src/trading_assistant/app/static/js/login.js` — HTTPS/loopback status and safe errors.
- `src/trading_assistant/app/static/js/auth.js` — shared request-state and status helpers.
- `tests/test_security.py` — no unsafe DOM/CSP behavior.
- `tests/test_security_headers.py` — local asset/CSP contract.
- `README.md` — operator-console screenshots and interpretation notes after verification.

---

### Task 1: Freeze the visual and semantic contract in tests

**Files:**

- Create: `DESIGN.md`
- Create: `tests/test_frontend_ui.py`
- Modify: `tests/test_security.py`

**Interfaces:**

- Defines CSS token names, state semantics, responsive breakpoints, and page landmarks.
- Produces reusable static assertions before markup changes.

- [ ] **Step 1: Write the repository design contract**

Use these exact core tokens:

```css
--canvas: #0b0914;
--surface: #101114;
--surface-raised: #171420;
--surface-interactive: #201a2e;
--border: #302941;
--brand: #7132f5;
--brand-hover: #5741d8;
--brand-deep: #5b1ecf;
--brand-wash: rgba(133, 91, 251, 0.16);
--text: #f7f4ff;
--text-muted: #9497a9;
--verified: #2bc48a;
--caution: #f0b45d;
--danger: #ff647c;
```

`DESIGN.md` must define:

- dark-only product surface;
- 4/8/12/16/24/32 spacing rhythm;
- 12px control radius and 14px panel radius;
- 40px default control/row height;
- system sans stack and system monospace stack;
- title, section, metric, label, tabular-number, and metadata scales;
- exact meanings of verified/caution/blocked/unknown/stale/simulated;
- focus ring `0 0 0 3px rgba(113, 50, 245, .35)`;
- desktop `>=1180`, compact `760–1179`, mobile `<760`;
- no trademark or remote asset usage.

- [ ] **Step 2: Write failing page/token tests**

```python
def test_console_declares_approved_tokens():
    css = static_text("css/console.css")
    for token, value in APPROVED_TOKENS.items():
        assert f"--{token}: {value};" in css


@pytest.mark.parametrize("page", [
    "index.html",
    "plans.html",
    "backtests.html",
    "login.html",
])
def test_every_page_uses_original_local_mark(page):
    html = static_text(page)
    assert "/static/img/trading-orbit.svg" in html
    assert "kraken" not in html.lower()
    assert "flight-deck.svg" not in html
```

Add assertions for one `main`, skip link, visible page title, explicit paper
label, local stylesheet/script, no remote URL, no inline style/script, no
`innerHTML`, no `insertAdjacentHTML`, no `<img src="http`, and no unlabelled
form control.

- [ ] **Step 3: Run and verify current-style failures**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security.py -v
```

Expected: FAIL because the approved tokens/mark/page contract are absent.

- [ ] **Step 4: Keep the red test uncommitted and proceed to Task 2**

Record the exact failing assertions in the task notes. Do not commit a red test
suite; Task 2 implements the shared system and commits the test with its passing
code.

---

### Task 2: Build the original mark and shared dark component system

**Files:**

- Create: `src/trading_assistant/app/static/img/trading-orbit.svg`
- Modify: `src/trading_assistant/app/static/css/console.css`
- Modify: `src/trading_assistant/app/static/index.html`
- Modify: `src/trading_assistant/app/static/plans.html`
- Modify: `src/trading_assistant/app/static/backtests.html`
- Modify: `src/trading_assistant/app/static/login.html`
- Modify: `tests/test_frontend_ui.py`
- Modify: `tests/test_security_headers.py`

**Interfaces:**

- Produces shared `.app-shell`, `.topbar`, `.environment-strip`,
  `.side-nav`, `.panel`, `.metric`, `.status-chip`, `.data-table`,
  `.button`, `.dialog`, `.skeleton`, and `.empty-state` components.
- Keeps static assets self-hosted and immutable.

- [ ] **Step 1: Create an original geometric mark**

Use a 32×32 SVG with:

- two offset orbital arcs;
- one solid circular node;
- `currentColor` fills/strokes;
- no text, animal silhouette, exchange icon, crown, tentacles, or copied path;
- accessible use through adjacent visible product text, so the SVG itself is
  `aria-hidden="true"`.

- [ ] **Step 2: Replace the stylesheet with token-driven layers**

Order CSS sections exactly:

1. tokens and color-scheme;
2. reset/base;
3. typography/numbers;
4. shell/navigation;
5. status/environment;
6. panels/metrics/tables;
7. forms/buttons/dialogs;
8. page-specific layouts;
9. loading/empty/stale/error states;
10. responsive;
11. reduced motion and forced colors.

Use `font-variant-numeric: tabular-nums` for quantities, prices, times, budgets,
and percentages. Body minimum size is 14px; metadata may be 12px. No gradient
behind body text. Purple glow is limited to focus/selected/action surfaces.

- [ ] **Step 3: Install one shared shell in all authenticated pages**

Each page gets:

- top-left original mark + “Trading Assistant”;
- nav tabs: Operations, Plans, Backtests;
- top-right operator/session controls;
- persistent environment strip with `ALPACA PAPER`, breaker state, daemon
  state, reconciliation state, and observed timestamp;
- a single page-specific `h1`.

Preserve existing route links and element IDs used by JavaScript. Replace the
text “flight deck” everywhere.

- [ ] **Step 4: Implement component states**

Every dynamic component must have CSS classes for:

```text
is-loading
is-empty
is-verified
is-caution
is-blocked
is-unknown
is-stale
has-error
```

Unknown and stale cannot inherit verified color. Disabled destructive buttons
must remain visibly disabled and retain explanatory text.

- [ ] **Step 5: Run static component tests**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security_headers.py -v
```

Expected: all tests written through Task 2 PASS.

- [ ] **Step 6: Commit**

```bash
git add DESIGN.md src/trading_assistant/app/static/img/trading-orbit.svg src/trading_assistant/app/static/css/console.css src/trading_assistant/app/static/index.html src/trading_assistant/app/static/plans.html src/trading_assistant/app/static/backtests.html src/trading_assistant/app/static/login.html tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py
git commit -m "feat(ui): establish dark operator design system"
```

---

### Task 3: Recompose the operations page around proof and decisions

**Files:**

- Modify: `src/trading_assistant/app/static/index.html`
- Modify: `src/trading_assistant/app/static/css/console.css`
- Modify: `tests/test_frontend_ui.py`
- Modify: `tests/test_security.py`

**Interfaces:**

- Preserves every existing operational control ID.
- Adds posture, provider-budget, freshness, and signed-candidate regions.

- [ ] **Step 1: Add failing landmark and safety-copy tests**

Assert the operations page contains:

- `#environment-mode` with initial text `ALPACA PAPER`;
- `#security-posture-panel`;
- `#provider-budget-calls`, `#provider-budget-input`,
  `#provider-budget-output`, and `#provider-budget-reset`;
- `#critical-banner`;
- `#pending-list`;
- `#positions`, `#holdings`, `#receipt-panel`, `#execution-log`;
- `#assistant-form`, `#assistant-messages`, and `#assistant-candidates`;
- breaker reset and panic buttons/dialogs;
- copy saying candidates do not queue themselves and approval rechecks risk.

- [ ] **Step 2: Run and verify missing regions**

```bash
uv run pytest tests/test_frontend_ui.py -k operations -v
```

Expected: FAIL until the new composition is present.

- [ ] **Step 3: Recompose the desktop layout**

Use this hierarchy:

```text
topbar
environment strip
main
  page heading + explicit refresh
  critical blocker banner
  proof grid
    account equity / buying power / cash / exposure
    broker mode / reconciliation / daemon / quote freshness
  split workspace
    primary column
      pending human decisions
      positions
      assistant research + candidate drafts
      immutable activity ledger
    proof rail
      security posture
      provider budgets
      scoped breakers + reset control
      panic procedure
      action receipts
```

At compact width, proof rail moves under pending decisions. At mobile width,
all sections stack; critical state and approval controls remain before market
research.

- [ ] **Step 4: Preserve exact dangerous-action friction**

Approval cards show:

- proposed symbol, side, quantity/notional, type, price;
- proposal age/expiry;
- source and reasoning availability;
- last quote timestamp;
- “fresh risk check occurs after approval”;
- review/approve and reject as distinct actions.

Breaker reset remains scoped to one currently tripped breaker and shows observed
generation. Panic remains a separate dialog with account scope and result
receipt. Neither action may appear as a purple primary shortcut.

- [ ] **Step 5: Run page/security tests**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security.py -k "operations or dialog or dom" -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/trading_assistant/app/static/index.html src/trading_assistant/app/static/css/console.css tests/test_frontend_ui.py tests/test_security.py
git commit -m "feat(ui): recompose operations around proof"
```

---

### Task 4: Render posture, budgets, freshness, and candidates fail-closed

**Files:**

- Create: `src/trading_assistant/app/static/js/posture.js`
- Create: `src/trading_assistant/app/static/js/candidates.js`
- Modify: `src/trading_assistant/app/static/js/auth.js`
- Modify: `src/trading_assistant/app/static/js/index.js`
- Modify: `tests/test_frontend_ui.py`
- Modify: `tests/test_security.py`

**Interfaces:**

- Produces `normalizePosture()`, `renderPosture()`, and `clearPosture()`.
- Produces `renderCandidates()` and `queueCandidate()`.
- Polls local state only on explicit refresh and a bounded 30-second cadence.

- [ ] **Step 1: Write JavaScript behavior tests using the existing Node harness**

Test:

- failed `/security/posture` clears every old posture/budget value;
- missing `observed_at` displays `Unknown`, never “just now”;
- stale checks become caution/blocked according to server status;
- exhausted provider budget disables chat submit before a provider call;
- a signed candidate renders immutable fields and one explicit queue button;
- queue sends the signed envelope, operator reason, CSRF, and idempotency key;
- queue success says `Proposal queued — not executed`;
- replay/expiry/risk rejection clears the candidate action and shows request ID;
- all model strings are assigned through `textContent`.
- a `429` surfaces `Retry-After`/reset time and temporarily disables only the
  action governed by that policy.

- [ ] **Step 2: Run and verify missing modules**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security.py -k "posture or candidate or stale" -v
```

Expected: FAIL because posture/candidate modules do not exist.

- [ ] **Step 3: Normalize posture without inventing state**

`normalizePosture(payload, now)`:

- accepts only arrays/strings matching the endpoint schema;
- maps unknown names to a generic row without special authority;
- computes display age but does not override server status;
- treats parse failure as all checks unknown;
- never derives `canTrade` from green checks.

`clearPosture(error)` writes `Unavailable`, adds `.has-error`, and includes the
stable request ID when present.

`ApiRequestError` captures integer `Retry-After` and `X-RateLimit-Reset`
headers. Shared status rendering shows the exact retry time and leaves unrelated
read-only controls available. It never starts a retry loop automatically.

- [ ] **Step 4: Render budget authority**

Display calls/input/output remaining and UTC reset time. Use progress bars with
accessible text. At zero remaining or unknown budget state:

- disable chat/analyze buttons that would invoke a model;
- leave read-only broker refresh available if its own policy permits;
- explain `Provider call blocked before network I/O`;
- never suggest editing database counters.

- [ ] **Step 5: Render and queue signed candidates**

Use DOM APIs:

```javascript
const text = document.createElement("p");
text.textContent = candidate.payload.thesis;
```

Never decode or execute candidate text. Queue requires a nonblank operator
reason and an explicit button click. The queue response is rendered as a
proposal receipt, not a fill/order receipt.

The `/chat` response contract consumed here is:

```json
{
  "reply": "Research text",
  "candidates": [
    {"version": 1, "kind": "order", "payload": {}, "signature": "..."}
  ]
}
```

No candidate is automatically queued after chat.

- [ ] **Step 6: Bound refresh activity**

Use one `AbortController` per resource, abort superseded requests, 10-second
fetch timeout, and one 30-second refresh interval only while
`document.visibilityState === "visible"`. Manual refresh cancels and replaces
the current cycle. `pagehide` clears timers and aborts.

- [ ] **Step 7: Run behavior/security tests**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security.py -k "posture or candidate or stale or abort" -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/trading_assistant/app/static/js/posture.js src/trading_assistant/app/static/js/candidates.js src/trading_assistant/app/static/js/auth.js src/trading_assistant/app/static/js/index.js tests/test_frontend_ui.py tests/test_security.py
git commit -m "feat(ui): render bounded security and candidate state"
```

---

### Task 5: Rework the plans workspace as research evidence

**Files:**

- Modify: `src/trading_assistant/app/static/plans.html`
- Modify: `src/trading_assistant/app/static/js/plans.js`
- Modify: `src/trading_assistant/app/static/css/console.css`
- Modify: `tests/test_frontend_ui.py`
- Modify: `tests/test_security.py`

**Interfaces:**

- Keeps existing screener/analyze/propose/approval/cancel API behavior.
- Adds visible model-budget, untrusted-source, regime, earnings, correlation,
  and evidence-boundary states.

- [ ] **Step 1: Add failing plans-page contract tests**

Require:

- explicit `Research / paper-only` badge;
- provider budget summary;
- screener and analysis controls with reason labels;
- selected plan detail with thesis, bull/bear, catalysts, risks,
  uncertainties, regime, market context, relative strength, earnings, sizing,
  invalidation, and source references;
- separate `Proposed`, `Approved`, `Armed`, `Canceled`, and `Blocked` state copy;
- plan approval dialog stating that it arms paper rules and does not prove
  profitability;
- no “AI pick”, “winner”, “guaranteed”, or “live” copy.

- [ ] **Step 2: Run and verify missing evidence hierarchy**

```bash
uv run pytest tests/test_frontend_ui.py -k plans -v
```

Expected: FAIL.

- [ ] **Step 3: Build the three-pane research layout**

Desktop:

```text
filter/actions rail | plan queue | selected evidence
```

Compact/mobile:

```text
actions
plan queue
selected evidence
```

Rows show symbol, action, confidence, regime, freshness, status, and paper
badge. Confidence is never color-coded green as a correctness claim.

- [ ] **Step 4: Render structured evidence safely**

Use definition lists and text nodes for all analyst content. Show
`UntrustedSummary.injection_flags` as caution metadata. If sources are absent or
the summary failed, render `External context unavailable`; do not retain a
previous plan's sources.

Provider budget denial disables paid analysis/proposal generation but keeps
saved-plan review and cancellation available.

- [ ] **Step 5: Preserve approval/cancellation safety**

Approval keeps exact plan ID, symbol, action, operator reason, recent auth,
idempotency, and server response. Cancellation remains separate. No card click
may approve or cancel.

- [ ] **Step 6: Run plans/security suites**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security.py tests/test_plans_api.py -k "plans or plan" -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/trading_assistant/app/static/plans.html src/trading_assistant/app/static/js/plans.js src/trading_assistant/app/static/css/console.css tests/test_frontend_ui.py tests/test_security.py
git commit -m "feat(ui): turn plans into evidence workspace"
```

---

### Task 6: Rework backtests as clearly simulated comparative evidence

**Files:**

- Modify: `src/trading_assistant/app/static/backtests.html`
- Modify: `src/trading_assistant/app/static/js/backtests.js`
- Modify: `src/trading_assistant/app/static/css/console.css`
- Modify: `tests/test_frontend_ui.py`
- Modify: `tests/test_security.py`
- Modify: `tests/test_backtests_api.py`

**Interfaces:**

- Permanent warning text: `Simulated — past performance does not predict future results.`
- Displays run ceilings and provider budget before start.
- Uses local SVG/DOM charts only.

- [ ] **Step 1: Add failing simulation-evidence tests**

Require the permanent warning:

- above every run form;
- above every report;
- adjacent to every equity/drawdown chart;
- present when no report is selected.

Require development/validation/holdout labels, benchmark side-by-side metrics,
fees/slippage, run duration, data range, symbols, regime attribution,
episode breakdown, and holdout-access status.

- [ ] **Step 2: Run and verify incomplete report contract**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_backtests_api.py -k backtest -v
```

Expected: FAIL.

- [ ] **Step 3: Build the launch/evidence layout**

Left rail:

- hard runtime/symbol/date limits;
- provider LLM disabled/enabled state;
- daily run budget;
- one-active-run state;
- explicit run reason.

Main:

- run metadata/status;
- strategy vs buy-and-hold metric table;
- equity curve;
- drawdown curve;
- regime attribution;
- historical episodes;
- holdout audit.

- [ ] **Step 4: Render charts with safe SVG nodes**

Use `document.createElementNS()` and numeric arrays from the API. Reject
non-finite points. Set chart title/description nodes, keyboard-readable summary,
and visible legend. Do not use HTML injection, remote chart libraries, canvas
pixel-only information, or animated profit effects.

- [ ] **Step 5: Make budget/run denials explicit**

`429`, `409 backtest_busy`, provider-budget denial, timeout, cancellation, and
holdout refusal each get distinct status copy and request ID. A failed refresh
clears the old selected report.

- [ ] **Step 6: Run backtest UI/API tests**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security.py tests/test_backtests_api.py -k "backtest or simulated or chart" -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/trading_assistant/app/static/backtests.html src/trading_assistant/app/static/js/backtests.js src/trading_assistant/app/static/css/console.css tests/test_frontend_ui.py tests/test_security.py tests/test_backtests_api.py
git commit -m "feat(ui): present backtests as simulated evidence"
```

---

### Task 7: Finish the secure login and responsive accessibility states

**Files:**

- Modify: `src/trading_assistant/app/static/login.html`
- Modify: `src/trading_assistant/app/static/js/login.js`
- Modify: `src/trading_assistant/app/static/css/console.css`
- Modify: `src/trading_assistant/app/static/index.html`
- Modify: `src/trading_assistant/app/static/plans.html`
- Modify: `src/trading_assistant/app/static/backtests.html`
- Modify: `tests/test_frontend_ui.py`
- Modify: `tests/test_security.py`

**Interfaces:**

- Login states HTTPS loopback boundary without exposing secret retrieval.
- Every dialog traps/restores focus and supports Escape/cancel.
- Mobile preserves dangerous-action context.

- [ ] **Step 1: Write accessibility and responsive contract tests**

Assert:

- logical heading order;
- named nav/main/aside/dialog regions;
- visible labels and error associations;
- `aria-live` only on bounded status regions;
- dialogs have labelled title/description;
- destructive controls include text, not color/icon alone;
- touch targets are at least 40×40 CSS pixels;
- focus styles are not removed;
- reduced-motion rule exists;
- no horizontal page overflow at 390px;
- tables use a scroll wrapper with a visible cue rather than clipping.

- [ ] **Step 2: Run and verify failures**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security.py -k "accessibility or responsive or login" -v
```

Expected: FAIL.

- [ ] **Step 3: Rework login**

Show:

- original mark and product name;
- `Local HTTPS operator console`;
- `Alpaca paper only`;
- one password field;
- no token-copy or `.env` instructions;
- stable login error and retry delay;
- note that the secret is cleared before request and never stored by the page.

On submit, immediately copy the value into the request body, clear the DOM
input, send once, and clear the local variable in `finally`.

- [ ] **Step 4: Implement shared dialog focus behavior**

In `auth.js`, provide:

```javascript
openModal(dialog, initialFocus)
closeModal(dialog)
```

Store the opener, focus the first meaningful control, contain Tab/Shift+Tab,
close on Escape when cancellation is safe, and restore focus. Panic/approval
submission cannot close by backdrop click during an in-flight request.

- [ ] **Step 5: Complete responsive and preference styles**

- desktop side rail fixed within content, never viewport-overlay;
- compact two-column proof grid;
- mobile single-column with sticky environment strip;
- no sticky approval button detached from its order context;
- `prefers-reduced-motion: reduce` removes transitions/animations;
- `forced-colors: active` adds system borders and preserves focus;
- print hides controls and labels every report paper/simulated.

- [ ] **Step 6: Run all frontend/static tests**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/trading_assistant/app/static/login.html src/trading_assistant/app/static/js/login.js src/trading_assistant/app/static/css/console.css src/trading_assistant/app/static/index.html src/trading_assistant/app/static/plans.html src/trading_assistant/app/static/backtests.html src/trading_assistant/app/static/js/auth.js tests/test_frontend_ui.py tests/test_security.py
git commit -m "feat(ui): complete accessible secure console"
```

---

### Task 8: Verify the console in a real browser at desktop and mobile widths

**Files:**

- Modify: `tests/test_frontend_ui.py`
- Modify: `README.md`
- Create verification artifacts under untracked `.local/verification/ui/`

**Interfaces:**

- Uses the strict HTTPS app with injected mock broker/data and no provider calls.
- Produces desktop/mobile screenshots and a console/network audit.

- [ ] **Step 1: Start an isolated mock-data UI instance**

Use a temporary copied database and injected mock broker. Do not use the user's
normal database, Alpaca credentials, Keychain items, daemon, or provider
backends. Serve through the same HTTPS transport policy on a non-conflicting
loopback port and seed:

- tripped equity breaker;
- unknown/stale daemon;
- pending proposal near expiry;
- positions and one empty state;
- exhausted model output budget;
- one signed unqueued candidate;
- completed and failed backtest runs.

- [ ] **Step 2: Inspect at exact viewports**

Use browser automation with:

- desktop: 1440×900;
- compact: 1024×768;
- mobile: 390×844;

For Operations, Plans, Backtests, and Login:

- capture full-page screenshot;
- inspect console errors/warnings;
- inspect failed network requests;
- tab through controls;
- open/close every dialog;
- test loading, empty, stale, blocked, error, and success states;
- verify no horizontal viewport overflow.

- [ ] **Step 3: Check visual safety invariants**

Confirm visually:

- `ALPACA PAPER` is always visible;
- tripped/unknown state is not green;
- security posture and budgets are readable without scrolling past decisions;
- proposal, approval, submission, and fill labels are distinct;
- simulated warning is adjacent to charts;
- candidate queueing cannot be mistaken for execution;
- no Kraken mark/text appears.

- [ ] **Step 4: Add deterministic regressions discovered by browser review**

For every browser finding, first add a failing assertion to
`tests/test_frontend_ui.py` or `tests/test_security.py`, then fix the smallest
HTML/CSS/JS surface and rerun the focused test.

- [ ] **Step 5: Run frontend and full suites**

```bash
uv run pytest tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py tests/test_api.py tests/test_plans_api.py tests/test_backtests_api.py -v
uv run pytest
```

Expected: all tests PASS with only the documented skip.

- [ ] **Step 6: Update README with verified screenshots**

Reference screenshots from documentation only after inspection. Captions must
state `Alpaca paper`, the seeded blocker/stale condition, and
`Simulated — past performance does not predict future results` where relevant.
Do not commit local certificates, databases, secrets, or session data.

- [ ] **Step 7: Commit**

```bash
git add tests/test_frontend_ui.py README.md src/trading_assistant/app/static
git commit -m "test(ui): verify dark console across viewports"
```

---

## Plan 3 completion checkpoint

Run:

```bash
git status --short
uv run pytest tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py -v
uv run pytest
uv run python scripts/check_release_safety.py
```

Required result:

- complete frontend/static, full pytest, and release-gate pass;
- desktop/compact/mobile browser inspection has no console or network errors;
- no unsafe DOM API, remote asset, inline script, or Kraken trademark asset;
- paper, blocker, freshness, provider-budget, and simulated states remain
  explicit;
- no UI action bypasses candidate queueing, approval, risk, idempotency, or
  recent authentication;
- browser verification made no broker/provider call and did not start the
  daemon or reset a breaker.
