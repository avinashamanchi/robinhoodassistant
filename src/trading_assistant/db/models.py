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
    Index,
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
            OrderStatus.APPROVAL_RECORDED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.APPROVAL_RECORDED: frozenset(
        {OrderStatus.SUBMITTING, OrderStatus.EXPIRED, OrderStatus.REJECTED}
    ),
    OrderStatus.SUBMITTING: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.ACCEPTANCE_UNKNOWN,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.ACCEPTANCE_UNKNOWN: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELED,
        }
    ),
    # Legacy deserialization only. No runtime transition may enter or leave it.
    OrderStatus.APPROVED: frozenset(),
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
    __table_args__ = (
        Index("ix_orders_status_broker_order_id", "status", "broker_order_id"),
        Index("ix_orders_status_idempotency_key", "status", "idempotency_key"),
    )

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
    approval_actor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    approval_reason: Mapped[str] = mapped_column(Text, default="")
    approved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    submission_kind: Mapped[str] = mapped_column(String(16), default="simple")
    submission_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    submission_attempt: Mapped[int] = mapped_column(default=0)
    submission_started_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    acceptance_state: Mapped[str] = mapped_column(String(24), default="not_started")
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_error_code: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    proposal: Mapped[Optional["Proposal"]] = relationship(
        back_populates="order", uselist=False
    )
    fills: Mapped[list["Fill"]] = relationship(back_populates="order")


FILL_RECONCILIATION_REQUIRED = "fill_reconcile_required"


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    result_code: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(default=0)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    source_rule_group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("rule_groups.id"), nullable=True, index=True
    )
    reasoning: Mapped[str] = mapped_column(Text, default="")
    ttl_minutes: Mapped[int] = mapped_column(default=15)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())

    order: Mapped["Order"] = relationship(back_populates="proposal")

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        return now >= self.expires_at


class RuleGroup(Base):
    __tablename__ = "rule_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(16), default="active", index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    terminal_rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("rules.id"), nullable=True
    )
    version: Mapped[int] = mapped_column(default=0)
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("rule_groups.id"), nullable=False, index=True
    )
    payload_version: Mapped[int] = mapped_column(default=1)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    condition_json: Mapped[str] = mapped_column(Text)
    action_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # Phase 8: trade-plan rule groups + exit-rule state.
    plan_id: Mapped[Optional[int]] = mapped_column(index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="price")  # price|entry|target|stop|trailing|time
    fraction: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6), nullable=True)
    # Trailing-stop high-water mark — PERSISTED so it survives a daemon restart
    # (an in-memory HWM would reset and silently widen the stop).
    hwm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 6), nullable=True)
    deadline: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    pre_approved: Mapped[bool] = mapped_column(Boolean, default=False)


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
    analyst_version: Mapped[str] = mapped_column(String(16), default="v1", index=True)
    report_json: Mapped[str] = mapped_column(Text)      # full AnalysisReport
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    grade: Mapped[Optional["GradedCallRow"]] = relationship(
        back_populates="report", uselist=False
    )


class TradePlanRow(Base):
    __tablename__ = "trade_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed|approved|canceled
    paper_only: Mapped[bool] = mapped_column(Boolean, default=True)
    shadow: Mapped[bool] = mapped_column(Boolean, default=False)  # D1 shadow-mode plan
    plan_json: Mapped[str] = mapped_column(Text)      # full TradePlan
    sized_json: Mapped[str] = mapped_column(Text)     # SizedTradePlan
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ShadowCall(Base):
    """A shadow-mode call awaiting horizon grading (D1). No order is ever placed."""

    __tablename__ = "shadow_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("analysis_reports.id"))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    grade_after: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    graded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class GradedCallRow(Base):
    __tablename__ = "graded_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("analysis_reports.id"), unique=True)
    correct: Mapped[bool] = mapped_column(Boolean)
    forward_return_pct: Mapped[float] = mapped_column(Numeric(12, 4))
    graded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    report: Mapped["AnalysisReportRow"] = relationship(back_populates="grade")


class Heartbeat(Base):
    """Daemon liveness marker — written each loop; read by GET /health (D3)."""

    __tablename__ = "heartbeats"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(24), default="daemon")
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class HoldoutAccessLog(Base):
    """Audit trail: every holdout access, especially blocked sweep attempts (#1)."""

    __tablename__ = "holdout_access_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    context: Mapped[str] = mapped_column(Text)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)


class CircuitBreakerState(Base):
    """One durable row per typed breaker scope."""

    __tablename__ = "circuit_breaker_state"

    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    target: Mapped[str] = mapped_column(String(32), default="")
    tripped: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(128), default="")
    generation: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AccountRiskState(Base):
    """Durable account high-water mark for one asset class."""

    __tablename__ = "account_risk_state"

    asset_class: Mapped[str] = mapped_column(String(16), primary_key=True)
    high_water_mark: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    last_equity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ReconciliationCursor(Base):
    """Durable high-water mark for one broker activity stream."""

    __tablename__ = "reconciliation_cursors"

    broker: Mapped[str] = mapped_column(String(64), primary_key=True)
    stream: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_activity_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    version: Mapped[int] = mapped_column(default=0)


# ── Atomic approval primitive (A5) ──────────────────────────────
def approve_proposed(
    session: Session,
    order_id: int,
    *,
    actor: str,
    reason: str,
    request_id: str = "",
) -> None:
    """Record one identified approval via a compare-and-set.

    Emits a single UPDATE guarded on the current status. If it changes zero rows
    the order was not PROPOSED (already approved/rejected/expired or gone), which
    for a concurrent second approver means a conflict -> raises ApprovalConflict.
    The caller commits the state transition and its audit event together.
    """
    if not actor.strip() or not reason.strip():
        raise ValueError("approval actor and reason must be non-empty")
    idempotency_key = session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == OrderStatus.PROPOSED.value)
        .values(
            status=OrderStatus.APPROVAL_RECORDED.value,
            approval_actor=actor,
            approval_reason=reason,
            approved_at=utcnow(),
            updated_at=utcnow(),
            version=Order.version + 1,
        )
        .returning(Order.idempotency_key)
    ).scalar_one_or_none()
    if idempotency_key is None:
        raise ApprovalConflict(
            f"order {order_id} was not in PROPOSED state (already decided?)"
        )
    session.add(
        AuditEvent(
            actor=actor,
            action="order.approve",
            target_type="order",
            target_id=str(order_id),
            request_id=request_id,
            idempotency_key=idempotency_key,
            reason=reason,
            result_code=OrderStatus.APPROVAL_RECORDED.value,
        )
    )


def create_all(engine) -> None:
    Base.metadata.create_all(engine)
