"""The sacred holdout (Phase 7 guardrail #1).

The most recent ``holdout_months`` of history are quarantined. Parameter sweeps
against the holdout are refused outright, and every holdout access is logged. The
holdout may be evaluated at most as a final, one-shot check — never tuned on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_DAYS_PER_MONTH = 365.25 / 12


class HoldoutViolation(Exception):
    """Raised when code tries to run a parameter sweep against the holdout."""


@dataclass
class HoldoutAccess:
    at: datetime
    context: str
    blocked: bool = False


class HoldoutGuard:
    def __init__(self, timeline: list[datetime], holdout_months: int = 12) -> None:
        if not timeline:
            raise ValueError("empty timeline")
        last = max(timeline)
        self.holdout_start = last - timedelta(days=_DAYS_PER_MONTH * holdout_months)
        self.access_log: list[HoldoutAccess] = []

    def is_holdout(self, ts: datetime) -> bool:
        return ts >= self.holdout_start

    def split(self, timeline: list[datetime]) -> tuple[list[datetime], list[datetime]]:
        dev = [t for t in timeline if t < self.holdout_start]
        hold = [t for t in timeline if t >= self.holdout_start]
        return dev, hold

    def log_access(self, context: str, blocked: bool = False) -> None:
        self.access_log.append(
            HoldoutAccess(at=datetime.now(timezone.utc), context=context, blocked=blocked)
        )

    def forbid_sweep(self, window: str) -> None:
        """Guard a parameter sweep. Sweeping the holdout is a hard error and logged."""
        if window == "holdout":
            self.log_access("parameter sweep attempted on holdout", blocked=True)
            raise HoldoutViolation(
                "parameter sweeps against the holdout are forbidden — the holdout "
                "is evaluated once, never tuned on"
            )

    def evaluate_holdout(self, context: str) -> None:
        """Record a legitimate one-shot holdout evaluation (allowed, but logged)."""
        self.log_access(f"holdout evaluation: {context}", blocked=False)
