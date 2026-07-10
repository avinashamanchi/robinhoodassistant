# Phase 7 — Backtesting Harness, Signal Library & Stress Scenarios

**Date:** 2026-07-09
**Status:** Approved — build deterministic harness now; crypto in scope; LLM-in-the-loop deferred.
**Builds on:** Phases 1–3 (Phases 4–6 not yet built; LLM/analyst integration deferred).

---

## Scope decisions

1. **Deterministic harness now.** Signals, strategies, data layer, sim broker, replay
   engine, walk-forward on baselines, and the full stress suite. **Deferred until Phase 6
   exists:** `backtest/llm_runner.py`, wiring `MarketFeatures` into an analyst prompt, and
   graded-call linkage to `AnalysisReport`s. `MarketFeatures` and `playbook.md` are still
   built now (strategies consume `MarketFeatures`; the playbook ships as a doc).
2. **Crypto is a real asset class in the live path** (not backtest-only). This modifies
   Phase 1–3 code (§1). **Hard condition:** the entire existing Phase 1 risk + kill-switch
   suite must pass unchanged, plus new tests proving equity/crypto trip independently and
   the crypto clock is always-open while the equity clock is unaffected.

---

## 1. Asset-class abstraction (modifies live-path code)

`assets.py`: `class AssetClass(str, Enum): EQUITY="equity"; CRYPTO="crypto"` and
`AssetClass.for_symbol(sym)` — a symbol containing "/" (e.g. `BTC/USD`) is CRYPTO, else
EQUITY (config crypto allowlist is authoritative).

| Module | Change | Backward compat |
|--------|--------|-----------------|
| `db/models.py` | `killswitch_state` keyed by `asset_class` (one row/class) | methods default `asset_class=EQUITY` |
| `risk/killswitch.py` | `is_tripped/trip/reset/evaluate_daily_loss(session, ..., asset_class=EQUITY)` | equity default preserves current tests |
| `risk/pnl.py` | `most_recent_daily_boundary(now, asset_class)`: equity=NY 09:30 (existing), crypto=UTC midnight; `realized_pnl_today(fills, now, asset_class=EQUITY)` | equity path unchanged |
| `risk/clock.py` | add `CryptoClock` (`is_open()` always True) | FakeClock/AlpacaClock unchanged |
| `config.py`, `config.yaml` | add `risk.crypto` sub-limits + crypto allowlist; add `backtest:` section | new sections only |
| `service.py` | resolve asset class per order → per-class kill switch, clock, P&L boundary, and RiskConfig | equity behavior identical |

The pure `RiskEngine.check` stays asset-class-agnostic (still takes `killswitch_tripped` +
`market_open` bools). The **service** picks which kill switch / clock / risk config to use
per the order's asset class — so the purity invariant (A1) is preserved.

---

## 2. Signal library (`signals/`)

- `indicators.py` — SMA/EMA(20/50/200), MACD, ADX; RSI(14), ROC(10); ATR(14), Bollinger
  Bands, realized vol(20d); volume vs 20d avg. **(OBV cut — redundant with volume_vs_20d.)**
- `structure.py` — swing-pivot support/resistance, distance to 52-wk high/low, gap detection,
  consecutive up/down days.
- `events.py` — typed timestamped detectors: GOLDEN_CROSS/DEATH_CROSS, BREAKOUT/BREAKDOWN
  (close vs resistance on >1.5× avg volume), RSI_OVERSOLD/OVERBOUGHT (+divergence),
  BB_SQUEEZE (bandwidth bottom decile), GAP_UP/GAP_DOWN.
- `regime.py` — TRENDING_UP/DOWN, RANGING, HIGH_VOLATILITY (ADX + SMA slope + realized-vol
  percentile).
- `features.py` — the `MarketFeatures` pydantic model (§3).
- `playbook.md` — heuristics-not-laws doc (§4).

## 3. `MarketFeatures` schema

```
MarketFeatures (extra="forbid"):
  symbol; asset_class; as_of(UTC == t, never future)
  recent_bars: list[Bar]                         # adjusted OHLCV window
  last_close; prev_close
  trend:      sma_20/50/200, ema_20/50/200, macd{line,signal,hist}, adx_14,
              sma50_slope, price_vs_sma200_pct
  momentum:   rsi_14, roc_10
  volatility: atr_14, bb_{upper,mid,lower}, bb_bandwidth, realized_vol_20
  volume:     volume, volume_vs_avg20            # (OBV removed)
  structure:  support_levels[], resistance_levels[], dist_to_52w_high_pct,
              dist_to_52w_low_pct, gap_pct, consecutive_up_days, consecutive_down_days
  events:     list[EventTag]
  regime:     Regime
  # ── added per review ──
  days_to_next_earnings: Optional[int]           # None for crypto
  market_context: { spy_regime: Regime, spy_realized_vol_20_pct: float }
  relative_strength_vs_spy: { rs_20d: float, rs_60d: float }
```

**Every field, including `market_context` and `relative_strength_vs_spy`, is computed from a
`DataView` bounded at t** — SPY bars are pulled through the same no-lookahead view as the
single name, so market context cannot leak future data (guardrail-tested, §6/§8).

## 4. `playbook.md` sections

Preamble (heuristics, mixed evidence, must cite) · Trend · Momentum (RSI-oversold-in-downtrend
failure) · Volatility · Volume · Structure · Regime conditioning · Position management ·
**Earnings handling** (before any earnings date inside the holding horizon the analyst must
explicitly reduce, exit, or accept the gap — silence disallowed) · **Correlation** (multiple
positions in highly correlated names count as concentration; the analyst must flag it) ·
Uncertainty & citation.

## 5. Baseline strategies (`strategies/`)

Common interface `Strategy.on_bar(features) -> Signal(BUY/SELL/HOLD, size_hint?)`.
`sma_crossover`, `rsi_reversion` (non-downtrend only), `breakout` (ATR trailing stop),
`buy_and_hold`. All use the **same risk engine + sizing** as the (future) LLM path.

## 6. Backtest harness (`backtest/`)

- `data.py` — Alpaca daily+hourly bars (equities + crypto), corporate-action-adjusted,
  cached to parquet. A deterministic **synthetic generator** provides bars for tests (no
  network/keys required in CI).
- `sim_broker.py` — implements `BrokerClient`. Market fills at **next bar open**; limit fills
  only if bar range crosses; **partial fills** capped at `max_participation_pct` of bar
  volume (remainder carries). Slippage + fees per §7 → emits real `Fill` rows.
- `engine.py` — event-driven replay. **`DataView`**: at sim-time t, physically returns only
  rows with ts ≤ t; requesting future rows raises `LookaheadError`. Unit test proves it (for
  single name AND SPY market context).
- `evaluate.py` — walk-forward: rolling train/validate + a **sacred 12-month holdout**.
  Metrics per strategy: total return, CAGR, Sharpe, Sortino, max DD, win rate, profit factor,
  avg win/loss, exposure %, turnover, **P&L attribution by regime**. Buy-and-hold shown
  side-by-side always. Persists to DB.
- `situations.py` — labeled historical episodes (COVID crash, 2022 bear, meme squeezes,
  crypto winter 2022) + auto-detected (30-day windows >15% DD / >20% rally / vol-regime flip).
- `holdout.py` — `HoldoutGuard`: refuses parameter sweeps against the holdout window; logs any
  access to a `holdout_access_log` table.
- **Deferred:** `llm_runner.py` (trigger-mode, response cache, cheap-model config, hard
  `max_llm_calls_per_run` budget + pre-run cost estimate).

## 7. Fee / slippage / fill model (`config.yaml → backtest:`)

Fees and slippage are **separate** (per review):

```yaml
backtest:
  fills:
    market: next_bar_open
    limit: bar_range_cross
    max_participation_pct: 10
  slippage_bps:
    equity: 5
    crypto: 20
  fees_bps:                 # taker fees, distinct from slippage
    equity: 0               # Alpaca equities commission-free
    crypto: 25              # Alpaca crypto worst taker tier (configurable)
  holdout_months: 12
```

Fill price = reference × (1 ± slippage); fees deducted separately per trade. Crypto is charged
both the 25 bps fee AND 20 bps slippage — under-charging crypto churn would flatter the exact
asset class with the most turnover.

## 8. Guardrails (permanent)

1. **Holdout is sacred** — `HoldoutGuard` blocks sweeps on it and logs access.
2. Backtest results **never auto-enable** anything; promotion stays a manual config change and
   the Phase 6 "50 graded calls" gate still applies on top (when Phase 6 exists).
3. Every simulated result in the UI carries: **"Simulated — past performance does not predict
   future results."**
4. **No lookahead** — the `DataView` test (single name + SPY context) runs in CI forever.

## 9. Reporting & UI

`GET /backtests`, `POST /backtests/run`, `GET /backtests/{id}/report`. UI: equity curves
(strategy vs buy-and-hold), drawdown, metrics table, per-regime + per-episode breakdown, all
stamped Simulated.

## 10. Build order & review checkpoints

`assets` + config + **§1 asset-class live-path change (existing suite must stay green)** →
`signals` → `strategies` → `data` (synthetic for tests) → **`sim_broker` + `engine` +
no-lookahead `DataView` test** ⏸️ **STOP for review** → `evaluate` + **first walk-forward
baseline report** ⏸️ **STOP for review (before any LLM run)** → `situations` → `stress/` → UI.

New deps: `pandas`, `numpy`, `pyarrow`, `ta`.
