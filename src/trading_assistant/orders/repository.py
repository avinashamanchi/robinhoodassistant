"""Transactional persistence primitives for the recoverable order outbox."""

from __future__ import annotations

import json
import time
from datetime import datetime
from decimal import Decimal

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.broker.models import (
    FillQuantityRelation,
    OrderStatus,
    exact_fill_exceeds_order_quantity,
    fill_quantity_relation,
)
from trading_assistant.db.models import (
    AuditEvent,
    CircuitBreakerState,
    FILL_RECONCILIATION_REQUIRED,
    Fill,
    Order,
    Proposal,
    RiskEvent,
    RuleGroup,
    fill_has_trusted_identity,
)
from trading_assistant.risk.breakers import (
    BreakerScope,
    trip_in_session,
)
from trading_assistant.risk.submission_barrier import (
    SubmissionBarrier,
    serialized_writer,
)


def _require_context(
    actor: str,
    reason: str,
    request_id: str,
) -> tuple[str, str, str]:
    actor = actor.strip()
    reason = reason.strip()
    request_id = request_id.strip()
    if not actor or not reason or not request_id:
        raise ValueError(
            "order mutation actor, reason, and request_id must be non-empty"
        )
    return actor, reason, request_id


def _audit_order_mutation(
    session: Session,
    *,
    order_id: int,
    actor: str,
    reason: str,
    request_id: str,
    action: str,
    result_code: str,
    detail: dict[str, object] | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            target_type="order",
            target_id=str(order_id),
            request_id=request_id,
            reason=reason,
            result_code=result_code,
            detail_json=json.dumps(detail or {}, sort_keys=True),
        )
    )


def _audit_group_mutation(
    session: Session,
    *,
    group_id: int,
    actor: str,
    reason: str,
    request_id: str,
    action: str,
    result_code: str,
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            target_type="rule_group",
            target_id=str(group_id),
            request_id=request_id,
            reason=reason,
            result_code=result_code,
        )
    )


class OrderRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.submission_barrier = SubmissionBarrier(
            session_factory
        )

    @serialized_writer
    def record_approval(
        self, order_id: int, actor: str, reason: str, request_id: str, now: datetime
    ) -> bool:
        """Atomically record one human approval and its audit event."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )

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

    @serialized_writer
    def claim_submission(
        self,
        order_id: int,
        now: datetime,
        breaker_scope_keys: tuple[str, ...],
        *,
        breaker_trips=(),
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        """Claim once iff every relevant durable breaker is absent or clear."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        scope_keys = tuple(dict.fromkeys(breaker_scope_keys))
        if not scope_keys or any(not key for key in scope_keys):
            raise ValueError("submission claim requires stable breaker scope keys")
        with self.session_factory() as session:
            for intent in breaker_trips:
                trip_in_session(
                    session,
                    intent.scope,
                    intent.reason,
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=reason,
                )
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
                if breaker_trips:
                    session.commit()
                else:
                    session.rollback()
                return False
            source_rule_group_id = session.scalar(
                select(Proposal.source_rule_group_id).where(
                    Proposal.order_id == order_id
                )
            )
            if source_rule_group_id is not None:
                group_result = session.execute(
                    update(RuleGroup)
                    .where(RuleGroup.id == source_rule_group_id)
                    .values(
                        reconciliation_required=True,
                        updated_at=now,
                    )
                )
                if group_result.rowcount:
                    _audit_group_mutation(
                        session,
                        group_id=source_rule_group_id,
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                        action="rule_group.reconciliation_latch",
                        result_code="required",
                    )
            _audit_order_mutation(
                session,
                order_id=order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="order.submission_claim",
                result_code=OrderStatus.SUBMITTING.value,
            )
            session.commit()
            return True

    @serialized_writer
    def expire_if_eligible(
        self,
        order_id: int,
        now: datetime,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> OrderStatus | None:
        """Expire only a still-pending approval and return the resulting status.

        A failed compare-and-set returns the current status so a retry cannot
        overwrite a submission claim that won the race.
        """
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
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
                _audit_order_mutation(
                    session,
                    order_id=order_id,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    action="order.expire",
                    result_code=OrderStatus.EXPIRED.value,
                )
                session.commit()
                return OrderStatus(status)
            session.rollback()
            current = session.get(Order, order_id)
            return OrderStatus(current.status) if current is not None else None

    @serialized_writer
    def record_submission_result(
        self,
        order_id: int,
        status: OrderStatus,
        broker_order_id: str | None,
        error_code: str,
        now: datetime,
        filled_qty: Decimal = Decimal(0),
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> OrderStatus:
        """Record the single definitive or indeterminate post-send outcome."""
        actor = actor.strip()
        reason = reason.strip()
        request_id = request_id.strip()
        if not actor or not reason or not request_id:
            raise ValueError(
                "submission result actor, reason, and request_id "
                "must be non-empty"
            )
        if status not in {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.ACCEPTANCE_UNKNOWN,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }:
            raise ValueError(f"invalid submission result {status.value}")
        with self.session_factory() as session:
            requested_qty = session.scalar(
                select(Order.qty).where(Order.id == order_id)
            )
            authoritative_qty = self._authoritative_fill_qty(
                session,
                order_id,
                broker_order_id,
            )
            exact_fill_overflow = exact_fill_exceeds_order_quantity(
                requested_qty,
                authoritative_qty,
            )
            (
                requires_fill_reconciliation,
                invalid_cumulative,
                cumulative_contradiction,
            ) = (
                self._requires_fill_reconciliation(
                    status,
                    filled_qty,
                    authoritative_qty,
                )
            )
            acceptance_state = (
                FILL_RECONCILIATION_REQUIRED
                if requires_fill_reconciliation or exact_fill_overflow
                else status.value
            )
            persisted_error_code = (
                "fill_quantity_exceeds_order"
                if exact_fill_overflow
                else (
                    "invalid_cumulative_fill"
                    if invalid_cumulative
                    else (
                        "cumulative_fill_contradiction"
                        if cumulative_contradiction
                        else error_code
                    )
                )
            )
            persisted_status = (
                OrderStatus.ACCEPTANCE_UNKNOWN
                if (
                    invalid_cumulative
                    or cumulative_contradiction
                    or exact_fill_overflow
                )
                else status
            )
            result = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.SUBMITTING.value,
                )
                .values(
                    status=persisted_status.value,
                    broker_order_id=broker_order_id,
                    acceptance_state=acceptance_state,
                    last_error_code=persisted_error_code,
                    updated_at=now,
                    version=Order.version + 1,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError(f"order {order_id} lost submission claim")
            if (
                invalid_cumulative
                or cumulative_contradiction
                or exact_fill_overflow
            ):
                if exact_fill_overflow:
                    drift_reason = (
                        f"authoritative fill quantity {authoritative_qty} for "
                        f"order {order_id} exceeds requested quantity "
                        f"{requested_qty}"
                    )
                elif invalid_cumulative:
                    drift_reason = (
                        f"broker cumulative fill {filled_qty} for order "
                        f"{order_id} is invalid"
                    )
                else:
                    drift_reason = (
                        f"broker cumulative fill {filled_qty} for order "
                        f"{order_id} is below authoritative local quantity "
                        f"{authoritative_qty}"
                    )
                trip_in_session(
                    session,
                    BreakerScope.broker_drift(),
                    drift_reason,
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=reason,
                )
            if persisted_status is OrderStatus.ACCEPTANCE_UNKNOWN:
                source_rule_group_id = session.scalar(
                    select(Proposal.source_rule_group_id).where(
                        Proposal.order_id == order_id
                    )
                )
                if source_rule_group_id is not None:
                    group_result = session.execute(
                        update(RuleGroup)
                        .where(RuleGroup.id == source_rule_group_id)
                        .values(
                            reconciliation_required=True,
                            updated_at=now,
                        )
                    )
                    if group_result.rowcount:
                        _audit_group_mutation(
                            session,
                            group_id=source_rule_group_id,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            action="rule_group.reconciliation_latch",
                            result_code="required",
                        )
            _audit_order_mutation(
                session,
                order_id=order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="order.submission_result",
                result_code=persisted_status.value,
                detail={"error_code": persisted_error_code},
            )
            session.commit()
            return persisted_status

    @serialized_writer
    def record_invalid_broker_identity(
        self,
        order_id: int,
        reason: str,
        now: datetime,
        *,
        actor: str,
        context_reason: str,
        request_id: str,
    ) -> None:
        """Atomically latch an indeterminate post-send identity and drift."""
        self.record_invalid_broker_data(
            order_id,
            reason,
            now,
            broker_order_id=None,
            error_code="invalid_broker_identity",
            actor=actor,
            context_reason=context_reason,
            request_id=request_id,
        )

    @serialized_writer
    def record_invalid_broker_data(
        self,
        order_id: int,
        reason: str,
        now: datetime,
        *,
        broker_order_id: str | None,
        error_code: str,
        actor: str,
        context_reason: str,
        request_id: str,
    ) -> None:
        """Atomically latch malformed synchronous broker truth and drift."""
        reason = reason.strip()
        actor = actor.strip()
        context_reason = context_reason.strip()
        request_id = request_id.strip()
        if not reason:
            raise ValueError("invalid broker data reason must be non-empty")
        if not actor or not context_reason or not request_id:
            raise ValueError(
                "invalid broker data actor, reason, and request_id "
                "must be non-empty"
            )
        if error_code not in {
            "invalid_broker_data",
            "invalid_broker_identity",
            "invalid_cumulative_fill",
        }:
            raise ValueError("unsupported broker data integrity error code")
        trusted_broker_order_id = (
            broker_order_id.strip()
            if isinstance(broker_order_id, str) and broker_order_id.strip()
            else None
        )
        with self.session_factory() as session:
            result = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.SUBMITTING.value,
                )
                .values(
                    status=OrderStatus.ACCEPTANCE_UNKNOWN.value,
                    broker_order_id=trusted_broker_order_id,
                    acceptance_state=FILL_RECONCILIATION_REQUIRED,
                    last_error_code=error_code,
                    updated_at=now,
                    version=Order.version + 1,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError(f"order {order_id} lost submission claim")
            source_rule_group_id = session.scalar(
                select(Proposal.source_rule_group_id).where(
                    Proposal.order_id == order_id
                )
            )
            if source_rule_group_id is not None:
                group_result = session.execute(
                    update(RuleGroup)
                    .where(RuleGroup.id == source_rule_group_id)
                    .values(
                        reconciliation_required=True,
                        updated_at=now,
                    )
                )
                if group_result.rowcount:
                    _audit_group_mutation(
                        session,
                        group_id=source_rule_group_id,
                        actor=actor,
                        reason=context_reason,
                        request_id=request_id,
                        action="rule_group.reconciliation_latch",
                        result_code="required",
                    )
            trip_in_session(
                session,
                BreakerScope.broker_drift(),
                f"invalid broker submission data for order {order_id}",
                actor,
                request_id=request_id,
                now=now,
                audit_reason=context_reason,
            )
            _audit_order_mutation(
                session,
                order_id=order_id,
                actor=actor,
                reason=context_reason,
                request_id=request_id,
                action="order.submission_result",
                result_code=OrderStatus.ACCEPTANCE_UNKNOWN.value,
                detail={"error_code": error_code},
            )
            session.commit()

    @staticmethod
    def _authoritative_fill_qty(
        session: Session,
        order_id: int,
        broker_order_id: str | None,
    ) -> Decimal:
        local_fills = session.scalars(
            select(Fill).where(Fill.order_id == order_id)
        ).all()
        synthetic_prefix = (
            f"{broker_order_id}:" if broker_order_id is not None else None
        )
        return sum(
            (
                fill.qty
                for fill in local_fills
                if fill_has_trusted_identity(fill)
                and (
                    synthetic_prefix is None
                    or not fill.broker_fill_id.startswith(synthetic_prefix)
                )
            ),
            Decimal(0),
        )

    @staticmethod
    def _requires_fill_reconciliation(
        status: OrderStatus,
        filled_qty: Decimal,
        authoritative_qty: Decimal,
    ) -> tuple[bool, bool, bool]:
        relation = fill_quantity_relation(filled_qty, authoritative_qty)
        invalid_cumulative = relation is None
        remote_fill_ahead = relation is FillQuantityRelation.AHEAD
        cumulative_contradiction = relation is FillQuantityRelation.BEHIND
        return (
            status
            in {
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
            }
            or invalid_cumulative
            or remote_fill_ahead
            or cumulative_contradiction,
            # Contradictory cumulative truth also requires reconciliation.
            # It is returned separately so callers can preserve the local
            # status and persist a durable drift reason.
            invalid_cumulative,
            cumulative_contradiction,
        )

    @serialized_writer
    def resolve_acceptance(
        self,
        order_id: int,
        broker_order_id: str | None,
        status: OrderStatus,
        filled_qty: Decimal,
        now: datetime,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        """Atomically persist acceptance and latch unresolved cumulative fills."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
        if broker_order_id is None:
            return False
        with self.session_factory() as session:
            requested_qty = session.scalar(
                select(Order.qty).where(Order.id == order_id)
            )
            authoritative_qty = self._authoritative_fill_qty(
                session,
                order_id,
                broker_order_id,
            )
            exact_fill_overflow = exact_fill_exceeds_order_quantity(
                requested_qty,
                authoritative_qty,
            )
            (
                requires_fill_reconciliation,
                invalid_cumulative,
                cumulative_contradiction,
            ) = (
                self._requires_fill_reconciliation(
                    status,
                    filled_qty,
                    authoritative_qty,
                )
            )
            acceptance_state = (
                FILL_RECONCILIATION_REQUIRED
                if requires_fill_reconciliation or exact_fill_overflow
                else "accepted"
            )
            values: dict[str, object] = {
                "broker_order_id": broker_order_id,
                "acceptance_state": acceptance_state,
                "last_reconciled_at": now,
                "last_error_code": (
                    "fill_quantity_exceeds_order"
                    if exact_fill_overflow
                    else (
                        "invalid_cumulative_fill"
                        if invalid_cumulative
                        else (
                            "cumulative_fill_contradiction"
                            if cumulative_contradiction
                            else ""
                        )
                    )
                ),
                "updated_at": now,
                "version": Order.version + 1,
            }
            if not cumulative_contradiction and not exact_fill_overflow:
                values["status"] = status.value
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
                .values(**values)
            )
            if result.rowcount != 1:
                session.rollback()
                return False
            if cumulative_contradiction or exact_fill_overflow:
                drift_reason = (
                    (
                        f"authoritative fill quantity {authoritative_qty} for "
                        f"order {order_id} exceeds requested quantity "
                        f"{requested_qty}"
                    )
                    if exact_fill_overflow
                    else (
                        f"broker cumulative fill {filled_qty} for order "
                        f"{order_id} is below authoritative local quantity "
                        f"{authoritative_qty}"
                    )
                )
                trip_in_session(
                    session,
                    BreakerScope.broker_drift(),
                    drift_reason,
                    actor,
                    now=now,
                    request_id=request_id,
                    audit_reason=reason,
                )
            _audit_order_mutation(
                session,
                order_id=order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="order.reconcile",
                result_code=(
                    OrderStatus.ACCEPTANCE_UNKNOWN.value
                    if cumulative_contradiction or exact_fill_overflow
                    else status.value
                ),
                detail={
                    "changed_fields": [
                        "acceptance_state",
                        "broker_order_id",
                        "last_error_code",
                        "last_reconciled_at",
                        "status",
                    ]
                },
            )
            session.commit()
            return not exact_fill_overflow

    @serialized_writer
    def record_pre_submission_rejection(
        self,
        order_id: int,
        reasons: tuple[str, ...],
        now: datetime,
        *,
        breaker_trips=(),
        actor: str,
        reason: str,
        request_id: str,
    ) -> None:
        """Persist a fresh deterministic risk rejection before a broker send."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
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
            for intent in breaker_trips:
                trip_in_session(
                    session,
                    intent.scope,
                    intent.reason,
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=reason,
                )
            _audit_order_mutation(
                session,
                order_id=order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="order.reject_execution_risk",
                result_code=OrderStatus.REJECTED.value,
            )
            session.commit()

    @serialized_writer
    def expire_approved(
        self,
        order_id: int,
        now: datetime,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        """Expire a still-unclaimed approval before it can reach the broker."""
        actor, reason, request_id = _require_context(
            actor, reason, request_id
        )
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
            _audit_order_mutation(
                session,
                order_id=order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="order.expire_approved",
                result_code=OrderStatus.EXPIRED.value,
            )
            session.commit()
            return True
