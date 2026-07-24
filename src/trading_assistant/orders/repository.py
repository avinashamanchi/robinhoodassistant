"""Transactional persistence primitives for the recoverable order outbox."""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import AuditEvent, Order


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

    def claim_submission(self, order_id: int, now: datetime) -> bool:
        """Claim a recorded approval exactly once before any broker I/O occurs."""
        with self.session_factory() as session:
            result = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.APPROVAL_RECORDED.value,
                )
                .values(
                    status=OrderStatus.SUBMITTING.value,
                    submission_attempt=Order.submission_attempt + 1,
                    submission_started_at=now,
                    updated_at=now,
                    version=Order.version + 1,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return False
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
