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
    """Run the live provider with caches captured at the decision instant.

    The cache file time is the frame's fetch provenance, so it must agree
    with the simulated ``now``: a frame fetched today cannot hold bars from
    a later simulated date.
    """
    import os

    from trading_assistant.analyst.live_features import build_live_feature_provider
    from trading_assistant.backtest import data as backtest_data

    frames = {"AAPL": frame, "SPY": spy}
    for symbol, data in frames.items():
        path = backtest_data.cache_path(tmp_path, symbol, "1Day")
        backtest_data.write_parquet_atomic(data, path)
        os.utime(path, (now.timestamp(), now.timestamp()))

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


# ── bar finality: a mid-session capture is never a final bar ──────────────────
from trading_assistant.signals.sessions import (  # noqa: E402
    decision_cutoff,
    final_instant,
    final_through,
)


@pytest.mark.parametrize(
    ("fetched", "expected"),
    [
        (ny(2026, 9, 29, 15, 0), date(2026, 9, 28)),   # mid-session
        (ny(2026, 9, 29, 16, 30), date(2026, 9, 28)),  # closed, not yet final
        (ny(2026, 9, 29, 20, 0), date(2026, 9, 29)),   # final
        (ny(2026, 11, 2, 19, 59), date(2026, 11, 1)),  # EST, just before
        (ny(2026, 3, 9, 20, 1), date(2026, 3, 9)),     # EDT, just after
    ],
)
def test_equity_bar_finality_follows_new_york_time(fetched, expected):
    assert final_through(fetched.astimezone(timezone.utc), EQUITY) == expected
    assert final_through(fetched.astimezone(ZoneInfo("Asia/Tokyo")), EQUITY) == expected


def test_final_instant_and_final_through_agree():
    for session in (date(2026, 3, 9), date(2026, 7, 10), date(2026, 11, 2)):
        for ac in (EQUITY, CRYPTO):
            instant = final_instant(session, ac)
            assert final_through(instant, ac) == session
            assert final_through(instant - timedelta(seconds=1), ac) < session


def test_crypto_candle_is_final_an_hour_after_the_utc_day():
    day = date(2026, 10, 6)
    assert final_through(datetime(2026, 10, 7, 0, 30, tzinfo=timezone.utc), CRYPTO) == date(2026, 10, 5)
    assert final_through(datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc), CRYPTO) == day


def test_closed_market_does_not_make_an_unfinal_bar_usable():
    observation = observed(False, ny(2026, 9, 29, 9, 30))
    assert decision_cutoff(
        now=ny(2026, 9, 29, 16, 30), asset_class=EQUITY, observation=observation
    ) == date(2026, 9, 28)
    assert decision_cutoff(
        now=ny(2026, 9, 29, 20, 30), asset_class=EQUITY, observation=observation
    ) == date(2026, 9, 29)


def _provider_on_cache(tmp_path, *, now, observation, cached, cached_at, served):
    """A live provider over a pre-populated cache and a recording fake SDK."""
    import os

    from trading_assistant.analyst.live_features import build_live_feature_provider
    from trading_assistant.backtest import data as backtest_data

    for symbol in ("AAPL", "SPY"):
        path = backtest_data.cache_path(tmp_path, symbol, "1Day")
        backtest_data.write_parquet_atomic(cached, path)
        os.utime(path, (cached_at.timestamp(), cached_at.timestamp()))
    calls = []

    class FakeHistory:
        def get_stock_bars(self, request):
            calls.append(request.symbol_or_symbols)
            return SimpleNamespace(df=served.copy())

    provider = build_live_feature_provider(
        None,
        SimpleNamespace(alpaca_api_key="k", alpaca_secret_key="s"),
        alpaca_client_factory=lambda *_a: FakeHistory(),
        cache_dir=tmp_path,
        market_clock=lambda _ac: SimpleNamespace(observe=lambda _at: observation),
        now=lambda: now,
        require_market_clock=True,
    )
    return provider, calls


def test_a_forming_bar_never_supplies_the_close(tmp_path):
    """At 16:30 the market is closed, but today's bar is not final until
    20:00 ET. Whether it comes from a 15:00 cache or a fresh fetch that
    still returns the forming bar, it must not drive a decision."""
    days = _trading_days(date(2026, 9, 29), 260)
    forming = alpaca_daily(days)
    forming.loc[forming.index[-1], "close"] = 999.0       # not final
    closed = observed(False, ny(2026, 9, 29, 9, 30))

    provider, _calls = _provider_on_cache(
        tmp_path,
        now=ny(2026, 9, 29, 16, 30),
        observation=closed,
        cached=forming,
        cached_at=ny(2026, 9, 29, 15, 0),
        served=forming,
    )
    features = provider("AAPL")
    assert session_date(features.as_of, EQUITY) == date(2026, 9, 28)
    assert features.last_close != 999.0


def test_after_the_bar_is_final_a_stale_capture_is_refreshed(tmp_path):
    days = _trading_days(date(2026, 9, 29), 260)
    partial = alpaca_daily(days)
    partial.loc[partial.index[-1], "close"] = 999.0
    final = alpaca_daily(days)
    final.loc[final.index[-1], "close"] = 123.0

    provider, calls = _provider_on_cache(
        tmp_path,
        now=ny(2026, 9, 29, 20, 30),
        observation=observed(False, ny(2026, 9, 29, 9, 30)),
        cached=partial,
        cached_at=ny(2026, 9, 29, 15, 0),
        served=final,
    )
    features = provider("AAPL")
    assert sorted(calls) == ["AAPL", "SPY"]
    assert session_date(features.as_of, EQUITY) == date(2026, 9, 29)
    assert features.last_close == 123.0


def test_order_paths_require_a_market_clock():
    from trading_assistant.analyst.live_features import build_live_feature_provider
    from trading_assistant.dependencies import RequiredDependencyUnavailable

    with pytest.raises(RequiredDependencyUnavailable):
        build_live_feature_provider(None, SimpleNamespace(), require_market_clock=True)


def test_an_unreadable_clock_fails_closed(tmp_path):
    from trading_assistant.analyst.live_features import build_live_feature_provider
    from trading_assistant.dependencies import RequiredDependencyUnavailable

    def broken(_at):
        raise TimeoutError("clock unavailable")

    provider = build_live_feature_provider(
        None,
        SimpleNamespace(alpaca_api_key="k", alpaca_secret_key="s"),
        cache_dir=tmp_path,
        market_clock=lambda _ac: SimpleNamespace(observe=broken),
    )
    with pytest.raises(RequiredDependencyUnavailable):
        provider("AAPL")


@pytest.mark.parametrize(
    ("first", "first_state", "later", "expected_both"),
    [
        # Cached "open" observation lingers 50s past the 16:00 close.
        (ny(2026, 9, 29, 15, 59),
         (True, ny(2026, 9, 29, 9, 30)), ny(2026, 9, 29, 16, 0), date(2026, 9, 28)),
        # Cached "closed" observation lingers past the 09:30 open.
        (ny(2026, 9, 29, 9, 29), (False, ny(2026, 9, 28, 9, 30)), ny(2026, 9, 29, 9, 30),
         date(2026, 9, 28)),
    ],
)
def test_clock_memo_is_only_ever_conservative(first, first_state, later, expected_both):
    from trading_assistant.analyst.live_features import _session_cutoff

    instants = iter([first, later])
    ticks = iter([0.0, 0.0, 50.0])
    observations = []

    def observe(at):
        observations.append(at)
        return observed(*first_state)

    cutoff = _session_cutoff(
        market_clock=lambda _ac: SimpleNamespace(observe=observe),
        now=lambda: next(instants),
        monotonic=lambda: next(ticks),
    )
    assert cutoff(EQUITY) == expected_both
    assert cutoff(EQUITY) == expected_both  # memo hit 50s later
    assert len(observations) == 1
