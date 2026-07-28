# Trading Assistant

An LLM-driven agentic trading assistant (Alpaca broker, Model Context Protocol).
**Human-gated, risk-enforced, paper-only.** The LLM proposes; a human approves;
a deterministic risk engine is the final authority on every order.

> ⚠️ This release rejects live and non-Alpaca production configuration. Passing
> its safety drills does not prove profitability, authorize live trading, or
> guarantee returns.

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
- **Phase 8 ✅** — the decision layer: full `TradePlan` (bear/base/bull scenarios,
  invalidation, entry ladder, exits), deterministic sizing, exit rule types
  (trailing/time stops, progressive OCO), independent entry tranches, exits
  activated and resized only from trusted entry fills, approved-plan →
  human-gated rule proposals, deterministic screener, `/analyze` +
  plans/screener UI, optional Alpaca news.
- **Phase 6 ✅** — LLM analyst (interprets `MarketFeatures` via the playbook,
  cited + regime-conditioned, earnings-aware), scorecard grading vs realized
  forward returns, and a 50-graded-calls promotion gate (advice only — never
  auto-enables; this release rejects live mode at startup).
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
uv run python -m trading_assistant.ops.serve
# visit http://127.0.0.1:8000/backtests/ui  → "Run new backtest"

# Real data (equities + crypto), cached to parquet, adjusted for corp actions:
#   backtest.data.download_alpaca_bars(symbol, ALPACA_API_KEY, ALPACA_SECRET_KEY)
```

**LLM-in-the-loop.** `backtest/llm_runner.py` runs the Phase-6 analyst inside the
harness with cost controls: **trigger-mode** (the analyst only fires on signal
events, not every bar), a response **cache** keyed on (symbol, date, features
hash), a hard **`max_llm_calls` budget** that aborts the run, a printed pre-run
cost estimate, and an optional cheap-model **spot-check** against the full model.
The analyst's calls are graded against realized forward returns and feed the
scorecard — so you can finally compare *analyst vs buy-and-hold on the holdout*.

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

## Quickstart

```bash
uv venv --python 3.11
uv sync --all-extras --dev
cp .env.example .env
openssl rand -hex 32      # use as APP_API_TOKEN
uv run python -m trading_assistant.db.migrate upgrade
uv run pytest
```

## Running

```bash
# API + UI (chat, approvals, positions, backtests):
uv run python -m trading_assistant.ops.serve

# Monitoring daemon (evaluates conditional rules against live quotes):
uv run python -m trading_assistant.daemon.main

# Verify Alpaca's full paper order path with one tiny submit/cancel:
uv run python -m trading_assistant.ops.paper_drill

# Exercise migration, response-loss recovery, OCO, breakers, and reconciliation
# offline against an explicit online SQLite copy (the source is opened mode=ro):
safety_stage="$(mktemp -d)"
safety_dir="$(cd "$safety_stage" && pwd -P)"
uv run python -m trading_assistant.ops.safety_drill \
  --database-copy "$safety_dir/release-safety.sqlite3" --mock

# Install API + watchdog + nightly encrypted backup on macOS:
./scripts/launchd/install.sh
```

Order lifecycle is hardened: partial fills advance PARTIALLY_FILLED → FILLED,
duplicate broker fill events are idempotent (`broker_fill_id`), `POST
/orders/{id}/cancel` cancels live orders, `POST /reconcile` compares broker
positions to local truth and logs drift, and the daily-loss kill switch trips
per asset class (`enforce_daily_loss_limits`). Plan exits are keyed to a
monotonic residual generation rather than timestamps: a delayed fill makes
every older exit intent stale and forces broker-confirmed cancellation before a
replacement. Plan-owned rules cannot be canceled through the generic rule API;
use the plan cancellation workflow, which refuses to abandon confirmed
quantity. Plan-order cancellation intent is persisted independently from broker
error codes, retried after restart, and keeps startup plus the daemon fail-closed
until terminal broker and fill truth are confirmed.

Operational backups retain only verified
`whole-database-v1.sqlite3.aesgcm` artifacts; no plaintext SQLite backup is
published. Detailed backup/migration/restore commands and the optional
credentialed Alpaca paper gate are in
[`docs/RUNBOOK.md`](docs/RUNBOOK.md). Paper trading is a simulation and does not
establish that a strategy will be profitable in live markets.

The mock gate is deterministic and broker-write-free. It exercises a real
restart reconstruction, two independently constructed OCO repository
contenders, breaker persistence, and reconciliation on the copy. Active WAL
sources are supported when their regular `-wal` and `-shm` files already exist;
the drill never creates, deletes, or recovers primary sidecars. Missing required
credentials make preflight exit nonzero with `NOT READY`, not a conditional
ready state.

The checked-in operating profile is intentionally conservative:
`trading.mode: paper`, autonomous pre-approved-rule execution OFF, broker bracket
submission OFF, and shadow analysis ON. Do not enable execution features from
backtest results alone; require the scorecard/paper evidence gates in the runbook
and a separate manual decision.

## LLM providers & market data

The agent/analyst run on a pluggable backend (`llm/`): set `llm.provider` to
`anthropic`, `gemini`, or `groq`. Runtime cross-provider fallback is prohibited:
a provider change requires an explicit configuration edit and process restart, so
the same financial context is never silently sent to a second vendor.
Install with `uv sync --all-extras --dev`. Keys:
`GEMINI_API_KEY` / `GROQ_API_KEY` / `ANTHROPIC_API_KEY` in `.env`.

Historical bars come from Alpaca, **MarketStack** (equities EOD/splits/dividends —
`MARKETSTACK_API_KEY`, cached to parquet since the free tier is ~100 req/month), or
**CoinGecko** (crypto OHLCV, **no key required** — the recommended crypto source).
The same supported-extra install includes the market-data clients.

The abstract read-only external-account protocol and deterministic mock remain for
portfolio tests. No unofficial Robinhood login library or production factory path
is shipped.

## Safety model

1. This safety-foundation runtime is paper-only and rejects live mode at startup.
2. The LLM only ever produces `PROPOSED` orders. Execution needs human approval;
   autonomous pre-approved-rule execution is disabled in the release profile.
3. The risk engine runs on every order and cannot be bypassed.
4. Everything dangerous defaults OFF.
5. Every production runtime role writes redacted, owner-only, bounded rotating
   logs under `logs/`.

A narrowly proven reduce-only order may pass only active loss, drawdown, and
operator-global scopes so an open position can be made smaller. The observed
breach is still persisted atomically. Data, liquidity, broker-drift, stale-fill,
and allocation failures always remain blocking.

Configuration lives in `config.yaml` (risk limits, non-secret) and `.env`
(secrets, gitignored — see `.env.example`).
