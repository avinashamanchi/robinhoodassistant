"""ORM models, the order state machine (A4), kill-switch state (A3), and the
compare-and-set approval primitive (A5).

Money columns use ``Numeric`` mapped to :class:`~decimal.Decimal`. All timestamps
are stored in UTC (A2); timezone conversion happens only at market-day boundaries.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    update,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)

from ..broker.models import (
    FILL_NUMERIC_PRECISION,
    FILL_NUMERIC_SCALE,
    OrderStatus,
)


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
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
        }
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
NONTERMINAL_STATES = frozenset(
    status for status in OrderStatus if status not in TERMINAL_STATES
)


class OrderStateMachine:
    """Enforces legal lifecycle transitions. Illegal moves raise (A4)."""

    @staticmethod
    def can_transition(current: OrderStatus, new: OrderStatus) -> bool:
        return new in _LEGAL_TRANSITIONS.get(current, frozenset())

    @staticmethod
    def is_reachable(initial: OrderStatus, current: OrderStatus) -> bool:
        """Return whether ``current`` is reachable without bypassing the graph."""

        if initial == current:
            return True
        visited = {initial}
        pending = [initial]
        while pending:
            state = pending.pop()
            for successor in _LEGAL_TRANSITIONS.get(state, frozenset()):
                if successor == current:
                    return True
                if successor not in visited:
                    visited.add(successor)
                    pending.append(successor)
        return False

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
    # Durable plan-order cancellation intent. Error classification is kept in
    # ``last_error_code`` so an indeterminate broker response can never erase
    # the retry obligation.
    plan_cancel_state: Mapped[str] = mapped_column(
        String(16),
        default="none",
        server_default="none",
        index=True,
    )
    version: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    proposal: Mapped[Optional["Proposal"]] = relationship(
        back_populates="order", uselist=False
    )
    fills: Mapped[list["Fill"]] = relationship(back_populates="order")


FILL_RECONCILIATION_REQUIRED = "fill_reconcile_required"
FILL_RECONCILIATION_TRUSTED = "trusted"
FILL_RECONCILIATION_QUARANTINED = "quarantined"
FILL_RECONCILIATION_SUPERSEDED = "superseded"
PLAN_CANCEL_NONE = "none"
PLAN_CANCEL_REQUESTED = "requested"
PLAN_CANCEL_INDETERMINATE = "indeterminate"
PLAN_CANCEL_SETTLED = "settled"


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


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(128), default="operator")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    authenticated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    source_rule_group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("rule_groups.id"), nullable=True, index=True
    )
    source_rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("rules.id"), nullable=True, index=True
    )
    plan_generation: Mapped[int] = mapped_column(default=0)
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
    activation: Mapped[str] = mapped_column(
        String(20), default="immediate"
    )
    terminal_on_trigger: Mapped[bool] = mapped_column(
        Boolean, default=True
    )


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
    qty: Mapped[Decimal] = mapped_column(
        Numeric(FILL_NUMERIC_PRECISION, FILL_NUMERIC_SCALE)
    )
    price: Mapped[Decimal] = mapped_column(
        Numeric(FILL_NUMERIC_PRECISION, FILL_NUMERIC_SCALE)
    )
    # Broker's fill event id — unique so a duplicated fill webhook is idempotent.
    broker_fill_id: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    reconciliation_state: Mapped[str] = mapped_column(
        String(24),
        default=FILL_RECONCILIATION_TRUSTED,
        server_default=FILL_RECONCILIATION_TRUSTED,
    )
    filled_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, index=True
    )

    order: Mapped[Optional["Order"]] = relationship(back_populates="fills")


def fill_has_trusted_identity(fill: Fill) -> bool:
    """Whether a ledger row is eligible for execution-risk arithmetic."""
    return (
        fill.reconciliation_state == FILL_RECONCILIATION_TRUSTED
        and isinstance(fill.broker_fill_id, str)
        and bool(fill.broker_fill_id.strip())
    )


def fill_requires_reconciliation(fill: Fill) -> bool:
    """Whether a legacy unidentified row still needs authoritative matching."""
    if fill.reconciliation_state == FILL_RECONCILIATION_SUPERSEDED:
        return False
    return (
        fill.reconciliation_state == FILL_RECONCILIATION_QUARANTINED
        or not isinstance(fill.broker_fill_id, str)
        or not fill.broker_fill_id.strip()
    )


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
    __table_args__ = (
        CheckConstraint(
            "(authority_version = 0 AND authority_digest IS NULL) OR "
            "(authority_version = 1 "
            "AND length(authority_digest) = 64 "
            "AND authority_digest NOT GLOB '*[^0-9a-f]*')",
            name="ck_trade_plans_authority_evidence",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    paper_only: Mapped[bool] = mapped_column(Boolean, default=True)
    shadow: Mapped[bool] = mapped_column(Boolean, default=False)  # D1 shadow-mode plan
    plan_json: Mapped[str] = mapped_column(Text)      # full TradePlan
    sized_json: Mapped[str] = mapped_column(Text)     # SizedTradePlan
    authority_version: Mapped[int] = mapped_column(default=0)
    authority_digest: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    entry_filled_qty: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal(0)
    )
    exit_filled_qty: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal(0)
    )
    residual_generation: Mapped[int] = mapped_column(default=0)
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
    source: Mapped[str] = mapped_column(
        String(24),
        default="daemon",
        unique=True,
    )
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


class StartupReconciliationState(Base):
    """Durable proof that the newest process-start generation saw broker truth."""

    __tablename__ = "startup_reconciliation_state"

    broker: Mapped[str] = mapped_column(String(64), primary_key=True)
    generation: Mapped[int] = mapped_column(default=0)
    completed_generation: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(
        String(16), default="required", index=True
    )
    actor: Mapped[str] = mapped_column(String(128), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    request_id: Mapped[str] = mapped_column(String(64), default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow
    )


class RateWindow(Base):
    __tablename__ = "rate_windows"

    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_name: Mapped[str] = mapped_column(String(32), index=True)
    window_started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    hits: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column(default=0)


class ConcurrencyLease(Base):
    __tablename__ = "concurrency_leases"

    resource_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    generation: Mapped[int] = mapped_column(default=0)


class RuntimeTenure(Base):
    """Fenced runtime ownership and exclusive sensitive maintenance."""

    __tablename__ = "runtime_tenures"
    __table_args__ = (
        CheckConstraint(
            "("
            "resource_key = 'runtime:app' AND role = 'app'"
            ") OR ("
            "resource_key = 'runtime:daemon' AND role = 'daemon'"
            ") OR ("
            "resource_key = 'runtime:mcp' AND role = 'mcp'"
            ") OR ("
            "resource_key = 'runtime:validation' "
            "AND role = 'validation'"
            ") OR ("
            "resource_key = 'sensitive-migration:global' "
            "AND role = 'maintenance'"
            ")",
            name="ck_runtime_tenures_resource_role",
        ),
        CheckConstraint(
            "state IN ('held','released','fenced')",
            name="ck_runtime_tenures_state",
        ),
        CheckConstraint(
            "generation > 0",
            name="ck_runtime_tenures_generation_positive",
        ),
        CheckConstraint(
            "length(owner_id) = 36",
            name="ck_runtime_tenures_owner_id",
        ),
        CheckConstraint(
            "pid > 0",
            name="ck_runtime_tenures_pid_positive",
        ),
        CheckConstraint(
            "length(process_start_identity) BETWEEN 1 AND 256",
            name="ck_runtime_tenures_process_identity",
        ),
        CheckConstraint(
            "acquired_at <= renewed_at AND renewed_at <= expires_at",
            name="ck_runtime_tenures_timestamp_order",
        ),
        CheckConstraint(
            "(state = 'held' AND released_at IS NULL "
            "AND renewed_at < expires_at) OR "
            "(state IN ('released','fenced') "
            "AND released_at IS NOT NULL "
            "AND released_at = expires_at)",
            name="ck_runtime_tenures_lifecycle",
        ),
    )

    resource_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(16), index=True)
    owner_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column()
    pid: Mapped[int] = mapped_column()
    process_start_identity: Mapped[str] = mapped_column(String(256))
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime())
    renewed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        index=True,
    )
    released_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )


class MutationInterlock(Base):
    """Non-expiring authority that fences one protected mutation resource."""

    __tablename__ = "mutation_interlocks"
    __table_args__ = (
        CheckConstraint(
            "generation >= 0",
            name="ck_mutation_interlocks_generation_nonnegative",
        ),
        CheckConstraint(
            "operation IN ("
            "'order_approve','order_reject','breaker_reset','order_cancel',"
            "'portfolio_reconcile','order_sync','panic','analysis',"
            "'plan_approve','plan_cancel','proposal_batch','backtest'"
            ")",
            name="ck_mutation_interlocks_operation",
        ),
        CheckConstraint(
            "outcome_code IN ("
            "'','handler_completed','request_cancelled','handler_failed',"
            "'lease_renewal_unproven','lease_ownership_lost',"
            "'panic_settlement_unproven','lease_release_unproven',"
            "'interlock_settlement_unproven'"
            ")",
            name="ck_mutation_interlocks_outcome",
        ),
        CheckConstraint(
            "("
            "state = 'active' AND outcome_code = '' "
            "AND worker_finished_at IS NULL"
            ") OR ("
            "state = 'settled' AND outcome_code = 'handler_completed' "
            "AND worker_finished_at IS NOT NULL"
            ") OR ("
            "state = 'uncertain' AND outcome_code NOT IN "
            "('', 'handler_completed')"
            ")",
            name="ck_mutation_interlocks_state_lifecycle",
        ),
    )

    resource_key: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    owner: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column()
    operation: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
    )
    outcome_code: Mapped[str] = mapped_column(String(64), default="")
    worker_finished_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utcnow,
    )


class ProviderBudgetDay(Base):
    __tablename__ = "provider_budget_days"
    __table_args__ = (
        CheckConstraint(
            "calls_used >= 0",
            name="ck_provider_budget_days_calls_nonnegative",
        ),
        CheckConstraint(
            "input_tokens_used >= 0",
            name="ck_provider_budget_days_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "output_tokens_used >= 0",
            name="ck_provider_budget_days_output_tokens_nonnegative",
        ),
    )

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    budget_day: Mapped[date] = mapped_column(Date, primary_key=True)
    calls_used: Mapped[int] = mapped_column(default=0)
    input_tokens_used: Mapped[int] = mapped_column(default=0)
    output_tokens_used: Mapped[int] = mapped_column(default=0)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    reconciliation_code: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ProviderReservation(Base):
    __tablename__ = "provider_reservations"
    __table_args__ = (
        CheckConstraint(
            "state IN "
            "('reserved', 'started', 'settled', 'unknown', 'released')",
            name="ck_provider_reservations_state",
        ),
        CheckConstraint(
            "input_reserved >= 0",
            name="ck_provider_reservations_input_reserved_nonnegative",
        ),
        CheckConstraint(
            "output_reserved >= 0",
            name="ck_provider_reservations_output_reserved_nonnegative",
        ),
        CheckConstraint(
            "input_actual IS NULL OR input_actual >= 0",
            name="ck_provider_reservations_input_actual_nonnegative",
        ),
        CheckConstraint(
            "output_actual IS NULL OR output_actual >= 0",
            name="ck_provider_reservations_output_actual_nonnegative",
        ),
    )

    reservation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    budget_day: Mapped[date] = mapped_column(Date, index=True)
    state: Mapped[str] = mapped_column(
        String(16), default="reserved", index=True
    )
    input_reserved: Mapped[int] = mapped_column()
    output_reserved: Mapped[int] = mapped_column()
    input_actual: Mapped[Optional[int]] = mapped_column(nullable=True)
    output_actual: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    settled_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class SensitiveMigrationState(Base):
    """Singleton proof of sensitive-field migration and active-key state."""

    __tablename__ = "sensitive_migration_state"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1",
            name="ck_sensitive_migration_state_singleton",
        ),
        CheckConstraint(
            "schema_version > 0",
            name="ck_sensitive_migration_state_schema_positive",
        ),
        CheckConstraint(
            "state IN ('required','migrating','complete','rotating','failed')",
            name="ck_sensitive_migration_state_state",
        ),
        CheckConstraint(
            "length(active_key_id) BETWEEN 8 AND 64 "
            "AND substr(active_key_id,1,1) GLOB '[A-Za-z0-9]' "
            "AND active_key_id NOT GLOB '*[^A-Za-z0-9._-]*'",
            name="ck_sensitive_migration_state_key_id",
        ),
        CheckConstraint(
            "rows_total >= 0 AND rows_completed >= 0 "
            "AND rows_completed <= rows_total",
            name="ck_sensitive_migration_state_progress",
        ),
        CheckConstraint(
            "backup_path_hash IS NULL OR "
            "(length(backup_path_hash) = 64 "
            "AND backup_path_hash NOT GLOB '*[^0-9a-f]*')",
            name="ck_sensitive_migration_state_backup_hash",
        ),
        CheckConstraint(
            "completed_at IS NULL OR "
            "(started_at IS NOT NULL AND completed_at >= started_at)",
            name="ck_sensitive_migration_state_timestamp_order",
        ),
        CheckConstraint(
            "(state = 'required' AND started_at IS NULL "
            "AND completed_at IS NULL AND rows_completed = 0) OR "
            "(state IN ('migrating','rotating','failed') "
            "AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(state = 'complete' AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL "
            "AND rows_completed = rows_total "
            "AND backup_path_hash IS NOT NULL)",
            name="ck_sensitive_migration_state_lifecycle",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(primary_key=True, default=1)
    schema_version: Mapped[int] = mapped_column(default=1)
    state: Mapped[str] = mapped_column(
        String(16),
        default="required",
        index=True,
    )
    active_key_id: Mapped[str] = mapped_column(String(64))
    rows_total: Mapped[int] = mapped_column(default=0)
    rows_completed: Mapped[int] = mapped_column(default=0)
    backup_path_hash: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utcnow,
        index=True,
    )


class CandidateNonce(Base):
    """Hashed, expiring replay protection for untrusted candidate actions."""

    __tablename__ = "candidate_nonces"
    __table_args__ = (
        CheckConstraint(
            "length(nonce_hash) = 64 "
            "AND nonce_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_candidate_nonces_hash",
        ),
        CheckConstraint(
            "length(actor) BETWEEN 1 AND 128",
            name="ck_candidate_nonces_actor",
        ),
        CheckConstraint(
            "length(kind) BETWEEN 1 AND 32",
            name="ck_candidate_nonces_kind",
        ),
        CheckConstraint(
            "length(request_id) BETWEEN 1 AND 64",
            name="ck_candidate_nonces_request_id",
        ),
    )

    nonce_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(),
        nullable=True,
        index=True,
    )
    request_id: Mapped[str] = mapped_column(String(64), index=True)


class CandidateQueueReceipt(Base):
    """Metadata-only recovery receipt for one explicit signed queue action."""

    __tablename__ = "candidate_queue_receipts"
    __table_args__ = (
        UniqueConstraint(
            "session_binding_hash",
            "kind",
            "idempotency_key_hash",
            name="uq_candidate_queue_receipt_identity",
        ),
        UniqueConstraint(
            "nonce_hash",
            name="uq_candidate_queue_receipt_nonce",
        ),
        CheckConstraint(
            "length(session_binding_hash) = 64 "
            "AND session_binding_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_candidate_queue_receipts_session_hash",
        ),
        CheckConstraint(
            "length(actor_hash) = 64 "
            "AND actor_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_candidate_queue_receipts_actor_hash",
        ),
        CheckConstraint(
            "kind IN ('order','rule')",
            name="ck_candidate_queue_receipts_kind",
        ),
        CheckConstraint(
            "length(idempotency_key_hash) = 64 "
            "AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_candidate_queue_receipts_idempotency_hash",
        ),
        CheckConstraint(
            "length(candidate_hash) = 64 "
            "AND candidate_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_candidate_queue_receipts_candidate_hash",
        ),
        CheckConstraint(
            "length(reason_hash) = 64 "
            "AND reason_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_candidate_queue_receipts_reason_hash",
        ),
        CheckConstraint(
            "length(nonce_hash) = 64 "
            "AND nonce_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_candidate_queue_receipts_nonce_hash",
        ),
        CheckConstraint(
            "state IN ('reserved','target_persisted','completed')",
            name="ck_candidate_queue_receipts_state",
        ),
        CheckConstraint(
            "length(request_id) BETWEEN 1 AND 64",
            name="ck_candidate_queue_receipts_request_id",
        ),
        CheckConstraint(
            "outcome_code IS NULL OR "
            "length(outcome_code) BETWEEN 1 AND 64",
            name="ck_candidate_queue_receipts_outcome",
        ),
        CheckConstraint(
            "target_id IS NULL OR target_id > 0",
            name="ck_candidate_queue_receipts_target_id",
        ),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_candidate_queue_receipts_http_status",
        ),
        CheckConstraint(
            "(state = 'reserved' AND target_id IS NULL "
            "AND outcome_code IS NULL AND http_status IS NULL "
            "AND completed_at IS NULL) OR "
            "(state = 'target_persisted' AND target_id IS NOT NULL "
            "AND outcome_code IS NOT NULL AND http_status IS NOT NULL "
            "AND completed_at IS NULL) OR "
            "(state = 'completed' AND outcome_code IS NOT NULL "
            "AND http_status IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_candidate_queue_receipts_lifecycle",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_binding_hash: Mapped[str] = mapped_column(String(64), index=True)
    actor_hash: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(8), index=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64))
    candidate_hash: Mapped[str] = mapped_column(String(64))
    reason_hash: Mapped[str] = mapped_column(String(64))
    nonce_hash: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(24), default="reserved", index=True)
    outcome_code: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    target_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    http_status: Mapped[Optional[int]] = mapped_column(nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )


class UntrustedIngestEvent(Base):
    """Metadata-only record; raw external text is never stored."""

    __tablename__ = "untrusted_ingest_events"
    __table_args__ = (
        CheckConstraint(
            "length(source_hash) = 64 "
            "AND source_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_untrusted_ingest_events_source_hash",
        ),
        CheckConstraint(
            "length(content_hash) = 64 "
            "AND content_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_untrusted_ingest_events_content_hash",
        ),
        CheckConstraint(
            "byte_length >= 0",
            name="ck_untrusted_ingest_events_byte_length_nonnegative",
        ),
        CheckConstraint(
            "json_valid(flags_json)",
            name="ck_untrusted_ingest_events_flags_json",
        ),
        CheckConstraint(
            "state IN ('received','summarized','rejected','failed')",
            name="ck_untrusted_ingest_events_state",
        ),
        CheckConstraint(
            "state != 'summarized' OR summary_decision_id IS NOT NULL",
            name="ck_untrusted_ingest_events_summary",
        ),
        Index(
            "ux_untrusted_ingest_source_content",
            "source_hash",
            "content_hash",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    byte_length: Mapped[int] = mapped_column()
    flags_json: Mapped[str] = mapped_column(Text, default="[]")
    state: Mapped[str] = mapped_column(
        String(16),
        default="received",
        index=True,
    )
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utcnow,
        index=True,
    )
    summary_decision_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("llm_decisions.id"),
        nullable=True,
        index=True,
    )


class PanicReceipt(Base):
    __tablename__ = "panic_receipts"

    account_scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    lease_generation: Mapped[int] = mapped_column(
        default=0,
        server_default="0",
    )
    state: Mapped[str] = mapped_column(String(16), index=True)
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


# ── Atomic approval primitive (A5) ──────────────────────────────
def approve_proposed(
    session: Session,
    order_id: int,
    *,
    actor: str,
    reason: str,
    request_id: str,
) -> None:
    """Record one identified approval via a compare-and-set.

    Emits a single UPDATE guarded on the current status. If it changes zero rows
    the order was not PROPOSED (already approved/rejected/expired or gone), which
    for a concurrent second approver means a conflict -> raises ApprovalConflict.
    The caller commits the state transition and its audit event together.
    """
    if not actor.strip() or not reason.strip() or not request_id.strip():
        raise ValueError(
            "approval actor, reason, and request_id must be non-empty"
        )
    idempotency_key = session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == OrderStatus.PROPOSED.value)
        .values(
            status=OrderStatus.APPROVAL_RECORDED.value,
            approval_actor=actor,
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
    from ..security.sensitive_fields import persist_sensitive
    from .lifecycle_proofs import augment_lifecycle_detail

    order = session.get(Order, order_id)
    if order is None:
        raise ApprovalConflict(f"order {order_id} disappeared")
    persist_sensitive(
        session,
        order,
        {"approval_reason": reason},
    )
    persist_sensitive(
        session,
        AuditEvent(
            actor=actor,
            action="order.approve",
            target_type="order",
            target_id=str(order_id),
            request_id=request_id,
            idempotency_key=idempotency_key,
            result_code=OrderStatus.APPROVAL_RECORDED.value,
        ),
        {
            "reason": reason,
            "detail_json": json.dumps(
                augment_lifecycle_detail(
                    session,
                    target_type="order",
                    target_id=order_id,
                ),
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    )
