"""Signal library: indicators, structure, events, regime, feature assembly."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_assistant.assets import AssetClass
from trading_assistant.backtest.synthetic import make_bars, make_trend, ohlcv_from_closes
from trading_assistant.signals import indicators as ind
from trading_assistant.signals import regime as rg
from trading_assistant.signals import structure as st
from trading_assistant.signals.events import detect_events
from trading_assistant.signals.features import build_features
from trading_assistant.signals.models import EventType, MarketFeatures, Regime


# ── indicators ──────────────────────────────────────────────────
def test_sma_matches_manual():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert ind.sma(s, 3).iloc[-1] == 4.0  # mean(3,4,5)


def test_rsi_bounded_0_100():
    df = make_bars(200, seed=1)
    r = ind.rsi(df["close"], 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_atr_positive():
    df = make_bars(100, seed=2)
    a = ind.atr(df, 14).dropna()
    assert (a > 0).all()


def test_rsi_all_gains_is_100():
    s = pd.Series(np.arange(1, 50, dtype=float))  # monotonically rising
    assert ind.rsi(s, 14).iloc[-1] == 100.0


# ── structure ───────────────────────────────────────────────────
def test_consecutive_up_days():
    df = ohlcv_from_closes([100, 101, 102, 103])
    up, down = st.consecutive_days(df)
    assert up == 3 and down == 0


def test_gap_detection():
    df = ohlcv_from_closes([100, 100, 100])
    # Force a gap on the last bar: open well above prior close.
    df.iloc[-1, df.columns.get_loc("open")] = 110.0
    assert st.last_gap_pct(df) == 10.0


# ── events ──────────────────────────────────────────────────────
def test_golden_cross_detected_somewhere():
    df = make_trend(n_base=210, n_move=120, start=100.0, end=180.0)
    found = any(
        any(e.type is EventType.GOLDEN_CROSS for e in detect_events(df.iloc[:i]))
        for i in range(201, len(df))  # from the first bar SMA-200 exists
    )
    assert found


def test_death_cross_detected_somewhere():
    df = make_trend(n_base=210, n_move=120, start=180.0, end=90.0)
    found = any(
        any(e.type is EventType.DEATH_CROSS for e in detect_events(df.iloc[:i]))
        for i in range(201, len(df))
    )
    assert found


# ── regime ──────────────────────────────────────────────────────
def test_uptrend_classified_trending_up():
    df = make_trend(n_base=20, n_move=280, start=100.0, end=260.0)
    assert rg.classify(df) is Regime.TRENDING_UP


def test_short_history_returns_none():
    df = make_bars(10, seed=3)
    assert rg.classify(df) is None


# ── features ────────────────────────────────────────────────────
def test_build_features_populated_on_long_series():
    df = make_bars(400, seed=4)
    spy = make_bars(400, seed=99)
    f = build_features("AAPL", AssetClass.EQUITY, df, spy_df=spy)
    assert isinstance(f, MarketFeatures)
    assert f.sma_200 is not None
    assert f.rsi_14 is not None and 0 <= f.rsi_14 <= 100
    assert f.regime is not None
    assert f.market_context.spy_regime is not None
    assert f.relative_strength_vs_spy.rs_20d is not None
    assert len(f.recent_bars) == 20


def test_build_features_short_series_no_crash():
    df = make_bars(15, seed=5)
    f = build_features("AAPL", AssetClass.EQUITY, df)
    assert f.sma_200 is None       # not enough history
    assert f.last_close is not None


def test_crypto_has_no_earnings():
    df = make_bars(60, seed=6)
    from datetime import date

    f = build_features(
        "BTC/USD", AssetClass.CRYPTO, df, earnings_date=date(2030, 1, 1)
    )
    assert f.days_to_next_earnings is None  # earnings N/A for crypto
