"""Approval identity is persisted before any submission can be claimed."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Barrier, Thread

import pytest

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import AuditEvent, Order
from trading_assistant.orders.application import (
    ApprovalCommand,
    ApprovalConflict,
    OrderApplicationService,
)


def _proposed_order_id(make_service) -> tuple[object, int]:
    service = make_service()
    result = service.propose_order("AAPL", "buy", "market", notional="100")
    return service, result["order_id"]


def test_approval_records_actor_reason_and_audit(make_service):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)

    result = app.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed receipt",
            datetime.now(timezone.utc),
        )
    )

    assert result.status is OrderStatus.APPROVAL_RECORDED
    with service.session_factory() as session:
        row = session.get(Order, order_id)
        assert row.approval_actor == "operator:avi"
        assert row.approval_reason == "reviewed receipt"
        assert session.query(AuditEvent).filter_by(action="order.approve").count() == 1


@pytest.mark.parametrize("actor,reason", [("", "reviewed"), ("operator:avi", "")])
def test_approval_requires_non_empty_actor_and_reason(make_service, actor, reason):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)

    with pytest.raises(ValueError, match="actor and reason"):
        app.approve(ApprovalCommand(order_id, actor, reason, datetime.now(timezone.utc)))


def test_approval_compare_and_set_succeeds_once(make_service):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)
    command = ApprovalCommand(
        order_id, "operator:avi", "reviewed", datetime.now(timezone.utc)
    )

    app.approve(command)

    with pytest.raises(ApprovalConflict):
        app.approve(command)


def test_concurrent_approvals_record_exactly_one_audit_event(make_service):
    service, order_id = _proposed_order_id(make_service)
    gate = Barrier(2)
    outcomes: list[str] = []

    def approve() -> None:
        app = OrderApplicationService(service.session_factory)
        gate.wait()
        try:
            app.approve(
                ApprovalCommand(
                    order_id,
                    "operator:avi",
                    "reviewed concurrently",
                    datetime.now(timezone.utc),
                )
            )
            outcomes.append("approved")
        except ApprovalConflict:
            outcomes.append("conflict")

    threads = [Thread(target=approve), Thread(target=approve)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["approved", "conflict"]
    with service.session_factory() as session:
        assert session.query(AuditEvent).filter_by(action="order.approve").count() == 1


def test_submission_claim_succeeds_once_after_approval(make_service):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)
    app.approve(
        ApprovalCommand(
            order_id, "operator:avi", "reviewed", datetime.now(timezone.utc)
        )
    )

    assert app.repository.claim_submission(order_id, datetime.now(timezone.utc)) is True
    assert app.repository.claim_submission(order_id, datetime.now(timezone.utc)) is False
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTING.value
        assert order.submission_attempt == 1
