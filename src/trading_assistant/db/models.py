"""ORM models, the order state machine (A4), kill-switch state (A3), and the
compare-and-set approval primitive (A5).

Money columns use ``Numeric`` mapped to :class:`~decimal.Decimal`. All timestamps
are stored in UTC (A2); timezone conversion happens only at market-day boundaries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    TypeDecorator,
    update,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)

from ..broker.models import OrderStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores datetimes as UTC and always returns them tz-aware (UTC).

    SQLite has no native tz support, so a plain DateTime column round-trips to a
    naive value. That would silently break FIFO P&L, which compares fill times
    against a tz-aware boundary (A2). This decorator guarantees UTC in and out.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


# ── Exceptions ──────────────────────────────────────────────────
class IllegalStateTransition(Exception):
    """Raised when an order is moved between states that are not connected (A4)."""


class ApprovalConflict(Exception):
    """Raised when a second actor tries to approve an already-decided proposal (A5)."""


# ── Order state machine (A4) ────────────────────────────────────
_LEGAL_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PROPOSED: frozenset(
        {
            OrderStatus.APPROVED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELED,
        }
    ),
    # Execution-time risk re-check can still REJECT an approved order.
    OrderStatus.APPROVED: frozenset(
        {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELED}
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED}
    ),
    # Terminal states.
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}

TERMINAL_STATES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)


class OrderStateMachine:
    """Enforces legal lifecycle transitions. Illegal moves raise (A4)."""

    @staticmethod
    def can_transition(current: OrderStatus, new: OrderStatus) -> bool:
        return new in _LEGAL_TRANSITIONS.get(current, frozenset())

    @staticmethod
    def transition(order: "Order", new: OrderStatus) -> None:
        current = OrderStatus(order.status)
        if not OrderStateMachine.can_transition(current, new):
            raise IllegalStateTransition(
                f"illegal transition {current.value} -> {new.value} "
                f"(order id={order.id})"
            )
        order.status = new.value
        order.updated_at = utcnow()


# ── Tables ──────────────────────────────────────────────────────
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(8))
    qty: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 6), nullable=True)
    notional: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 6), nullable=True)
    limit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 6), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default=OrderStatus.PROPOSED.value, index=True
    )
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    proposal: Mapped[Optional["Proposal"]] = relationship(
        back_populates="order", uselist=False
    )
    fills: Mapped[list["Fill"]] = relationship(back_populates="order")


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    ttl_minutes: Mapped[int] = mapped_column(default=15)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())

    order: Mapped["Order"] = relationship(back_populates="proposal")

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        return now >= self.expires_at


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    condition_json: Mapped[str] = mapped_column(Text)
    action_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class LLMDecision(Base):
    __tablename__ = "llm_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    prompt: Mapped[str] = mapped_column(Text)
    tool_calls_json: Mapped[str] = mapped_column(Text, default="[]")
    reasoning_summary: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(32))  # rejection|killswitch_trip|reset
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id"), nullable=True
    )
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    # Broker's fill event id — unique so a duplicated fill webhook is idempotent.
    broker_fill_id: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    filled_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, index=True
    )

    order: Mapped[Optional["Order"]] = relationship(back_populates="fills")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    holdout_start: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    rows: Mapped[list["BacktestMetricRow"]] = relationship(back_populates="run")


class BacktestMetricRow(Base):
    __tablename__ = "backtest_metric_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id"))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(40), index=True)
    window: Mapped[str] = mapped_column(String(16))  # development | holdout | full
    metrics_json: Mapped[str] = mapped_column(Text)

    run: Mapped["BacktestRun"] = relationship(back_populates="rows")


class AnalysisReportRow(Base):
    __tablename__ = "analysis_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime())
    action: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Numeric(6, 4))
    report_json: Mapped[str] = mapped_column(Text)      # full AnalysisReport
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    grade: Mapped[Optional["GradedCallRow"]] = relationship(
        back_populates="report", uselist=False
    )


class GradedCallRow(Base):
    __tablename__ = "graded_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("analysis_reports.id"), unique=True)
    correct: Mapped[bool] = mapped_column(Boolean)
    forward_return_pct: Mapped[float] = mapped_column(Numeric(12, 4))
    graded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    report: Mapped["AnalysisReportRow"] = relationship(back_populates="grade")


class HoldoutAccessLog(Base):
    """Audit trail: every holdout access, especially blocked sweep attempts (#1)."""

    __tablename__ = "holdout_access_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    context: Mapped[str] = mapped_column(Text)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)


class KillSwitchState(Base):
    """One row per asset class. Persisting here means a restart returns tripped (A3).

    Keyed by ``asset_class`` (Phase 7) so equity and crypto trip independently.
    """

    __tablename__ = "killswitch_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_class: Mapped[str] = mapped_column(
        String(16), unique=True, index=True, default="equity"
    )
    tripped: Mapped[bool] = mapped_column(Boolean, default=False)
    tripped_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ── Atomic approval primitive (A5) ──────────────────────────────
def approve_proposed(session: Session, order_id: int) -> None:
    """Transition PROPOSED -> APPROVED exactly once via compare-and-set.

    Emits a single UPDATE guarded on the current status. If it changes zero rows
    the order was not PROPOSED (already approved/rejected/expired or gone), which
    for a concurrent second approver means a conflict -> raises ApprovalConflict.
    The caller commits.
    """
    result = session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == OrderStatus.PROPOSED.value)
        .values(status=OrderStatus.APPROVED.value, updated_at=utcnow())
    )
    if result.rowcount != 1:
        raise ApprovalConflict(
            f"order {order_id} was not in PROPOSED state (already decided?)"
        )


def create_all(engine) -> None:
    Base.metadata.create_all(engine)
