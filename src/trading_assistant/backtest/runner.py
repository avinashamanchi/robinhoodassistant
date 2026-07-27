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
from typing import Callable, Optional

from ..config import BacktestConfig
from ..db.models import AuditEvent, BacktestRun
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


def build_synthetic_source(
    symbols: list[str],
    bars: int = 650,
    *,
    cancel_check: Callable[[], None] | None = None,
) -> DataSource:
    frames = {}
    for sym in set(symbols) | {"SPY"}:
        if cancel_check is not None:
            cancel_check()
        drift, vol = _PROFILES.get(sym, (0.0003, 0.014))
        frames[sym] = make_bars(bars, drift=drift, vol=vol, seed=_seed(sym))
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
    ) -> tuple[int, EvaluationReport]:
        actor = actor.strip()
        reason = reason.strip()
        request_id = request_id.strip()
        if not actor or not reason or not request_id:
            raise ValueError(
                "backtest actor, reason, and request_id must be non-empty"
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
        try:
            control.check()
            source = build_synthetic_source(
                selected_symbols,
                bars,
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
            )
            control.check()
            run_id = persist_report(
                self.session_factory,
                report,
                guard,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
            return run_id, report
        except BacktestTimedOut:
            event.set()
            run_id = self._persist_timeout(
                symbols=selected_symbols,
                actor=actor,
                reason=reason,
                request_id=request_id,
                label=label,
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
    ) -> int:
        with self.session_factory() as session:
            run = BacktestRun(
                label=label,
                config_json=json.dumps(
                    {
                        "runtime_seconds": self.runtime_seconds,
                        "status": "timed_out",
                        "symbol_count": len(symbols),
                    },
                    sort_keys=True,
                ),
            )
            session.add(run)
            session.flush()
            session.add(
                AuditEvent(
                    actor=actor,
                    action="backtest.run",
                    target_type="backtest_run",
                    target_id=str(run.id),
                    request_id=request_id,
                    reason=reason,
                    result_code="timed_out",
                    detail_json=json.dumps(
                        {
                            "runtime_seconds": self.runtime_seconds,
                            "stage": "replay",
                            "symbol_count": len(symbols),
                        },
                        sort_keys=True,
                    ),
                )
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
            session.add(
                AuditEvent(
                    actor=actor,
                    action="backtest.run",
                    target_type="backtest_run",
                    target_id="unpersisted",
                    request_id=request_id,
                    reason=reason,
                    result_code="failed",
                    detail_json=json.dumps(
                        {
                            "stage": "launch",
                            "symbol_count": len(symbols),
                        },
                        sort_keys=True,
                    ),
                )
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
    )
