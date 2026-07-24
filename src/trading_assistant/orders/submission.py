"""Durable, one-way submission orchestration for approved order outbox rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.broker.base import (
    BrokerDataIntegrityError,
    BrokerSubmissionRejected,
)
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    order_result_identity_error,
)
from trading_assistant.db.models import Order
from trading_assistant.risk.breakers import relevant_scopes_for_symbol
from trading_assistant.risk.engine import RiskResult
from trading_assistant.risk.submission_barrier import SubmissionBarrier

from .repository import OrderRepository


_DEFINITIVE_BROKER_STATUSES = frozenset(
    {
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)


@dataclass(frozen=True)
class SubmissionResult:
    order_id: int
    status: OrderStatus
    broker_order_id: str | None = None
    risk_reasons: tuple[str, ...] = ()


def order_to_request(order: Order) -> OrderRequest:
    """Construct the broker request solely from validated persisted columns."""
    return OrderRequest(
        ticker=order.ticker,
        side=OrderSide(order.side),
        order_type=OrderType(order.order_type),
        idempotency_key=order.idempotency_key,
        qty=order.qty,
        notional=order.notional,
        limit_price=order.limit_price,
    )


def bracket_prices(order: Order) -> tuple[Decimal, Decimal]:
    """Parse the small, validated bracket payload; never execute stored data."""
    try:
        payload = json.loads(order.submission_payload_json)
        if not isinstance(payload, dict) or set(payload) != {"take_profit", "stop_loss"}:
            raise ValueError("invalid bracket submission payload")
        take_profit = Decimal(str(payload["take_profit"]))
        stop_loss = Decimal(str(payload["stop_loss"]))
    except (json.JSONDecodeError, InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid bracket submission payload") from exc
    if take_profit <= 0 or stop_loss <= 0:
        raise ValueError("bracket prices must be positive")
    return take_profit, stop_loss


class OrderSubmissionService:
    def __init__(
        self,
        repository: OrderRepository,
        session_factory: sessionmaker[Session],
        broker,
        snapshot_service,
        risk_for_symbol: Callable[[str], object],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.repository = repository
        self.session_factory = session_factory
        self.broker = broker
        self.snapshot_service = snapshot_service
        self.risk_for_symbol = risk_for_symbol
        self.now = now
        self.submission_barrier = SubmissionBarrier(session_factory)

    def _risk_check(
        self, request: OrderRequest, order_id: int
    ) -> RiskResult:
        snapshot = self.snapshot_service.assemble_for_execution(
            request.ticker, exclude_order_id=order_id
        )
        return self.risk_for_symbol(request.ticker).check(
            request, snapshot
        )

    def submit(self, order_id: int) -> SubmissionResult:
        now = self.now()
        with self.session_factory() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise KeyError(f"order {order_id} not found")
            current = OrderStatus(order.status)
            if current is OrderStatus.ACCEPTANCE_UNKNOWN:
                return SubmissionResult(order_id, current, order.broker_order_id)
            if current is not OrderStatus.APPROVAL_RECORDED:
                return SubmissionResult(order_id, current, order.broker_order_id)
            expired = order.proposal is not None and order.proposal.is_expired(now)
            request = order_to_request(order)
            submission_kind = order.submission_kind
            payload_order = order
            session.expunge(payload_order)
        if expired:
            self.repository.expire_approved(order_id, now)
            return SubmissionResult(order_id, OrderStatus.EXPIRED)
        bracket_payload = (
            bracket_prices(payload_order) if submission_kind == "bracket" else None
        )

        while True:
            with self.submission_barrier.hold_submission() as barrier_guard:
                # The barrier makes this snapshot fresh relative to every
                # earlier risk writer and claim/send/persist sequence.
                # Provider reads and the pure risk check still occur before,
                # and outside, the claim transaction.
                risk = self._risk_check(request, order_id)
                if risk.rejected:
                    reasons = tuple(risk.reasons)
                    self.repository.record_pre_submission_rejection(
                        order_id, reasons, now
                    )
                    return SubmissionResult(
                        order_id,
                        OrderStatus.REJECTED,
                        risk_reasons=reasons,
                    )

                # A risk writer that announced itself after snapshot assembly
                # makes this evaluation stale. Release the main lock so the
                # writer can commit, then rebuild every provider/database
                # input before considering a claim.
                with barrier_guard.claim_if_current() as risk_is_current:
                    if not risk_is_current:
                        continue

                    claim_now = self.now()
                    breaker_scope_keys = tuple(
                        scope.key for scope in relevant_scopes_for_symbol(
                            request.ticker
                        )
                    )
                    if not self.repository.claim_submission(
                        order_id, claim_now, breaker_scope_keys
                    ):
                        if self.repository.expire_approved(
                            order_id, claim_now
                        ):
                            return SubmissionResult(
                                order_id, OrderStatus.EXPIRED
                            )
                        with self.session_factory() as session:
                            changed = session.get(Order, order_id)
                            if changed is None:
                                raise KeyError(
                                    f"order {order_id} not found"
                                )
                            return SubmissionResult(
                                order_id,
                                OrderStatus(changed.status),
                                changed.broker_order_id,
                            )
                    try:
                        if submission_kind == "bracket":
                            assert bracket_payload is not None
                            take_profit, stop_loss = bracket_payload
                            broker_result = self.broker.submit_bracket(
                                request, take_profit, stop_loss
                            )
                        else:
                            broker_result = self.broker.submit_order(request)
                    except BrokerSubmissionRejected as exc:
                        self.repository.record_submission_result(
                            order_id,
                            OrderStatus.REJECTED,
                            None,
                            exc.stable_code,
                            self.now(),
                        )
                        return SubmissionResult(
                            order_id, OrderStatus.REJECTED
                        )
                    except BrokerDataIntegrityError as exc:
                        self.repository.record_invalid_broker_data(
                            order_id,
                            str(exc),
                            self.now(),
                            broker_order_id=exc.broker_order_id,
                            error_code="invalid_broker_data",
                        )
                        return SubmissionResult(
                            order_id,
                            OrderStatus.ACCEPTANCE_UNKNOWN,
                            exc.broker_order_id,
                        )
                    except Exception as exc:
                        self.repository.record_submission_result(
                            order_id,
                            OrderStatus.ACCEPTANCE_UNKNOWN,
                            None,
                            type(exc).__name__,
                            self.now(),
                        )
                        return SubmissionResult(
                            order_id, OrderStatus.ACCEPTANCE_UNKNOWN
                        )
                    identity_error = order_result_identity_error(
                        broker_result,
                        request.idempotency_key,
                        request.ticker,
                    )
                    if identity_error is not None:
                        self.repository.record_invalid_broker_identity(
                            order_id,
                            identity_error,
                            self.now(),
                        )
                        return SubmissionResult(
                            order_id,
                            OrderStatus.ACCEPTANCE_UNKNOWN,
                        )
                    status = broker_result.status
                    error_code = ""
                    if status not in _DEFINITIVE_BROKER_STATUSES:
                        status = OrderStatus.ACCEPTANCE_UNKNOWN
                        error_code = "invalid_broker_submission_status"
                    persisted_status = self.repository.record_submission_result(
                        order_id,
                        status,
                        broker_result.broker_order_id,
                        error_code,
                        self.now(),
                        broker_result.filled_qty,
                    )
                    return SubmissionResult(
                        order_id,
                        persisted_status,
                        broker_result.broker_order_id,
                    )
