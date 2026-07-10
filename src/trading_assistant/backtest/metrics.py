"""Performance metrics for a backtest result.

All metrics are computed from the equity curve + fills of a BacktestResult.
Return-based metrics annualize with 252 trading periods (daily bars). P&L
attribution by regime uses the per-bar regime recorded during the run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

import numpy as np

from ..risk.pnl import FillLike, realized_events
from .engine import BacktestResult

PERIODS_PER_YEAR = 252


@dataclass
class Metrics:
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    exposure_pct: float = 0.0
    turnover: float = 0.0
    num_trades: int = 0
    pnl_by_regime: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _returns(equity: np.ndarray) -> np.ndarray:
    if len(equity) < 2:
        return np.array([])
    return equity[1:] / equity[:-1] - 1


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    running_max = np.maximum.accumulate(equity)
    drawdowns = equity / running_max - 1
    return float(drawdowns.min() * 100)


def _sharpe(rets: np.ndarray) -> float:
    if len(rets) < 2 or rets.std(ddof=1) == 0:
        return 0.0
    return float(rets.mean() / rets.std(ddof=1) * np.sqrt(PERIODS_PER_YEAR))


def _sortino(rets: np.ndarray) -> float:
    if len(rets) < 2:
        return 0.0
    downside = rets[rets < 0]
    dd = downside.std(ddof=1) if len(downside) > 1 else 0.0
    if dd == 0:
        return 0.0
    return float(rets.mean() / dd * np.sqrt(PERIODS_PER_YEAR))


def _trade_stats(result: BacktestResult) -> tuple[float, float, float, float, int]:
    fills = [
        FillLike(f.symbol, f.side, Decimal(str(f.qty)), Decimal(str(f.price)), f.ts)
        for f in result.fills
    ]
    pnls = [float(p) for _, p in realized_events(fills)]
    if not pnls:
        return 0.0, 0.0, 0.0, 0.0, 0
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls) * 100
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss else float("inf")
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return win_rate, profit_factor, avg_win, avg_loss, len(pnls)


def _pnl_by_regime(result: BacktestResult) -> dict:
    """Attribute each bar's equity change to the regime prevailing at that bar."""
    out: dict[str, float] = {}
    curve = result.equity_curve
    for i in range(1, len(curve)):
        delta = curve[i][1] - curve[i - 1][1]
        regime = result.regimes[i] if i < len(result.regimes) else None
        key = regime.value if regime is not None else "unknown"
        out[key] = out.get(key, 0.0) + delta
    return {k: round(v, 2) for k, v in out.items()}


def compute_metrics(result: BacktestResult) -> Metrics:
    equity = np.array([e for _, e in result.equity_curve], dtype=float)
    rets = _returns(equity)
    n = len(equity)

    total_return = result.total_return_pct
    cagr = 0.0
    if n > 1 and equity[0] > 0 and equity[-1] > 0:
        years = n / PERIODS_PER_YEAR
        if years > 0:
            cagr = (float(equity[-1] / equity[0]) ** (1 / years) - 1) * 100

    win_rate, profit_factor, avg_win, avg_loss, num_trades = _trade_stats(result)

    exposure = (
        sum(1 for x in result.invested if x) / len(result.invested) * 100
        if result.invested
        else 0.0
    )
    traded_notional = sum(f.qty * f.price for f in result.fills)
    avg_equity = float(equity.mean()) if n else 1.0
    years = max(n / PERIODS_PER_YEAR, 1e-9)
    turnover = (traded_notional / avg_equity / years) if avg_equity else 0.0

    return Metrics(
        total_return_pct=round(total_return, 2),
        cagr_pct=round(cagr, 2),
        sharpe=round(_sharpe(rets), 2),
        sortino=round(_sortino(rets), 2),
        max_drawdown_pct=round(_max_drawdown(equity), 2),
        win_rate_pct=round(win_rate, 2),
        profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else profit_factor,
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        exposure_pct=round(exposure, 2),
        turnover=round(turnover, 2),
        num_trades=num_trades,
        pnl_by_regime=_pnl_by_regime(result),
    )
