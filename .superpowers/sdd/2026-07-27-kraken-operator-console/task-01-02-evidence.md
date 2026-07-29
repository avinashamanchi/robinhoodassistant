# Plan 3 Tasks 1–2 evidence

## Scope

Implemented only the shared visual/semantic contract and dark component system.
No runtime database, Keychain, credential, service, provider, broker, daemon,
breaker, order, or notification action occurred.

The work is based on safety commits `9dc0c47` and `4e159ee`. Neither
`src/trading_assistant/bootstrap.py` nor
`tests/test_plan2_integration_correction.py` was edited or staged by this task.

## Frontend-design direction

- Subject: one local operator deciding whether Alpaca paper actions are safe
  from broker and server evidence.
- Palette: the approved canvas, surface, purple action, verified, caution, and
  blocked tokens in `DESIGN.md`.
- Type: system sans for operator language and system monospace for proof,
  timestamps, quantities, and status metadata.
- Layout: a compact product topbar above a persistent evidence strip, followed
  by the existing page-specific workspaces.
- Signature: an original proof-orbit mark made from two offset arcs and one
  evidence node.

Self-critique changed the initial dark-dashboard direction in two ways. The
shared strip reports unknown/stale evidence instead of decorative market
metrics, and the distinctive visual gesture is limited to the proof orbit.
This avoids a generic exchange dashboard and keeps the interface about
authority and evidence.

## TDD

RED:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security.py -v
16 failed, 107 passed in 10.91s
```

The failures were the intended missing contract: approved dark tokens,
original local mark, shared environment strip, login skip/main landmark,
component states, and the replacement static asset.

GREEN:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security_headers.py -v
29 passed in 0.83s

uv run pytest tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py -v
131 passed in 6.87s
```

Final focused rerun:

```text
uv run pytest tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py -q
131 passed
```

Static checks:

```text
uv run python -m compileall -q src/trading_assistant/app tests/test_frontend_ui.py tests/test_security.py tests/test_security_headers.py
PASS

uv run python scripts/check_release_safety.py
release static checks: PASS

git diff --check
PASS
```

## Preservation review

Compared every HTML `id` with committed HEAD before the UI change:

```text
index.html: preserved 88/88; missing []
plans.html: preserved 38/38; missing []
backtests.html: preserved 18/18; missing []
login.html: preserved 4/4; missing []; added [main-content]
```

All four pages retain one local stylesheet and one local ES module, contain no
inline script/style or remote asset, visibly identify Trading Assistant and
ALPACA PAPER, and use the original local mark. Authenticated pages retain all
route links, operator/session controls, forms, dialogs, CSRF/auth behavior
hooks, and safety copy.

## Implementation commit

`034f99b feat(ui): establish dark operator design system`

## Residuals

- This task is a static shared-system release, not browser visual verification;
  isolated mock browser checks are reserved for Plan 3 Task 8.
- Environment strip values intentionally remain unknown/stale until later
  behavior tasks bind them to server evidence.
- The legacy `flight-deck.svg` file remains unreferenced for history safety; no
  page, assertion, or served-asset contract uses it.
