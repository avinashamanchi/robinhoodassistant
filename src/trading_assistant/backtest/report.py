"""Backtest report structures. Every simulated result carries the mandatory
disclaimer (Phase 7 guardrail #3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .metrics import Metrics

SIMULATED_LABEL = "Simulated — past performance does not predict future results."


@dataclass
class ReportRow:
    symbol: str
    strategy: str
    window: str                 # development | holdout | full
    metrics: Metrics
    benchmark: Metrics          # buy-and-hold on the same symbol/window

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "window": self.window,
            "metrics": self.metrics.to_dict(),
            "benchmark_buy_and_hold": self.benchmark.to_dict(),
            "beat_buy_and_hold": self.metrics.total_return_pct
            > self.benchmark.total_return_pct,
        }


@dataclass
class EvaluationReport:
    rows: list[ReportRow] = field(default_factory=list)
    holdout_start: Optional[datetime] = None
    label: str = ""
    disclaimer: str = SIMULATED_LABEL

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "holdout_start": self.holdout_start.isoformat() if self.holdout_start else None,
            "disclaimer": self.disclaimer,
            "rows": [r.to_dict() for r in self.rows],
        }

    def render_table(self) -> str:
        header = (
            f"{'symbol':8} {'strategy':16} {'window':12} "
            f"{'ret%':>8} {'B&H%':>8} {'sharpe':>7} {'maxDD%':>8} {'beat':>5}"
        )
        lines = [self.disclaimer, "", header, "-" * len(header)]
        for r in self.rows:
            beat = "yes" if r.metrics.total_return_pct > r.benchmark.total_return_pct else "no"
            lines.append(
                f"{r.symbol:8} {r.strategy:16} {r.window:12} "
                f"{r.metrics.total_return_pct:8.2f} {r.benchmark.total_return_pct:8.2f} "
                f"{r.metrics.sharpe:7.2f} {r.metrics.max_drawdown_pct:8.2f} {beat:>5}"
            )
        return "\n".join(lines)
