"""One-call synthetic backtest runner used by the /backtests API.

CI and keyless environments can run a full walk-forward on deterministic synthetic
data. Real runs would build the DataSource from ``download_alpaca_bars`` instead;
everything downstream (engine, metrics, holdout, persistence) is identical.
"""

from __future__ import annotations

import json
from typing import Optional

from ..config import BacktestConfig
from ..db.models import AuditEvent
from .data import DataSource
from .evaluate import persist_report, walk_forward
from .report import EvaluationReport
from .synthetic import make_bars
from ..strategies.breakout import Breakout
from ..strategies.rsi_reversion import RsiReversion
from ..strategies.sma_crossover import SmaCrossover

# Deterministic per-symbol synthetic character (name -> drift, vol).
_PROFILES = {
    "TREND": (0.0009, 0.011),
    "CHOP": (0.0000, 0.016),
    "BEARY": (-0.0006, 0.020),
    "SPY": (0.0004, 0.010),
}
DEFAULT_SYMBOLS = ["TREND", "CHOP", "BEARY"]
STRATEGIES = [SmaCrossover, RsiReversion, Breakout]


def _seed(name: str) -> int:
    return abs(hash(name)) % 100_000


def build_synthetic_source(symbols: list[str], bars: int = 650) -> DataSource:
    frames = {}
    for sym in set(symbols) | {"SPY"}:
        drift, vol = _PROFILES.get(sym, (0.0003, 0.014))
        frames[sym] = make_bars(bars, drift=drift, vol=vol, seed=_seed(sym))
    return DataSource(frames)


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
) -> tuple[int, EvaluationReport]:
    actor = actor.strip()
    reason = reason.strip()
    request_id = request_id.strip()
    if not actor or not reason or not request_id:
        raise ValueError(
            "backtest actor, reason, and request_id must be non-empty"
        )
    symbols = symbols or DEFAULT_SYMBOLS
    try:
        source = build_synthetic_source(symbols, bars)
        report, guard = walk_forward(
            source,
            symbols,
            STRATEGIES,
            backtest_config=BacktestConfig(),
            holdout_months=holdout_months,
            spy_symbol="SPY",
            label=label,
        )
        run_id = persist_report(
            session_factory,
            report,
            guard,
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        return run_id, report
    except Exception:
        with session_factory() as session:
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
        raise
