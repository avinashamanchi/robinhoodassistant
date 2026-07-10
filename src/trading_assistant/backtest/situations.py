"""Historical situation generator.

Runs every strategy through named crisis/euphoria episodes and auto-detected
stress windows as isolated mini-backtests, so you can see WHERE each approach
breaks — not just the blended average. Reuses the walk-forward report structures;
each episode becomes a "window".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from ..config import BacktestConfig
from ..strategies.base import Strategy
from ..strategies.buy_and_hold import BuyAndHold
from .data import DataSource
from .engine import run_backtest
from .metrics import compute_metrics
from .report import EvaluationReport, ReportRow

StrategyFactory = Callable[[], Strategy]


def _utc(y, m, d) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Episode:
    name: str
    start: datetime
    end: datetime
    kind: str  # "named" | "auto"


# Curated episodes (equities unless noted). Crypto winter applies to crypto symbols.
NAMED_EPISODES: list[Episode] = [
    Episode("covid_crash_2020", _utc(2020, 2, 19), _utc(2020, 4, 7), "named"),
    Episode("rate_hike_bear_2022", _utc(2022, 1, 1), _utc(2022, 10, 15), "named"),
    Episode("meme_squeeze_2021", _utc(2021, 1, 13), _utc(2021, 2, 5), "named"),
    Episode("crypto_winter_2022", _utc(2022, 4, 1), _utc(2022, 12, 31), "named"),
]


def auto_detect_episodes(
    df: pd.DataFrame,
    window: int = 30,
    drawdown_pct: float = -15.0,
    rally_pct: float = 20.0,
) -> list[Episode]:
    """Rolling 30-bar windows with a >15% drawdown or >20% rally.

    On a hit the scan jumps a full window forward so episodes don't overlap.
    """
    closes = df["close"]
    idx = df.index
    n = len(df)
    episodes: list[Episode] = []
    start = 0
    while start <= n - window:
        w = closes.iloc[start : start + window]
        ret = (w.iloc[-1] / w.iloc[0] - 1) * 100
        run_max = w.cummax()
        dd = float((w / run_max - 1).min() * 100)
        kind = None
        if dd <= drawdown_pct:
            kind = "drawdown"
        elif ret >= rally_pct:
            kind = "rally"
        if kind:
            episodes.append(
                Episode(
                    f"auto_{kind}_{idx[start].date()}",
                    idx[start].to_pydatetime(),
                    idx[start + window - 1].to_pydatetime(),
                    "auto",
                )
            )
            start += window
        else:
            start += 1
    return episodes


def episodes_in_range(df: pd.DataFrame, episodes: list[Episode]) -> list[Episode]:
    """Keep only episodes overlapping the symbol's available data."""
    lo, hi = df.index.min().to_pydatetime(), df.index.max().to_pydatetime()
    return [e for e in episodes if e.end >= lo and e.start <= hi]


def run_situations(
    source: DataSource,
    symbols: list[str],
    strategy_factories: list[StrategyFactory],
    *,
    backtest_config: Optional[BacktestConfig] = None,
    named: Optional[list[Episode]] = None,
    include_auto: bool = True,
    spy_symbol: Optional[str] = None,
    label: str = "historical situations",
) -> EvaluationReport:
    backtest_config = backtest_config or BacktestConfig()
    named = NAMED_EPISODES if named is None else named
    report = EvaluationReport(label=label)

    for symbol in symbols:
        df = source.full(symbol)
        episodes = episodes_in_range(df, named)
        if include_auto:
            episodes = episodes + auto_detect_episodes(df)

        for ep in episodes:
            bench = compute_metrics(
                run_backtest(
                    BuyAndHold(), source, symbol, backtest_config=backtest_config,
                    spy_symbol=spy_symbol, start=ep.start, end=ep.end,
                )
            )
            for factory in strategy_factories:
                metrics = compute_metrics(
                    run_backtest(
                        factory(), source, symbol, backtest_config=backtest_config,
                        spy_symbol=spy_symbol, start=ep.start, end=ep.end,
                    )
                )
                report.rows.append(
                    ReportRow(
                        symbol=symbol,
                        strategy=factory().name,
                        window=ep.name,
                        metrics=metrics,
                        benchmark=bench,
                    )
                )
    return report
