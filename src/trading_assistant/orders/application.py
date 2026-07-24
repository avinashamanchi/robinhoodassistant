"""Application service for explicitly identified human approvals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import Order

from .repository import OrderRepository


class ApprovalConflict(RuntimeError):
    """Raised when a proposal cannot consume another human approval."""


@dataclass(frozen=True)
class ApprovalCommand:
    order_id: int
    actor: str
    reason: str
    now: datetime
    request_id: str = ""

    def __post_init__(self) -> None:
        if not self.actor.strip() or not self.reason.strip():
            raise ValueError("approval actor and reason must be non-empty")


@dataclass(frozen=True)
class ApprovalResult:
    order_id: int
    status: OrderStatus


class OrderApplicationService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.repository = OrderRepository(session_factory)

    def approve(self, command: ApprovalCommand) -> ApprovalResult:
        with self.session_factory() as session:
            order = session.get(Order, command.order_id)
            if order is None:
                raise KeyError(f"order {command.order_id} not found")
            if order.proposal is not None and order.proposal.is_expired(command.now):
                status = self.repository.expire_if_eligible(order.id, command.now)
                if status is OrderStatus.EXPIRED:
                    return ApprovalResult(order.id, status)
                if status is None:
                    raise KeyError(f"order {command.order_id} not found")
                raise ApprovalConflict(
                    f"order {command.order_id} approval already consumed ({status.value})"
                )

        request_id = command.request_id or uuid4().hex
        if not self.repository.record_approval(
            command.order_id,
            command.actor,
            command.reason,
            request_id,
            command.now,
        ):
            raise ApprovalConflict(f"order {command.order_id} approval already consumed")
        return ApprovalResult(command.order_id, OrderStatus.APPROVAL_RECORDED)
