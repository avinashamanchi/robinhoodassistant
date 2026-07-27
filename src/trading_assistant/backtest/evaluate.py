"""Walk-forward evaluation with a sacred holdout.

History splits into a development window (where tuning would happen) and a final
holdout (the most recent N months, evaluated once, never tuned on). Every
strategy is reported against buy-and-hold on the same symbol and window, so the
benchmark is always in view. Results persist to the DB.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Callable, Optional

from ..config import BacktestConfig
from ..db.models import (
    AuditEvent,
    BacktestMetricRow,
    BacktestRun,
    HoldoutAccessLog,
)
from ..strategies.base import Strategy
from ..strategies.buy_and_hold import BuyAndHold
from .data import DataSource
from .engine import run_backtest
from .holdout import HoldoutGuard
from .metrics import Metrics, compute_metrics
from .report import EvaluationReport, ReportRow

StrategyFactory = Callable[[], Strategy]


def _run_window(
    factory: StrategyFactory,
    source: DataSource,
    symbol: str,
    bounds: tuple[datetime, datetime],
    backtest_config: BacktestConfig,
    spy_symbol: Optional[str],
    cancel_check: Callable[[], None] | None,
) -> Metrics:
    if cancel_check is not None:
        cancel_check()
    result = run_backtest(
        factory(),
        source,
        symbol,
        backtest_config=backtest_config,
        spy_symbol=spy_symbol,
        start=bounds[0],
        end=bounds[1],
        cancel_check=cancel_check,
    )
    return compute_metrics(result)


def walk_forward(
    source: DataSource,
    symbols: list[str],
    strategy_factories: list[StrategyFactory],
    *,
    backtest_config: Optional[BacktestConfig] = None,
    holdout_months: int = 12,
    spy_symbol: Optional[str] = None,
    label: str = "baseline walk-forward",
    cancel_check: Callable[[], None] | None = None,
) -> tuple[EvaluationReport, HoldoutGuard]:
    backtest_config = backtest_config or BacktestConfig()
    if cancel_check is not None:
        cancel_check()
    timeline = source.timeline(symbols)
    guard = HoldoutGuard(timeline, holdout_months)
    dev, hold = guard.split(timeline)

    windows: dict[str, tuple[datetime, datetime]] = {}
    if dev:
        windows["development"] = (dev[0], dev[-1])
    if hold:
        windows["holdout"] = (hold[0], hold[-1])

    report = EvaluationReport(holdout_start=guard.holdout_start, label=label)
    for symbol in symbols:
        for window, bounds in windows.items():
            if cancel_check is not None:
                cancel_check()
            if window == "holdout":
                guard.evaluate_holdout(symbol)  # one-shot, logged
            benchmark = _run_window(
                BuyAndHold,
                source,
                symbol,
                bounds,
                backtest_config,
                spy_symbol,
                cancel_check,
            )
            for factory in strategy_factories:
                metrics = _run_window(
                    factory,
                    source,
                    symbol,
                    bounds,
                    backtest_config,
                    spy_symbol,
                    cancel_check,
                )
                if cancel_check is not None:
                    cancel_check()
                report.rows.append(
                    ReportRow(
                        symbol=symbol,
                        strategy=factory().name,
                        window=window,
                        metrics=metrics,
                        benchmark=benchmark,
                    )
                )
    return report, guard


def persist_report(
    session_factory,
    report: EvaluationReport,
    guard: HoldoutGuard,
    *,
    actor: str,
    reason: str,
    request_id: str,
) -> int:
    """Write the report + holdout-access audit to the DB. Returns the run id."""
    actor = actor.strip()
    reason = reason.strip()
    request_id = request_id.strip()
    if not actor or not reason or not request_id:
        raise ValueError(
            "backtest persistence actor, reason, and request_id "
            "must be non-empty"
        )
    with session_factory() as s:
        run = BacktestRun(
            label=report.label,
            holdout_start=report.holdout_start,
            config_json=json.dumps(
                {
                    "disclaimer": report.disclaimer,
                    "status": "succeeded",
                },
                sort_keys=True,
            ),
        )
        s.add(run)
        s.flush()
        for row in report.rows:
            s.add(
                BacktestMetricRow(
                    run_id=run.id,
                    symbol=row.symbol,
                    strategy=row.strategy,
                    window=row.window,
                    metrics_json=json.dumps(row.to_dict()),
                )
            )
        for access in guard.access_log:
            s.add(
                HoldoutAccessLog(
                    at=access.at, context=access.context, blocked=access.blocked
                )
            )
        s.add(
            AuditEvent(
                actor=actor,
                action="backtest.run",
                target_type="backtest_run",
                target_id=str(run.id),
                request_id=request_id,
                reason=reason,
                result_code="succeeded",
                detail_json=json.dumps(
                    {
                        "holdout_start": (
                            report.holdout_start.isoformat()
                            if report.holdout_start
                            else None
                        ),
                        "row_count": len(report.rows),
                    },
                    sort_keys=True,
                ),
            )
        )
        s.commit()
        return run.id
