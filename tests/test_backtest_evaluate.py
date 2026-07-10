"""Metrics, the sacred holdout guard, and walk-forward evaluation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from trading_assistant.backtest.data import DataSource
from trading_assistant.backtest.engine import BacktestResult
from trading_assistant.backtest.evaluate import persist_report, walk_forward
from trading_assistant.backtest.holdout import HoldoutGuard, HoldoutViolation
from trading_assistant.backtest.metrics import compute_metrics
from trading_assistant.backtest.report import SIMULATED_LABEL
from trading_assistant.backtest.sim_broker import SimFill
from trading_assistant.backtest.synthetic import make_bars
from trading_assistant.db.models import BacktestMetricRow, BacktestRun, HoldoutAccessLog
from trading_assistant.signals.models import Regime
from trading_assistant.strategies.breakout import Breakout
from trading_assistant.strategies.rsi_reversion import RsiReversion
from trading_assistant.strategies.sma_crossover import SmaCrossover


def _ts(i):
    return datetime(2020, 1, 1, tzinfo=timezone.utc).replace(day=i)


# ── metrics ─────────────────────────────────────────────────────
def test_metrics_on_known_curve():
    res = BacktestResult(symbol="AAPL", strategy="x", starting_equity=100.0)
    res.equity_curve = [(_ts(1), 100.0), (_ts(2), 110.0), (_ts(3), 99.0)]
    res.invested = [True, True, True]
    res.regimes = [None, Regime.TRENDING_UP, Regime.RANGING]
    res.fills = [
        SimFill("AAPL", "buy", 1, 100.0, 0.0, _ts(1)),
        SimFill("AAPL", "sell", 1, 110.0, 0.0, _ts(2)),
    ]
    m = compute_metrics(res)
    assert m.total_return_pct == -1.0
    assert m.max_drawdown_pct == pytest.approx(-10.0, abs=0.01)
    assert m.num_trades == 1
    assert m.win_rate_pct == 100.0
    assert m.exposure_pct == 100.0
    # +10 in the trending_up bar, -11 in the ranging bar.
    assert m.pnl_by_regime["trending_up"] == 10.0
    assert m.pnl_by_regime["ranging"] == -11.0


def test_metrics_empty_is_safe():
    res = BacktestResult(symbol="AAPL", strategy="x", starting_equity=100.0)
    res.equity_curve = [(_ts(1), 100.0)]
    m = compute_metrics(res)
    assert m.num_trades == 0 and m.sharpe == 0.0


# ── holdout guard (guardrail #1) ────────────────────────────────
def test_holdout_split_and_sweep_refused():
    source = DataSource({"AAPL": make_bars(500, seed=1)})
    timeline = source.timeline(["AAPL"])
    guard = HoldoutGuard(timeline, holdout_months=12)

    dev, hold = guard.split(timeline)
    assert dev and hold
    assert all(t < guard.holdout_start for t in dev)
    assert all(t >= guard.holdout_start for t in hold)

    with pytest.raises(HoldoutViolation):
        guard.forbid_sweep("holdout")           # sweeping holdout is forbidden
    assert any(a.blocked for a in guard.access_log)
    guard.forbid_sweep("development")           # allowed, no raise


# ── walk-forward ────────────────────────────────────────────────
def test_walk_forward_reports_vs_buy_and_hold(session_factory):
    source = DataSource({"AAPL": make_bars(550, seed=3)})
    report, guard = walk_forward(
        source, ["AAPL"], [SmaCrossover, RsiReversion, Breakout], holdout_months=12
    )
    assert report.disclaimer == SIMULATED_LABEL
    windows = {r.window for r in report.rows}
    assert "development" in windows and "holdout" in windows
    # Every row carries its buy-and-hold benchmark side by side.
    assert all(r.benchmark is not None for r in report.rows)
    # The holdout was accessed (once) and that is on record.
    assert any("holdout" in a.context for a in guard.access_log)
    # Rendered table starts with the mandatory disclaimer.
    assert report.render_table().startswith(SIMULATED_LABEL)


def test_persist_report_writes_rows_and_audit(session_factory):
    source = DataSource({"AAPL": make_bars(550, seed=4)})
    report, guard = walk_forward(source, ["AAPL"], [SmaCrossover], holdout_months=12)
    run_id = persist_report(session_factory, report, guard)

    with session_factory() as s:
        assert s.get(BacktestRun, run_id) is not None
        rows = s.execute(
            select(BacktestMetricRow).where(BacktestMetricRow.run_id == run_id)
        ).scalars().all()
        assert len(rows) == len(report.rows)
        access = s.execute(select(HoldoutAccessLog)).scalars().all()
        assert len(access) >= 1
