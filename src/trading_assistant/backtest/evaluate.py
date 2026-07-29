"""Walk-forward evaluation with a sacred holdout.

History splits into a development window (where tuning would happen) and a final
holdout (the most recent N months, evaluated once, never tuned on). Every
strategy is reported against buy-and-hold on the same symbol and window, so the
benchmark is always in view. Results persist to the DB.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from ..config import BacktestConfig
from ..db.models import (
    AuditEvent,
    BacktestArtifact,
    BacktestMetricRow,
    BacktestRun,
    HoldoutAccessLog,
)
from ..strategies.base import Strategy
from ..security.sensitive_fields import persist_sensitive
from ..strategies.buy_and_hold import BuyAndHold
from .data import DataSource
from .engine import BacktestResult, run_backtest
from .holdout import HoldoutGuard
from .metrics import Metrics, compute_metrics
from .report import (
    BACKTEST_ARTIFACT_SCHEMA_VERSION,
    EvaluationReport,
    ReportRow,
    canonical_metric_rows_digest,
)

StrategyFactory = Callable[[], Strategy]


@dataclass(frozen=True)
class BacktestArtifactContext:
    """Run-level evidence required to persist truthful replay artifacts."""

    data_source: str
    requested_start: datetime | None
    requested_end: datetime | None
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    backtest_config: BacktestConfig
    symbols: tuple[str, ...]
    strategies: tuple[str, ...]


def _finite(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _utc_iso(value: datetime | None, *, name: str) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _json_payload(value: dict) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _equity_points(
    points: list[tuple[datetime, float]],
    *,
    name: str,
) -> list[dict]:
    return [
        {
            "at": _utc_iso(timestamp, name=f"{name}.at"),
            "equity": _finite(equity, name=f"{name}.equity"),
        }
        for timestamp, equity in points
    ]


def _run_window(
    factory: StrategyFactory,
    source: DataSource,
    symbol: str,
    bounds: tuple[datetime, datetime],
    backtest_config: BacktestConfig,
    spy_symbol: Optional[str],
    cancel_check: Callable[[], None] | None,
) -> tuple[Metrics, BacktestResult]:
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
    return compute_metrics(result), result


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
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[EvaluationReport, HoldoutGuard]:
    backtest_config = backtest_config or BacktestConfig()
    if cancel_check is not None:
        cancel_check()
    timeline = source.timeline(symbols)
    if start is not None:
        timeline = [timestamp for timestamp in timeline if timestamp >= start]
    if end is not None:
        timeline = [timestamp for timestamp in timeline if timestamp <= end]
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
            benchmark, benchmark_result = _run_window(
                BuyAndHold,
                source,
                symbol,
                bounds,
                backtest_config,
                spy_symbol,
                cancel_check,
            )
            for factory in strategy_factories:
                metrics, result = _run_window(
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
                        equity_curve=list(result.equity_curve),
                        benchmark_equity_curve=list(
                            benchmark_result.equity_curve
                        ),
                        window_start=bounds[0],
                        window_end=bounds[1],
                        total_fees=sum(
                            float(fill.fee) for fill in result.fills
                        ),
                        benchmark_total_fees=sum(
                            float(fill.fee)
                            for fill in benchmark_result.fills
                        ),
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
    artifact_context: BacktestArtifactContext | None = None,
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
        if artifact_context is not None:
            _persist_artifacts(
                s,
                run,
                report,
                guard,
                artifact_context,
            )
        for access in guard.access_log:
            s.add(
                HoldoutAccessLog(
                    at=access.at, context=access.context, blocked=access.blocked
                )
            )
        persist_sensitive(
            s,
            AuditEvent(
                actor=actor,
                action="backtest.run",
                target_type="backtest_run",
                target_id=str(run.id),
                request_id=request_id,
                result_code="succeeded",
            ),
            {
                "reason": reason,
                "detail_json": json.dumps(
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
            },
        )
        s.commit()
        return run.id


def _persist_artifacts(
    session,
    run: BacktestRun,
    report: EvaluationReport,
    guard: HoldoutGuard,
    context: BacktestArtifactContext,
) -> None:
    if context.data_source != "synthetic":
        raise ValueError("unsupported backtest artifact data source")
    if context.completed_at < context.started_at:
        raise ValueError("backtest completion precedes start")
    if (
        context.requested_start is not None
        and context.requested_end is not None
        and context.requested_end < context.requested_start
    ):
        raise ValueError("requested backtest range is invalid")
    duration = _finite(
        context.duration_seconds,
        name="duration_seconds",
    )
    if duration < 0:
        raise ValueError("duration_seconds must not be negative")

    all_points = [
        point
        for row in report.rows
        for curve in (row.equity_curve, row.benchmark_equity_curve)
        for point in curve
    ]
    if not all_points:
        raise ValueError("successful backtest has no equity evidence")
    actual_start = min(timestamp for timestamp, _ in all_points)
    actual_end = max(timestamp for timestamp, _ in all_points)
    metric_rows = [row.to_dict() for row in report.rows]
    manifest = {
        "schema_version": BACKTEST_ARTIFACT_SCHEMA_VERSION,
        "data_source": context.data_source,
        "requested_range": {
            "start": _utc_iso(
                context.requested_start,
                name="requested_start",
            ),
            "end": _utc_iso(
                context.requested_end,
                name="requested_end",
            ),
        },
        "actual_range": {
            "start": _utc_iso(actual_start, name="actual_start"),
            "end": _utc_iso(actual_end, name="actual_end"),
        },
        "started_at": _utc_iso(context.started_at, name="started_at"),
        "completed_at": _utc_iso(
            context.completed_at,
            name="completed_at",
        ),
        "duration_seconds": duration,
        "backtest_config": context.backtest_config.model_dump(mode="json"),
        "symbols": list(context.symbols),
        "strategies": list(context.strategies),
        "metric_rows_sha256": canonical_metric_rows_digest(
            metric_rows
        ),
        "holdout_start": _utc_iso(
            report.holdout_start,
            name="holdout_start",
        ),
        "holdout_access_log": [
            {
                "at": _utc_iso(access.at, name="holdout_access.at"),
                "context": access.context,
                "blocked": bool(access.blocked),
            }
            for access in guard.access_log
        ],
        "validation": {
            "status": "unavailable",
            "reason": "not_run",
        },
        "episodes": {"status": "not_run"},
    }
    _add_artifact(session, run.id, "manifest", manifest)

    cost_assumptions = {
        "fills": context.backtest_config.fills.model_dump(mode="json"),
        "slippage_bps": dict(context.backtest_config.slippage_bps),
        "fees_bps": dict(context.backtest_config.fees_bps),
    }
    for index, row in enumerate(report.rows):
        if row.window_start is None or row.window_end is None:
            raise ValueError("backtest row is missing window bounds")
        if row.window_end < row.window_start:
            raise ValueError("backtest row window is invalid")
        strategy_curve = _equity_points(
            row.equity_curve,
            name="strategy_equity",
        )
        benchmark_curve = _equity_points(
            row.benchmark_equity_curve,
            name="benchmark_equity",
        )
        if not strategy_curve or not benchmark_curve:
            raise ValueError("backtest row is missing equity evidence")
        payload = {
            "schema_version": BACKTEST_ARTIFACT_SCHEMA_VERSION,
            "row_index": index,
            "symbol": row.symbol,
            "strategy": row.strategy,
            "window": row.window,
            "window_bounds": {
                "start": _utc_iso(
                    row.window_start,
                    name="window_start",
                ),
                "end": _utc_iso(row.window_end, name="window_end"),
            },
            "strategy_equity": strategy_curve,
            "benchmark_equity": benchmark_curve,
            "actual_total_fees": _nonnegative_finite(
                row.total_fees, name="actual_total_fees"
            ),
            "benchmark_actual_total_fees": _nonnegative_finite(
                row.benchmark_total_fees,
                name="benchmark_actual_total_fees",
            ),
            "cost_assumptions": cost_assumptions,
        }
        _add_artifact(
            session,
            run.id,
            f"series:{index:06d}",
            payload,
        )


def _add_artifact(
    session,
    run_id: int,
    artifact_key: str,
    payload: dict,
) -> None:
    encoded = _json_payload(payload)
    persist_sensitive(
        session,
        BacktestArtifact(
            run_id=run_id,
            artifact_key=artifact_key,
            schema_version=BACKTEST_ARTIFACT_SCHEMA_VERSION,
        ),
        {"payload_json": encoded},
    )


def _nonnegative_finite(value: float, *, name: str) -> float:
    number = _finite(value, name=name)
    if number < 0:
        raise ValueError(f"{name} must not be negative")
    return number
