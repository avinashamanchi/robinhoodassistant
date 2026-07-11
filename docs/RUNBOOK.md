# Paper Trading Runbook

Operating guide for running the assistant in **paper** mode. Live trading stays
double-locked OFF; Robinhood stays read-only.

## One-time setup

```bash
uv pip install -e '.[broker,mcp,app,analysis,external,llm,marketdata]'
cp .env.example .env          # fill in keys
openssl rand -hex 32          # -> paste into APP_API_TOKEN in .env
```
Required in `.env`: `APP_API_TOKEN` (≥32 hex), one LLM key (`GEMINI_API_KEY` /
`GROQ_API_KEY`), `ALPACA_API_KEY` + `ALPACA_SECRET_KEY`. Optional: Telegram, Robinhood.

## Every morning

1. **Preflight** (do not skip):
   ```bash
   uv run python -m trading_assistant.preflight
   ```
   Exit 0 / `=> READY` means go. Any `FAIL` blocks the day; `NEEDS-ME` means a key
   is missing. If kill switches show TRIPPED, investigate `risk_events` before reset.
2. **Start the app** (UI + API):
   ```bash
   uv run uvicorn trading_assistant.app.main:create_app --factory --host 127.0.0.1 --port 8000
   ```
3. **Start the daemon** (evaluates rules, writes heartbeat):
   ```bash
   uv run python -m trading_assistant.daemon.main
   ```
4. Open http://127.0.0.1:8000 — the UI prompts once for your `APP_API_TOKEN`.
5. Check `GET /health` shows `daemon_alive: true`.

## Daily routine

- **Chat** proposes orders; they land in **Pending approvals**. You **Approve**
  (re-runs the risk engine at execution) or **Reject**. Nothing executes without you.
- **Plans & Screener** (`/plans/ui`): run the screener, Analyze a candidate → a full
  sized `TradePlan`. Approving a plan arms its pre-approved rule set; the daemon runs
  the ladder/exits **only if** `features.auto_execute_preapproved_rules: true`.
- **Panic** button (or `POST /panic`): cancels open orders, disables all rules, trips
  both kill switches. Use if anything looks wrong.

## Reading alerts / log

- `killswitch_trip` — a daily-loss limit was breached; new orders for that class are
  blocked until you `POST /killswitch/reset`.
- `rejection` — risk engine refused an order (reason in the message).
- `warning` — non-blocking (e.g. cross-broker concentration).
- `reconciliation` — broker vs local position drift; investigate.

## Weekly review

- Scorecard (`GET /analyst/scorecard`): graded calls, accuracy, promotion status.
- Drawdown + `risk_events` for the week.
- Backtest holdout report — has anything changed?

## Keeping it running (see `docs/ops/`)

- `launchd` plist (macOS) / `systemd` unit for app + daemon auto-restart.
- Nightly backup cron: `sqlite3 trading_assistant.db ".backup ..."`, 14-day retention.

## Analyst version + scorecard reset

The analyst is versioned (`analyst.version`, currently **v2**). The scorecard grades
only the current version — v1's grades never transfer to a changed analyst. To
reset after any change to the analyst's logic or prompt, **bump `analyst.version`**
(e.g. v2 → v3); shadow mode then grades the new version from zero.

**v2 changes (from the first accuracy report — 51% hit, overconfident):**
- **Suppress RANGING** — the analyst returns NO_TRADE in ranging regimes
  (`analyst.suppress_ranging: true`). Don't take directional trades in directionless
  markets.
- **Confidence neutralized** — the confidence field is still emitted and graded, but
  it sizes/weights/filters NOTHING until calibration proves out.

## Promotion to live — MY pre-registered decision, never automated

Decided **before** seeing v2's results (anti-goalpost-moving). Promotion is a manual
config change I make only when ALL hold and I decide they hold:

- **≥ 60 graded calls** for the asset class (code gate is ≥50; my bar is 60), AND
- hit rate whose **confidence interval clears 50%** (not just point estimate), AND
- **Brier < 0.25** (calibration beats no-skill), AND
- **does not lose to buy-and-hold** over the same window.

Discipline while grading: **ration holdout runs to once per major version** (iterating
the prompt against the holdout overfits it through my own eyes); do NOT add indicators/
data sources/second-LLM voting to chase accuracy (more parameters = more overfit — the
fix is sample size, not inputs); do NOT read the trending-up hit rate as edge (being
long in an uptrend is what buy-and-hold does for free).

Even then, live requires BOTH `trading.mode: live` AND
`LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING`. Paper for months first. If v2's live
sample looks like v1's, the honest conclusion stands: the assistant is a superb
execution/monitoring system, the "what to buy" layer stays advisory, and an index fund
keeps the crown.
