# Trading Assistant

An LLM-driven agentic trading assistant (Alpaca broker, Model Context Protocol).
**Human-gated, risk-enforced, paper-first.** The LLM proposes; a human approves;
a deterministic risk engine is the final authority on every order.

> ⚠️ Educational / paper-trading project. Live trading is OFF by default and
> requires a double-lock (config flag **and** an environment confirmation string).

## Status

Built in phases (see `docs/superpowers/specs/`):

- **Phase 1 ✅** — scaffold, config, DB models + order state machine, `BrokerClient`
  ABC + `MockBroker`, risk engine (pure) with FIFO P&L + persistent kill switch +
  injectable market clock. Full pytest coverage of every limit.
- **Phase 2 ✅** — `AlpacaBroker` (paper) + `AlpacaClock` + MCP server + `TradingService`.
- **Phase 3 ✅** — FastAPI host, agentic loop (Claude tool use), human approval gate
  with execution-time risk re-check, rate limiting, single-page UI.
- **Phase 4 ✅** — monitoring daemon (conditional rules, one-shot, crash-safe) + Telegram.
- **Phase 5 ✅** — hardening: partial fills, fill idempotency, cancel/replace,
  startup reconciliation, kill-switch drill.
- **Phase 7 (harness) ✅** — signal library, baseline strategies, event-driven
  backtester (no-lookahead), walk-forward + sacred holdout, historical situations,
  synthetic stress suite, crypto as an independent asset class. LLM-in-the-loop
  backtesting is deferred until the Phase 6 analyst exists.

## Backtesting (Phase 7)

Deterministic indicators are computed in code (`signals/`); the LLM only ever
*interprets* a `MarketFeatures` bundle. Baseline strategies (`strategies/`) and the
harness (`backtest/`) benchmark everything against buy-and-hold.

```bash
# Run a synthetic walk-forward (no credentials needed) and open the report UI:
uv run uvicorn trading_assistant.app.main:create_app --factory --reload
# visit http://127.0.0.1:8000/backtests/ui  → "Run new backtest"

# Real data (equities + crypto), cached to parquet, adjusted for corp actions:
#   backtest.data.download_alpaca_bars(symbol, ALPACA_API_KEY, ALPACA_SECRET_KEY)
```

**Reading the report.** Each strategy is shown side-by-side with buy-and-hold on
the same symbol and window, with return, Sharpe, Sortino, max drawdown, win rate,
profit factor, exposure, turnover, and **P&L attributed by regime**. The number
that matters most is a strategy's holdout result vs buy-and-hold.

**Walk-forward & holdout.** History splits into a *development* window (where any
tuning would happen) and a **sacred holdout** — the most recent 12 months, which
`HoldoutGuard` refuses to run parameter sweeps against and logs every access to.
The holdout is evaluated once, never tuned on; if performance collapses there
versus development, the strategy overfit.

**Guarantees.** No-lookahead is structural — a `DataView` physically cannot return
rows after the simulated time `t` (SPY market context flows through the same view).
Every simulated result carries the label *"Simulated — past performance does not
predict future results."* Backtest results never auto-enable anything.

The `tests/stress/` suite regression-tests **safety** (not profit) against flash
crashes, gap-through-stop fills, whipsaw position limits, stale-data halts,
independent crypto/equity kill switches, stale-approval rejection, and duplicate-
fill idempotency.

## Quickstart (Phase 1)

```bash
uv venv --python 3.11
uv pip install -e '.' pytest pytest-cov pyyaml
cp .env.example .env      # fill in when you reach Phase 2
uv run pytest             # run the suite
```

## Running

```bash
# API + UI (chat, approvals, positions, backtests):
uv run uvicorn trading_assistant.app.main:create_app --factory --reload

# Monitoring daemon (evaluates conditional rules against live quotes):
uv run python -m trading_assistant.daemon.main
```

Order lifecycle is hardened: partial fills advance PARTIALLY_FILLED → FILLED,
duplicate broker fill events are idempotent (`broker_fill_id`), `POST
/orders/{id}/cancel` cancels live orders, `POST /reconcile` compares broker
positions to local truth and logs drift, and the daily-loss kill switch trips
per asset class (`enforce_daily_loss_limits`).

## Safety model

1. Live trading requires BOTH `config.yaml` `trading.mode: live` AND
   `LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING`.
2. The LLM only ever produces `PROPOSED` orders. Execution needs human approval
   (or an explicitly pre-approved rule).
3. The risk engine runs on every order and cannot be bypassed.
4. Everything dangerous defaults OFF.

Configuration lives in `config.yaml` (risk limits, non-secret) and `.env`
(secrets, gitignored — see `.env.example`).
