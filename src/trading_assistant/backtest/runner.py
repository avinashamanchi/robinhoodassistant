"""One-call synthetic backtest runner used by the /backtests API.

CI and keyless environments can run a full walk-forward on deterministic synthetic
data. Real runs would build the DataSource from ``download_alpaca_bars`` instead;
everything downstream (engine, metrics, holdout, persistence) is identical.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timezone
from typing import Callable, Optional

from sqlalchemy import select, text

from ..config import BacktestConfig
from ..db.models import AuditEvent, BacktestRun
from ..security.sensitive_fields import persist_sensitive, sensitive_store
from ..strategies.breakout import Breakout
from ..strategies.rsi_reversion import RsiReversion
from ..strategies.sma_crossover import SmaCrossover
from .data import DataSource
from .evaluate import persist_report, walk_forward
from .report import EvaluationReport
from .synthetic import make_bars

# Deterministic per-symbol synthetic character (name -> drift, vol).
_PROFILES = {
    "TREND": (0.0009, 0.011),
    "CHOP": (0.0000, 0.016),
    "BEARY": (-0.0006, 0.020),
    "SPY": (0.0004, 0.010),
}
DEFAULT_SYMBOLS = ["TREND", "CHOP", "BEARY"]
STRATEGIES = [SmaCrossover, RsiReversion, Breakout]
MAX_CALENDAR_DAYS = 3_000


class BacktestTimedOut(RuntimeError):
    """Cooperative replay termination after the configured runtime ceiling."""

    def __init__(self, *, run_id: int | None = None) -> None:
        super().__init__("backtest runtime deadline exceeded")
        self.run_id = run_id


@dataclass
class _BacktestControl:
    deadline: float
    stop_event: threading.Event
    monotonic: Callable[[], float]

    def check(self) -> None:
        if (
            self.stop_event.is_set()
            or self.monotonic() >= self.deadline
        ):
            self.stop_event.set()
            raise BacktestTimedOut()


def _seed(name: str) -> int:
    return abs(hash(name)) % 100_000


def _requested_date_window(
    start_date: date | None,
    end_date: date | None,
) -> tuple[datetime, datetime, int] | None:
    if (start_date is None) != (end_date is None):
        raise ValueError(
            "backtest start_date and end_date must be provided together"
        )
    if start_date is None or end_date is None:
        return None
    if end_date < start_date:
        raise ValueError("backtest end_date must not precede start_date")
    inclusive_days = (end_date - start_date).days + 1
    if inclusive_days > MAX_CALENDAR_DAYS:
        raise ValueError(
            "backtest calendar range exceeds 3000 inclusive days"
        )
    return (
        datetime.combine(
            start_date,
            datetime_time.min,
            tzinfo=timezone.utc,
        ),
        datetime.combine(
            end_date,
            datetime_time.max,
            tzinfo=timezone.utc,
        ),
        inclusive_days,
    )


def build_synthetic_source(
    symbols: list[str],
    bars: int = 650,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> DataSource:
    requested_window = _requested_date_window(start_date, end_date)
    generated_bars = (
        requested_window[2]
        if requested_window is not None
        else bars
    )
    generated_start = (
        requested_window[0]
        if requested_window is not None
        else None
    )
    frames = {}
    for sym in set(symbols) | {"SPY"}:
        if cancel_check is not None:
            cancel_check()
        drift, vol = _PROFILES.get(sym, (0.0003, 0.014))
        frames[sym] = make_bars(
            generated_bars,
            drift=drift,
            vol=vol,
            seed=_seed(sym),
            start_ts=generated_start,
        )
    if cancel_check is not None:
        cancel_check()
    return DataSource(frames)


class BacktestRunner:
    """Synchronous cooperative runner; the owning request joins it by return."""

    def __init__(
        self,
        session_factory,
        *,
        runtime_seconds: int = 1_200,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if runtime_seconds <= 0 or runtime_seconds > 1_200:
            raise ValueError(
                "backtest runtime_seconds must be between 1 and 1200"
            )
        self.session_factory = session_factory
        self.runtime_seconds = runtime_seconds
        self.monotonic = monotonic

    def run(
        self,
        symbols: Optional[list[str]] = None,
        *,
        actor: str,
        reason: str,
        request_id: str,
        bars: int = 650,
        holdout_months: int = 12,
        label: str = "synthetic walk-forward",
        deadline: float | None = None,
        stop_event: threading.Event | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[int, EvaluationReport]:
        actor = actor.strip()
        reason = reason.strip()
        request_id = request_id.strip()
        if not actor or not reason or not request_id:
            raise ValueError(
                "backtest actor, reason, and request_id must be non-empty"
            )
        requested_window = _requested_date_window(
            start_date,
            end_date,
        )
        replay_start = (
            requested_window[0]
            if requested_window is not None
            else None
        )
        replay_end = (
            requested_window[1]
            if requested_window is not None
            else None
        )
        selected_symbols = symbols or DEFAULT_SYMBOLS
        event = stop_event or threading.Event()
        control = _BacktestControl(
            deadline=(
                deadline
                if deadline is not None
                else self.monotonic() + self.runtime_seconds
            ),
            stop_event=event,
            monotonic=self.monotonic,
        )
        persisted_run_id: int | None = None
        timeout_stage = "replay"
        try:
            control.check()
            source = build_synthetic_source(
                selected_symbols,
                bars,
                start_date=start_date,
                end_date=end_date,
                cancel_check=control.check,
            )
            control.check()
            report, guard = walk_forward(
                source,
                selected_symbols,
                STRATEGIES,
                backtest_config=BacktestConfig(),
                holdout_months=holdout_months,
                spy_symbol="SPY",
                label=label,
                cancel_check=control.check,
                start=replay_start,
                end=replay_end,
            )
            control.check()
            persisted_run_id = persist_report(
                self.session_factory,
                report,
                guard,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
            timeout_stage = "post_persistence"
            control.check()
            return persisted_run_id, report
        except BacktestTimedOut:
            event.set()
            run_id = self._persist_timeout(
                symbols=selected_symbols,
                actor=actor,
                reason=reason,
                request_id=request_id,
                label=label,
                persisted_run_id=persisted_run_id,
                stage=timeout_stage,
            )
            raise BacktestTimedOut(run_id=run_id) from None
        except Exception:
            self._persist_failure(
                symbols=selected_symbols,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
            raise

    def _persist_timeout(
        self,
        *,
        symbols: list[str],
        actor: str,
        reason: str,
        request_id: str,
        label: str,
        persisted_run_id: int | None = None,
        stage: str = "replay",
    ) -> int:
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if persisted_run_id is None:
                run = BacktestRun(
                    label=label,
                    config_json="{}",
                )
                session.add(run)
                session.flush()
                audit = AuditEvent(
                    actor=actor,
                    action="backtest.run",
                    target_type="backtest_run",
                    target_id=str(run.id),
                    request_id=request_id,
                )
                persist_sensitive(
                    session,
                    audit,
                    {"reason": reason, "detail_json": "{}"},
                    session_factory=self.session_factory,
                )
            else:
                run = session.get(BacktestRun, persisted_run_id)
                audit = session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "backtest.run",
                        AuditEvent.target_type == "backtest_run",
                        AuditEvent.target_id
                        == str(persisted_run_id),
                        AuditEvent.request_id == request_id,
                    )
                ).scalar_one_or_none()
                if run is None or audit is None:
                    session.rollback()
                    raise RuntimeError(
                        "persisted backtest timeout state unavailable"
                    )
            config = json.loads(run.config_json)
            if not isinstance(config, dict):
                session.rollback()
                raise RuntimeError(
                    "persisted backtest config is invalid"
                )
            config.update(
                {
                    "runtime_seconds": self.runtime_seconds,
                    "status": "timed_out",
                    "symbol_count": len(symbols),
                }
            )
            run.config_json = json.dumps(config, sort_keys=True)
            audit.result_code = "timed_out"
            sensitive_store(
                session,
                self.session_factory,
            ).write_many(
                audit,
                {
                    "detail_json": json.dumps(
                        {
                            "runtime_seconds": self.runtime_seconds,
                            "stage": stage,
                            "symbol_count": len(symbols),
                        },
                        sort_keys=True,
                    )
                },
            )
            session.commit()
            return run.id

    def _persist_failure(
        self,
        *,
        symbols: list[str],
        actor: str,
        reason: str,
        request_id: str,
    ) -> None:
        with self.session_factory() as session:
            persist_sensitive(
                session,
                AuditEvent(
                    actor=actor,
                    action="backtest.run",
                    target_type="backtest_run",
                    target_id="unpersisted",
                    request_id=request_id,
                    result_code="failed",
                ),
                {
                    "reason": reason,
                    "detail_json": json.dumps(
                        {
                            "stage": "launch",
                            "symbol_count": len(symbols),
                        },
                        sort_keys=True,
                    ),
                },
                session_factory=self.session_factory,
            )
            session.commit()


def run_synthetic_backtest(
    session_factory,
    symbols: Optional[list[str]] = None,
    *,
    actor: str,
    reason: str,
    request_id: str,
    bars: int = 650,
    holdout_months: int = 12,
    label: str = "synthetic walk-forward",
    runtime_seconds: int = 1_200,
    deadline: float | None = None,
    stop_event: threading.Event | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[int, EvaluationReport]:
    return BacktestRunner(
        session_factory,
        runtime_seconds=runtime_seconds,
        monotonic=monotonic,
    ).run(
        symbols,
        actor=actor,
        reason=reason,
        request_id=request_id,
        bars=bars,
        holdout_months=holdout_months,
        label=label,
        deadline=deadline,
        stop_event=stop_event,
        start_date=start_date,
        end_date=end_date,
    )
