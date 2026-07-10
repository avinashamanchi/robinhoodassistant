"""Historical situation generator: auto-detection + per-episode mini-backtests."""

from __future__ import annotations

import numpy as np

from trading_assistant.backtest.data import DataSource
from trading_assistant.backtest.situations import (
    auto_detect_episodes,
    run_situations,
)
from trading_assistant.backtest.synthetic import make_bars, ohlcv_from_closes
from trading_assistant.strategies.rsi_reversion import RsiReversion
from trading_assistant.strategies.sma_crossover import SmaCrossover


def test_auto_detect_finds_a_drawdown():
    # Flat, then a ~22% decline over 30 bars, then flat again.
    closes = (
        [100.0] * 60
        + list(np.linspace(100.0, 78.0, 30))
        + [78.0] * 60
    )
    df = ohlcv_from_closes(closes)
    episodes = auto_detect_episodes(df, window=30, drawdown_pct=-15.0)
    assert any(e.kind == "auto" and "drawdown" in e.name for e in episodes)


def test_run_situations_reports_per_episode():
    source = DataSource({"AAPL": make_bars(400, drift=-0.001, vol=0.02, seed=5)})
    report = run_situations(
        source,
        ["AAPL"],
        [SmaCrossover, RsiReversion],
        named=[],            # named episodes are 2020-2022; synthetic starts 2015
        include_auto=True,
    )
    # At least one auto episode produced rows, each with a buy-and-hold benchmark.
    assert report.rows
    assert all(r.benchmark is not None for r in report.rows)
    assert all(r.window.startswith("auto_") for r in report.rows)
