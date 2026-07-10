"""A5: approval is atomic compare-and-set — succeeds exactly once, else conflicts."""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import (
    ApprovalConflict,
    Order,
    OrderStateMachine,
    Proposal,
    approve_proposed,
    utcnow,
)


def _make_proposed(session_factory) -> int:
    with session_factory() as s:
        order = Order(
            idempotency_key="idem-approve",
            ticker="AAPL",
            side="buy",
            order_type="market",
            status=OrderStatus.PROPOSED.value,
        )
        s.add(order)
        s.flush()
        s.add(
            Proposal(
                order_id=order.id, expires_at=utcnow() + timedelta(minutes=15)
            )
        )
        s.commit()
        return order.id


def test_first_approval_succeeds(session_factory):
    oid = _make_proposed(session_factory)
    with session_factory() as s:
        approve_proposed(s, oid)
        s.commit()
    with session_factory() as s:
        assert s.get(Order, oid).status == OrderStatus.APPROVED.value


def test_second_approval_conflicts(session_factory):
    oid = _make_proposed(session_factory)
    with session_factory() as s:
        approve_proposed(s, oid)
        s.commit()
    # A second approver sees the row is no longer PROPOSED -> conflict (would be 409).
    with session_factory() as s:
        with pytest.raises(ApprovalConflict):
            approve_proposed(s, oid)


def test_cannot_approve_rejected_order(session_factory):
    oid = _make_proposed(session_factory)
    with session_factory() as s:
        order = s.get(Order, oid)
        OrderStateMachine.transition(order, OrderStatus.REJECTED)
        s.commit()
    with session_factory() as s:
        with pytest.raises(ApprovalConflict):
            approve_proposed(s, oid)
