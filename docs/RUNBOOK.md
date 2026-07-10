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

## Promotion to live — MY decision, never automated

The system will NEVER enable live trading. Promoting the analyst toward live is a
manual config change I make only when ALL of these hold, and I decide they hold:

- **≥ 50 graded calls** for the asset class (the code gate), AND
- analyst **beats buy-and-hold on the holdout** (not just dev), AND
- **calibration** within tolerance (0.8-confidence calls win ≈ 80%), AND
- a stretch of **shadow-mode** grades on live data agrees with the backtest.

Even then, live requires BOTH `trading.mode: live` AND
`LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING`. Paper for months first.
