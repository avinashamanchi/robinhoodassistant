"""A5: approval is atomic compare-and-set — succeeds exactly once, else conflicts."""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import (
    ApprovalConflict,
    AuditEvent,
    Order,
    OrderStateMachine,
    Proposal,
    approve_proposed,
    utcnow,
)
from trading_assistant.security.sensitive_fields import persist_sensitive
from tests.conftest import decrypt_test_sensitive


def _make_proposed(session_factory) -> int:
    with session_factory() as s:
        order = Order(
            idempotency_key="idem-approve",
            ticker="AAPL",
            side="buy",
            order_type="market",
            status=OrderStatus.PROPOSED.value,
        )
        persist_sensitive(
            s,
            order,
            {"approval_reason": "approval pending"},
        )
        persist_sensitive(
            s,
            Proposal(
                order_id=order.id,
                expires_at=utcnow() + timedelta(minutes=15),
            ),
            {"reasoning": "atomic approval fixture"},
        )
        s.commit()
        return order.id


def test_first_approval_succeeds(session_factory):
    oid = _make_proposed(session_factory)
    with session_factory() as s:
        approve_proposed(
            s,
            oid,
            actor="operator:avi",
            reason="reviewed",
            request_id="atomic-first-approval",
        )
        s.commit()
    with session_factory() as s:
        order = s.get(Order, oid)
        assert order.status == OrderStatus.APPROVAL_RECORDED.value
        assert order.approval_actor == "operator:avi"
        assert decrypt_test_sensitive(
            order,
            "approval_reason",
        ) == "reviewed"
        assert s.query(AuditEvent).filter_by(action="order.approve").count() == 1


def test_second_approval_conflicts(session_factory):
    oid = _make_proposed(session_factory)
    with session_factory() as s:
        approve_proposed(
            s,
            oid,
            actor="operator:avi",
            reason="reviewed",
            request_id="atomic-initial-approval",
        )
        s.commit()
    # A second approver sees the row is no longer PROPOSED -> conflict (would be 409).
    with session_factory() as s:
        with pytest.raises(ApprovalConflict):
            approve_proposed(
                s,
                oid,
                actor="operator:avi",
                reason="retry",
                request_id="atomic-retry-approval",
            )


def test_cannot_approve_rejected_order(session_factory):
    oid = _make_proposed(session_factory)
    with session_factory() as s:
        order = s.get(Order, oid)
        OrderStateMachine.transition(order, OrderStatus.REJECTED)
        s.commit()
    with session_factory() as s:
        with pytest.raises(ApprovalConflict):
            approve_proposed(
                s,
                oid,
                actor="operator:avi",
                reason="reviewed",
                request_id="atomic-rejected-approval",
            )


@pytest.mark.parametrize("actor,reason", [("", "reviewed"), ("operator:avi", "")])
def test_approval_identity_is_required(session_factory, actor, reason):
    oid = _make_proposed(session_factory)
    with session_factory() as s:
        with pytest.raises(ValueError, match="actor, reason, and request_id"):
            approve_proposed(
                s,
                oid,
                actor=actor,
                reason=reason,
                request_id="atomic-invalid-approval",
            )
