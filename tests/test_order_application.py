"""Approval identity is persisted before any submission can be claimed."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Barrier, Thread

import pytest
from sqlalchemy import event, func, select

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import AuditEvent, Order, Proposal
from trading_assistant.orders.application import (
    ApprovalCommand,
    ApprovalConflict,
    OrderApplicationService,
)
from trading_assistant.security.sensitive_fields import sensitive_store


def _proposed_order_id(make_service) -> tuple[object, int]:
    service = make_service()
    result = service.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        actor="operator:test",
        reason="order application proposal",
        request_id="order-application-proposal",
    )
    return service, result["order_id"]


def _fail_audit_action(action):
    def fail(session, flush_context, instances):
        if any(
            isinstance(row, AuditEvent) and row.action == action
            for row in session.new
        ):
            raise RuntimeError(f"injected {action} audit failure")

    return fail


def test_approval_records_actor_reason_and_audit(make_service):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)

    result = app.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed receipt",
            datetime.now(timezone.utc),
            "order-application-approval",
        )
    )

    assert result.status is OrderStatus.APPROVAL_RECORDED
    with service.session_factory() as session:
        row = session.get(Order, order_id)
        assert row.approval_actor == "operator:avi"
        assert (
            sensitive_store(session).read(row, "approval_reason")
            == "reviewed receipt"
        )
        assert session.query(AuditEvent).filter_by(action="order.approve").count() == 1


@pytest.mark.parametrize("actor,reason", [("", "reviewed"), ("operator:avi", "")])
def test_approval_requires_non_empty_actor_and_reason(make_service, actor, reason):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)

    with pytest.raises(ValueError, match="actor, reason, and request_id"):
        app.approve(
            ApprovalCommand(
                order_id,
                actor,
                reason,
                datetime.now(timezone.utc),
                "order-application-invalid-context",
            )
        )


def test_approval_requires_explicit_non_empty_request_id(make_service):
    _service, order_id = _proposed_order_id(make_service)

    with pytest.raises(ValueError, match="request_id"):
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed",
            datetime.now(timezone.utc),
            "",
        )


def test_approval_compare_and_set_succeeds_once(make_service):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)
    command = ApprovalCommand(
        order_id,
        "operator:avi",
        "reviewed",
        datetime.now(timezone.utc),
        "order-application-repeat",
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
                    "order-application-concurrent",
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
            order_id,
            "operator:avi",
            "reviewed",
            datetime.now(timezone.utc),
            "order-application-submission-claim",
        )
    )

    scopes = ("operator_global", "equity")
    assert app.repository.claim_submission(
        order_id,
        datetime.now(timezone.utc),
        scopes,
        actor="operator:avi",
        reason="claim approved order",
        request_id="order-application-submission-claim",
    ) is True
    assert app.repository.claim_submission(
        order_id,
        datetime.now(timezone.utc),
        scopes,
        actor="operator:avi",
        reason="retry approved order claim",
        request_id="order-application-submission-claim-retry",
    ) is False
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTING.value
        assert order.submission_attempt == 1


def test_expired_approval_retry_does_not_overwrite_submission_claim(make_service):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)
    approved_at = datetime.now(timezone.utc)
    command = ApprovalCommand(
        order_id,
        "operator:avi",
        "reviewed",
        approved_at,
        "order-application-expiry",
    )
    app.approve(command)
    assert app.repository.claim_submission(
        order_id,
        approved_at,
        ("operator_global", "equity"),
        actor=command.actor,
        reason=command.reason,
        request_id=command.request_id,
    ) is True

    with pytest.raises(ApprovalConflict):
        app.approve(
            ApprovalCommand(
                order_id,
                "operator:avi",
                "retry after submission claim",
                approved_at.replace(year=approved_at.year + 1),
                "order-application-expiry-retry",
            )
        )

    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTING.value
        assert order.version == 2


def test_expired_human_approval_has_exact_atomic_status_audit(make_service):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)
    with service.session_factory() as session:
        proposal = session.scalar(
            select(Proposal).where(Proposal.order_id == order_id)
        )
        expired_at = proposal.expires_at.replace(
            year=proposal.expires_at.year + 1
        )

    result = app.approve(
        ApprovalCommand(
            order_id,
            "operator:expiry",
            "review expired approval",
            expired_at,
            "order-approval-expired",
        )
    )

    assert result.status is OrderStatus.EXPIRED
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "order.expire",
                AuditEvent.target_id == str(order_id),
            )
        )
        audit_reason = sensitive_store(session).read(audit, "reason")
    assert order.status == OrderStatus.EXPIRED.value
    assert (
        audit.actor,
        audit_reason,
        audit.request_id,
        audit.result_code,
    ) == (
        "operator:expiry",
        "review expired approval",
        "order-approval-expired",
        OrderStatus.EXPIRED.value,
    )


def test_expired_approval_mutation_rolls_back_on_audit_failure(make_service):
    service, order_id = _proposed_order_id(make_service)
    app = OrderApplicationService(service.session_factory)
    with service.session_factory() as session:
        proposal = session.scalar(
            select(Proposal).where(Proposal.order_id == order_id)
        )
        expired_at = proposal.expires_at.replace(
            year=proposal.expires_at.year + 1
        )

    listener = _fail_audit_action("order.expire")
    session_type = service.session_factory.class_
    event.listen(session_type, "before_flush", listener)
    try:
        with pytest.raises(
            RuntimeError,
            match="injected order.expire audit failure",
        ):
            app.approve(
                ApprovalCommand(
                    order_id,
                    "operator:expiry",
                    "rollback expired approval",
                    expired_at,
                    "order-approval-expired-rollback",
                )
            )
    finally:
        event.remove(session_type, "before_flush", listener)

    with service.session_factory() as session:
        assert (
            session.get(Order, order_id).status
            == OrderStatus.PROPOSED.value
        )
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.request_id
                == "order-approval-expired-rollback"
            )
        ) == 0
