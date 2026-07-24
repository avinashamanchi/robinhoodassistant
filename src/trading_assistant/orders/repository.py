"""Transactional persistence primitives for the recoverable order outbox."""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import (
    AuditEvent,
    CircuitBreakerState,
    FILL_RECONCILIATION_REQUIRED,
    Order,
    Proposal,
    RiskEvent,
    RuleGroup,
)


class OrderRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def record_approval(
        self, order_id: int, actor: str, reason: str, request_id: str, now: datetime
    ) -> bool:
        """Atomically record one human approval and its audit event."""
        if not actor.strip() or not reason.strip():
            raise ValueError("approval actor and reason must be non-empty")

        started = time.perf_counter()
        with self.session_factory() as session:
            idempotency_key = session.execute(
                update(Order)
                .where(Order.id == order_id, Order.status == OrderStatus.PROPOSED.value)
                .values(
                    status=OrderStatus.APPROVAL_RECORDED.value,
                    approval_actor=actor,
                    approval_reason=reason,
                    approved_at=now,
                    updated_at=now,
                    version=Order.version + 1,
                )
                .returning(Order.idempotency_key)
            ).scalar_one_or_none()
            if idempotency_key is None:
                session.rollback()
                return False
            elapsed_ms = round((time.perf_counter() - started) * 1000)
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
                    latency_ms=elapsed_ms,
                )
            )
            session.commit()
            return True

    def claim_submission(
        self,
        order_id: int,
        now: datetime,
        breaker_scope_keys: tuple[str, ...],
    ) -> bool:
        """Claim once iff every relevant durable breaker is absent or clear."""
        scope_keys = tuple(dict.fromkeys(breaker_scope_keys))
        if not scope_keys or any(not key for key in scope_keys):
            raise ValueError("submission claim requires stable breaker scope keys")
        with self.session_factory() as session:
            result = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.APPROVAL_RECORDED.value,
                    or_(
                        ~Order.proposal.has(),
                        Order.proposal.has(Proposal.expires_at > now),
                    ),
                    ~exists().where(
                        CircuitBreakerState.scope_key.in_(scope_keys),
                        CircuitBreakerState.tripped.is_(True),
                    ),
                )
                .values(
                    status=OrderStatus.SUBMITTING.value,
                    submission_attempt=Order.submission_attempt + 1,
                    submission_started_at=now,
                    acceptance_state="pending",
                    updated_at=now,
                    version=Order.version + 1,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return False
            source_rule_group_id = session.scalar(
                select(Proposal.source_rule_group_id).where(
                    Proposal.order_id == order_id
                )
            )
            if source_rule_group_id is not None:
                session.execute(
                    update(RuleGroup)
                    .where(RuleGroup.id == source_rule_group_id)
                    .values(
                        reconciliation_required=True,
                        updated_at=now,
                    )
                )
            session.commit()
            return True

    def expire_if_eligible(self, order_id: int, now: datetime) -> OrderStatus | None:
        """Expire only a still-pending approval and return the resulting status.

        A failed compare-and-set returns the current status so a retry cannot
        overwrite a submission claim that won the race.
        """
        with self.session_factory() as session:
            status = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status.in_(
                        (
                            OrderStatus.PROPOSED.value,
                            OrderStatus.APPROVAL_RECORDED.value,
                        )
                    ),
                )
                .values(
                    status=OrderStatus.EXPIRED.value,
                    updated_at=now,
                    version=Order.version + 1,
                )
                .returning(Order.status)
            ).scalar_one_or_none()
            if status is not None:
                session.commit()
                return OrderStatus(status)
            session.rollback()
            current = session.get(Order, order_id)
            return OrderStatus(current.status) if current is not None else None

    def record_submission_result(
        self,
        order_id: int,
        status: OrderStatus,
        broker_order_id: str | None,
        error_code: str,
        now: datetime,
    ) -> None:
        """Record the single definitive or indeterminate post-send outcome."""
        if status not in {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.ACCEPTANCE_UNKNOWN,
            OrderStatus.REJECTED,
        }:
            raise ValueError(f"invalid submission result {status.value}")
        acceptance_state = (
            FILL_RECONCILIATION_REQUIRED
            if status
            in {
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
            }
            else status.value
        )
        with self.session_factory() as session:
            result = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.SUBMITTING.value,
                )
                .values(
                    status=status.value,
                    broker_order_id=broker_order_id,
                    acceptance_state=acceptance_state,
                    last_error_code=error_code,
                    updated_at=now,
                    version=Order.version + 1,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError(f"order {order_id} lost submission claim")
            if status is OrderStatus.ACCEPTANCE_UNKNOWN:
                source_rule_group_id = session.scalar(
                    select(Proposal.source_rule_group_id).where(
                        Proposal.order_id == order_id
                    )
                )
                if source_rule_group_id is not None:
                    session.execute(
                        update(RuleGroup)
                        .where(RuleGroup.id == source_rule_group_id)
                        .values(
                            reconciliation_required=True,
                            updated_at=now,
                        )
                    )
            session.commit()

    def resolve_acceptance(
        self,
        order_id: int,
        broker_order_id: str | None,
        status: OrderStatus,
        now: datetime,
    ) -> bool:
        """Persist broker truth for one indeterminate submission without resending."""
        if broker_order_id is None:
            return False
        with self.session_factory() as session:
            result = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status.in_(
                        (
                            OrderStatus.SUBMITTING.value,
                            OrderStatus.ACCEPTANCE_UNKNOWN.value,
                        )
                    ),
                )
                .values(
                    status=status.value,
                    broker_order_id=broker_order_id,
                    acceptance_state="accepted",
                    last_reconciled_at=now,
                    last_error_code="",
                    updated_at=now,
                    version=Order.version + 1,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    def record_pre_submission_rejection(
        self, order_id: int, reasons: tuple[str, ...], now: datetime
    ) -> None:
        """Persist a fresh deterministic risk rejection before a broker send."""
        with self.session_factory() as session:
            result = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.APPROVAL_RECORDED.value,
                )
                .values(
                    status=OrderStatus.REJECTED.value,
                    last_error_code="risk_rejected",
                    updated_at=now,
                    version=Order.version + 1,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError(f"order {order_id} changed during risk rejection")
            session.add(
                RiskEvent(
                    order_id=order_id,
                    event_type="rejection",
                    reason="execution-time: " + "; ".join(reasons),
                )
            )
            session.commit()

    def expire_approved(self, order_id: int, now: datetime) -> bool:
        """Expire a still-unclaimed approval before it can reach the broker."""
        with self.session_factory() as session:
            result = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.APPROVAL_RECORDED.value,
                    Order.proposal.has(Proposal.expires_at <= now),
                )
                .values(
                    status=OrderStatus.EXPIRED.value,
                    updated_at=now,
                    version=Order.version + 1,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True
