"""Decision-time bar policy shared by backtests and live consumers.

Daily strategies decide on completed sessions only. These tests pin the
session arithmetic (time zones, DST, weekends, holidays, early closes, bad
bars) and prove that the live feature path and the backtest engine produce
identical features and decisions from identical bars.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading_assistant.assets import AssetClass
from trading_assistant.risk.clock import MarketClockObservation
from trading_assistant.signals.sessions import (
    FEATURE_LOOKBACK,
    completed_bars,
    decision_bars,
    completed_through,
    session_date,
)

NY = ZoneInfo("America/New_York")
EQUITY = AssetClass.EQUITY
CRYPTO = AssetClass.CRYPTO


def ny(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=NY)


def observed(is_open, most_recent_open):
    return MarketClockObservation(
        is_open=is_open,
        most_recent_open=most_recent_open.astimezone(timezone.utc),
    )


def alpaca_daily(days, *, close_start=100.0):
    """Daily bars labelled the way Alpaca labels them: local midnight in NY."""
    index = pd.DatetimeIndex(
        [ny(d.year, d.month, d.day).astimezone(timezone.utc) for d in days],
        name="ts",
    )
    closes = [close_start + i for i in range(len(days))]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1_000.0] * len(days),
        },
        index=index,
    )


# ── which session a bar belongs to ────────────────────────────────────────────
def test_equity_bars_map_to_their_new_york_session_across_dst():
    summer = ny(2026, 7, 10).astimezone(timezone.utc)   # 04:00 UTC (EDT)
    winter = ny(2026, 12, 10).astimezone(timezone.utc)  # 05:00 UTC (EST)
    assert summer.hour == 4 and winter.hour == 5
    assert session_date(summer, EQUITY) == date(2026, 7, 10)
    assert session_date(winter, EQUITY) == date(2026, 12, 10)


def test_naive_timestamps_are_read_as_utc():
    assert session_date(datetime(2026, 7, 10, 4, 0), EQUITY) == date(2026, 7, 10)
    assert session_date(datetime(2026, 7, 10, 3, 0), EQUITY) == date(2026, 7, 9)


def test_crypto_sessions_are_utc_days():
    assert session_date(datetime(2026, 7, 10, 23, 30, tzinfo=timezone.utc), CRYPTO) == date(2026, 7, 10)


# ── which sessions are complete at the decision instant ──────────────────────
# Real 2026 NYSE sessions around each scenario (weekends/holidays absent).
_SESSIONS_2026 = [
    date(2026, 3, 6), date(2026, 3, 9),
    date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 8),
    date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),
    date(2026, 10, 8), date(2026, 10, 9),
    date(2026, 10, 30), date(2026, 11, 2),
    date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27),
]


@pytest.mark.parametrize(
    ("label", "now", "is_open", "recent_open", "last_kept"),
    [
        # Tuesday 2026-10-06
        ("pre-market", ny(2026, 10, 6, 8, 0), False, ny(2026, 10, 5, 9, 30), date(2026, 10, 5)),
        ("during session", ny(2026, 10, 6, 10, 0), True, ny(2026, 10, 6, 9, 30), date(2026, 10, 5)),
        ("after close", ny(2026, 10, 6, 16, 30), False, ny(2026, 10, 6, 9, 30), date(2026, 10, 6)),
        # Saturday after a normal week
        ("weekend", ny(2026, 10, 10, 11, 0), False, ny(2026, 10, 9, 9, 30), date(2026, 10, 9)),
        # Labor Day 2026-09-07 (closed): the last session is Friday
        ("holiday", ny(2026, 9, 7, 10, 0), False, ny(2026, 9, 4, 9, 30), date(2026, 9, 4)),
        # Day after Thanksgiving 2026-11-27 closes at 13:00; Thursday is a holiday
        ("early close, open", ny(2026, 11, 27, 12, 0), True, ny(2026, 11, 27, 9, 30), date(2026, 11, 25)),
        ("early close, after", ny(2026, 11, 27, 13, 30), False, ny(2026, 11, 27, 9, 30), date(2026, 11, 27)),
        # First session after the November DST change (EST), mid-session
        ("after DST change", ny(2026, 11, 2, 10, 0), True, ny(2026, 11, 2, 9, 30), date(2026, 10, 30)),
        # First session after the March DST change (EDT), after close
        ("after spring DST", ny(2026, 3, 9, 17, 0), False, ny(2026, 3, 9, 9, 30), date(2026, 3, 9)),
    ],
)
def test_completed_equity_sessions(label, now, is_open, recent_open, last_kept):
    through = completed_through(
        now=now,
        asset_class=EQUITY,
        observation=observed(is_open, recent_open),
    )
    # The bound may be a non-trading day; what matters is the bars it keeps.
    available = [d for d in _SESSIONS_2026 if d <= now.date()]
    kept = completed_bars(
        alpaca_daily(available), asset_class=EQUITY, through=through
    )
    assert session_date(kept.index[-1], EQUITY) == last_kept, label
    assert all(session_date(t, EQUITY) < now.date() or not is_open for t in kept.index)


def test_decision_instant_time_zone_does_not_change_the_answer():
    observation = observed(True, ny(2026, 10, 6, 9, 30))
    instant = ny(2026, 10, 6, 10, 0)
    for zone in ("UTC", "Asia/Tokyo", "America/Los_Angeles"):
        assert completed_through(
            now=instant.astimezone(ZoneInfo(zone)),
            asset_class=EQUITY,
            observation=observation,
        ) == date(2026, 10, 5)


def test_without_a_clock_today_is_never_treated_as_complete():
    late_evening = ny(2026, 10, 6, 23, 0)
    assert completed_through(
        now=late_evening, asset_class=EQUITY, observation=None
    ) == date(2026, 10, 5)


def test_crypto_current_utc_day_is_incomplete():
    from trading_assistant.risk.clock import CryptoClock

    now = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
    assert completed_through(
        now=now,
        asset_class=CRYPTO,
        observation=CryptoClock().observe(now),
    ) == date(2026, 10, 5)


# ── filtering bars ────────────────────────────────────────────────────────────
def test_in_progress_and_pre_market_bars_are_dropped():
    frame = alpaca_daily([date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)])
    kept = completed_bars(frame, asset_class=EQUITY, through=date(2026, 10, 5))
    assert [session_date(t, EQUITY) for t in kept.index] == [
        date(2026, 10, 2),
        date(2026, 10, 5),
    ]


def test_duplicate_session_bars_keep_the_last_row():
    frame = alpaca_daily([date(2026, 10, 2), date(2026, 10, 5)])
    duplicate = frame.iloc[[1]].copy()
    duplicate["close"] = 999.0
    frame = pd.concat([frame, duplicate])
    kept = completed_bars(frame, asset_class=EQUITY, through=date(2026, 10, 5))
    assert len(kept) == 2
    assert kept["close"].iloc[-1] == 999.0


def test_unsorted_and_gappy_bars_are_ordered_not_invented():
    frame = alpaca_daily([date(2026, 9, 28), date(2026, 10, 2), date(2026, 9, 29)])
    kept = completed_bars(frame, asset_class=EQUITY, through=date(2026, 10, 5))
    assert list(kept.index) == sorted(kept.index)
    assert len(kept) == 3


def test_empty_or_entirely_future_frames_stay_empty():
    frame = alpaca_daily([date(2026, 10, 6)])
    assert completed_bars(frame, asset_class=EQUITY, through=date(2026, 10, 5)).empty
    assert completed_bars(frame.iloc[0:0], asset_class=EQUITY, through=date(2026, 10, 5)).empty


def test_decision_window_matches_the_backtest_lookback():
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(FEATURE_LOOKBACK + 50)]
    frame = alpaca_daily(days)
    window = decision_bars(frame, asset_class=EQUITY, through=days[-1])
    assert len(window) == FEATURE_LOOKBACK
    assert window.index[-1] == frame.index[-1]


# ── live path ≡ backtest path ─────────────────────────────────────────────────
def _trading_days(end, count):
    days, current = [], end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    return sorted(days)


def _live_features(frame, spy, *, now, observation, tmp_path):
    from trading_assistant.analyst.live_features import build_live_feature_provider

    frames = {"AAPL": frame, "SPY": spy}

    class FakeHistory:
        def get_stock_bars(self, request):
            return SimpleNamespace(df=frames[request.symbol_or_symbols].copy())

    provider = build_live_feature_provider(
        None,
        SimpleNamespace(alpaca_api_key="k", alpaca_secret_key="s"),
        alpaca_client_factory=lambda *_a: FakeHistory(),
        cache_dir=tmp_path,
        market_clock=lambda _ac: SimpleNamespace(observe=lambda _at: observation),
        now=lambda: now,
    )
    return provider("AAPL")


def _backtest_features(frame, spy, t):
    from trading_assistant.backtest.data import DataSource
    from trading_assistant.signals.features import build_features

    view = DataSource({"AAPL": frame, "SPY": spy}).view(t)
    hist = view.history("AAPL", lookback=FEATURE_LOOKBACK)
    spy_hist = view.history("SPY", lookback=FEATURE_LOOKBACK)
    return build_features("AAPL", EQUITY, hist, spy_df=spy_hist, as_of=t)


@pytest.mark.parametrize("sessions", [260, 400])
def test_live_and_backtest_decide_identically_on_identical_bars(tmp_path, sessions):
    """The live path at 10:00 ET on day D+1 must see exactly what the backtest
    sees when deciding at bar D, even though the provider also returns D+1's
    in-progress bar and a longer history."""
    import numpy as np

    from trading_assistant.autopilot import strategy_decision
    from trading_assistant.strategies.rsi_reversion import RsiReversion
    from trading_assistant.strategies.sma_crossover import SmaCrossover
    from trading_assistant.strategies.sma_trend import SmaTrend

    decision_day = date(2026, 11, 3)  # Tuesday after the November DST change
    days = _trading_days(decision_day, sessions)
    rng = np.random.default_rng(sessions)
    walk = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, len(days))))
    frame = alpaca_daily(days)
    frame["close"] = walk
    frame["open"] = walk
    frame["high"] = walk * 1.01
    frame["low"] = walk * 0.99
    spy = alpaca_daily(days, close_start=400.0)

    completed_day = days[-2]
    t = frame.index[-2].to_pydatetime()
    now = ny(2026, 11, 3, 10, 0)
    observation = observed(True, ny(2026, 11, 3, 9, 30))

    live = _live_features(frame, spy, now=now, observation=observation, tmp_path=tmp_path)
    backtest = _backtest_features(frame.iloc[:-1], spy.iloc[:-1], t)

    assert session_date(live.as_of, EQUITY) == completed_day
    assert live.model_dump() == backtest.model_dump()
    for rule in (SmaTrend(), SmaCrossover(), RsiReversion()):
        assert strategy_decision(rule, live) == strategy_decision(rule, backtest)


def test_without_the_policy_the_in_progress_bar_would_leak_in(tmp_path):
    """Guards the test above: the raw frame really does differ."""
    from trading_assistant.signals.features import build_features

    days = _trading_days(date(2026, 11, 3), 260)
    frame = alpaca_daily(days)
    raw = build_features("AAPL", EQUITY, frame)
    live = _live_features(
        frame,
        alpaca_daily(days, close_start=400.0),
        now=ny(2026, 11, 3, 10, 0),
        observation=observed(True, ny(2026, 11, 3, 9, 30)),
        tmp_path=tmp_path,
    )
    assert raw.last_close != live.last_close
