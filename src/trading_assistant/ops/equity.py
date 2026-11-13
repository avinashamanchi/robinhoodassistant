"""Timestamped account-equity snapshots for honest performance measurement.

The daemon records at most one snapshot per interval (default 15 minutes) as
an encrypted ``account.equity_snapshot`` audit event: equity, cash, buying
power, the market value and gross exposure of positions, the number of
positions, and the last trade price of a benchmark symbol (default ``SPY``)
observed at the same moment. Equity drawdown and benchmark comparison are
computed from these snapshots, so they cover only periods the daemon was
running; gaps are reported, not filled.

Recording is best-effort: a failure is logged and never fails the safety
cycle. Nothing here places, changes or cancels orders.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
from typing import Any, Callable, Optional
from uuid import uuid4

from sqlalchemy import select

from ..db.models import AuditEvent
from ..operations.audit import AuditRecorder, MutationContext
from ..security.sensitive_fields import sensitive_store

SNAPSHOT_ACTION = "account.equity_snapshot"
log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EquitySnapshotRecorder:
    def __init__(
        self,
        service,
        *,
        interval_seconds: float = 900.0,
        benchmark: str = "SPY",
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.service = service
        self.interval = timedelta(seconds=interval_seconds)
        self.benchmark = benchmark.upper()
        self.now = now
        self._last: Optional[datetime] = None
        self._recorder = AuditRecorder(service.session_factory)

    def maybe_record(self) -> bool:
        """Record one snapshot if the interval has passed; never raises."""
        observed_at = self.now()
        if self._last is not None and observed_at - self._last < self.interval:
            return False
        try:
            detail = self._observe(observed_at)
            self._recorder.record(
                MutationContext(
                    actor="daemon:equity",
                    request_id=uuid4().hex,
                    reason="periodic account equity snapshot",
                    idempotency_key="",
                ),
                SNAPSHOT_ACTION,
                "account",
                "alpaca-paper",
                "recorded",
                detail,
            )
        except Exception:
            log.warning("equity_snapshot_failed")
            return False
        self._last = observed_at
        return True

    def _observe(self, observed_at: datetime) -> dict[str, Any]:
        broker = self.service.broker
        account = broker.get_account()
        positions = broker.get_positions()
        values = [p.qty * p.current_price for p in positions]
        benchmark_last: Optional[str] = None
        try:
            quote = broker.get_quote(self.benchmark)
            if quote.is_valid:
                benchmark_last = str(quote.last)
        except Exception:
            benchmark_last = None
        return {
            "schema": 1,
            "observed_at": observed_at.isoformat(),
            "source": "alpaca_paper" if self.service.config.trading.broker.value == "alpaca" else "mock",
            "equity": str(account.equity),
            "cash": str(account.cash),
            "buying_power": str(account.buying_power),
            "positions": len(positions),
            "positions_value": str(sum(values, Decimal(0))),
            "gross_exposure": str(sum((abs(v) for v in values), Decimal(0))),
            "benchmark": self.benchmark,
            "benchmark_last": benchmark_last,
        }


def read_snapshots(session_factory) -> list[dict[str, Any]]:
    """Decrypted snapshots in time order (unreadable rows are skipped and counted)."""
    out: list[dict[str, Any]] = []
    with session_factory() as session:
        store = sensitive_store(session, session_factory)
        query = (
            select(AuditEvent)
            .where(AuditEvent.action == SNAPSHOT_ACTION)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
        for event in session.scalars(query):
            try:
                detail = json.loads(store.read(event, "detail_json"))
            except Exception:
                continue
            if isinstance(detail, dict) and "equity" in detail:
                out.append(detail)
    return out


def equity_statistics(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Equity return, max equity drawdown, benchmark return over the same span."""
    points = [
        (datetime.fromisoformat(s["observed_at"]), Decimal(s["equity"]), s.get("benchmark_last"))
        for s in snapshots
    ]
    if len(points) < 2:
        return {"snapshots": len(points), "note": "fewer than two snapshots; nothing to measure"}
    peak = points[0][1]
    worst = Decimal(0)
    worst_pct = Decimal(0)
    largest_gap = timedelta(0)
    for (previous, _equity, _b), (at, equity, _bench) in zip(points, points[1:]):
        largest_gap = max(largest_gap, at - previous)
    for _at, equity, _bench in points:
        peak = max(peak, equity)
        drop = peak - equity
        if drop > worst:
            worst = drop
            worst_pct = (drop / peak * 100) if peak else Decimal(0)
    first, last = points[0], points[-1]
    equity_return = (last[1] / first[1] - 1) * 100 if first[1] else None
    benchmark_return = None
    if first[2] and last[2]:
        benchmark_return = (Decimal(last[2]) / Decimal(first[2]) - 1) * 100
    return {
        "snapshots": len(points),
        "from": first[0].isoformat(),
        "to": last[0].isoformat(),
        "largest_gap_hours": round(largest_gap.total_seconds() / 3600, 2),
        "equity_start": f"{first[1]:.2f}",
        "equity_end": f"{last[1]:.2f}",
        "equity_return_pct": None if equity_return is None else f"{equity_return:.3f}",
        "max_equity_drawdown": f"{worst:.2f}",
        "max_equity_drawdown_pct": f"{worst_pct:.3f}",
        "benchmark": snapshots[0].get("benchmark"),
        "benchmark_return_pct": None if benchmark_return is None else f"{benchmark_return:.3f}",
    }
