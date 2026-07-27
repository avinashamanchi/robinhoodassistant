"""Durable outbox submission behavior independent of the public HTTP shape."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select

from trading_assistant.broker.base import (
    BrokerDataIntegrityError,
    BrokerSubmissionRejected,
)
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    FILL_ECONOMIC_QUANTUM,
    OrderResult,
    OrderStatus,
)
from trading_assistant.assets import AssetClass
from trading_assistant.db.models import (
    FILL_RECONCILIATION_REQUIRED,
    AuditEvent,
    Fill,
    Order,
    Proposal,
    utcnow,
)
from trading_assistant.dependencies import RequiredDependencyUnavailable
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.orders.submission import OrderSubmissionService
from trading_assistant.risk.breakers import (
    BreakerScope,
    relevant_scopes_for_symbol,
)
from trading_assistant.risk.clock import FakeClock
from trading_assistant.service import TradingService


def _submit(submission, order_id):
    return submission.submit(
        order_id,
        actor="operator:test",
        reason="order submission test",
        request_id=f"order-submission-{order_id}",
    )


def _approved_order(svc) -> int:
    order_id = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        actor="operator:test",
        reason="order submission proposal",
        request_id="order-submission-proposal",
    )["order_id"]
    svc.order_application.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed",
            utcnow(),
            "order-submission-approval",
        )
    )
    return order_id


def _fail_audit_action(action):
    def fail(session, flush_context, instances):
        if any(
            isinstance(row, AuditEvent) and row.action == action
            for row in session.new
        ):
            raise RuntimeError(f"injected {action} audit failure")

    return fail


class AcceptThenDisconnectBroker(MockBroker):
    def submit_order(self, order):
        super().submit_order(order)
        raise ConnectionError("response lost after acceptance")


def test_accept_then_disconnect_becomes_unknown_without_duplicate(make_service):
    broker = AcceptThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    order_id = _approved_order(svc)

    result = _submit(svc.order_submission, order_id)

    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert len(broker._orders_by_key) == 1
    result2 = _submit(svc.order_submission, order_id)
    assert result2.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert len(broker._orders_by_key) == 1
    assert svc.breakers.is_tripped(BreakerScope.broker_drift()) is False
    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert order.last_error_code == "broker_submission_unknown"


def test_synchronous_broker_data_integrity_latches_and_trips_drift(
    make_service,
):
    class MalformedSynchronousBroker(MockBroker):
        def submit_order(self, order):
            accepted = super().submit_order(order)
            raise BrokerDataIntegrityError(
                "malformed synchronous order payload",
                broker_order_id=accepted.broker_order_id,
            )

    broker = MalformedSynchronousBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    order_id = _approved_order(service)

    result = _submit(service.order_submission, order_id)

    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert service.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert order.broker_order_id is not None
        assert order.acceptance_state == FILL_RECONCILIATION_REQUIRED
        assert order.last_error_code == "invalid_broker_data"


def test_broker_call_occurs_after_claim_transaction_commits(make_service):
    svc = make_service()
    order_id = _approved_order(svc)
    checked = {"status": None}
    original = svc.broker.submit_order

    def submit(order):
        with svc.session_factory() as session:
            checked["status"] = session.get(Order, order_id).status
        return original(order)

    svc.broker.submit_order = submit
    _submit(svc.order_submission, order_id)

    assert checked["status"] == OrderStatus.SUBMITTING.value


def test_panic_latch_atomically_prevents_a_later_broker_submit(make_service):
    svc = make_service()
    order_id = _approved_order(svc)
    panic = svc.reconciliation.panic(
        "operator:avi",
        "submission race",
        request_id="order-submission-race-panic",
    )

    result = _submit(svc.order_submission, order_id)

    assert panic.safe is False
    assert panic.local_enumeration == "confirmed"
    assert panic.unconfirmed_order_ids == (order_id,)
    assert panic.unsafe_local_state.live_or_unknown_order_ids == (
        order_id,
    )
    assert result.status is OrderStatus.REJECTED
    assert "active circuit breaker: operator_global" in result.risk_reasons
    assert svc.broker.submit_calls == 0


def test_scoped_data_breaker_atomically_prevents_a_later_broker_submit(make_service):
    svc = make_service()
    order_id = _approved_order(svc)
    svc.breakers.trip(
        BreakerScope.data(AssetClass.EQUITY),
        "asset race",
        "daemon",
        request_id="order-submission-asset-race",
    )

    result = _submit(svc.order_submission, order_id)

    assert result.status is OrderStatus.REJECTED
    assert "active circuit breaker: data:equity" in result.risk_reasons
    assert svc.broker.submit_calls == 0


def test_execution_risk_rejection_has_exact_atomic_status_audit(make_service):
    service = make_service()
    order_id = _approved_order(service)
    service.breakers.trip(
        BreakerScope.data(AssetClass.EQUITY),
        "feed disagreement",
        "daemon:risk",
        request_id="execution-risk-breaker",
    )

    result = _submit(service.order_submission, order_id)

    assert result.status is OrderStatus.REJECTED
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "order.reject_execution_risk",
                AuditEvent.target_id == str(order_id),
            )
        )
    assert order.status == OrderStatus.REJECTED.value
    assert (
        audit.actor,
        audit.reason,
        audit.request_id,
        audit.result_code,
    ) == (
        "operator:test",
        "order submission test",
        f"order-submission-{order_id}",
        OrderStatus.REJECTED.value,
    )


def test_execution_risk_rejection_rolls_back_on_audit_failure(make_service):
    service = make_service()
    order_id = _approved_order(service)
    service.breakers.trip(
        BreakerScope.data(AssetClass.EQUITY),
        "feed disagreement",
        "daemon:risk",
        request_id="execution-risk-rollback-breaker",
    )
    listener = _fail_audit_action("order.reject_execution_risk")
    session_type = service.session_factory.class_
    event.listen(session_type, "before_flush", listener)
    try:
        with pytest.raises(
            RuntimeError,
            match="injected order.reject_execution_risk audit failure",
        ):
            _submit(service.order_submission, order_id)
    finally:
        event.remove(session_type, "before_flush", listener)

    with service.session_factory() as session:
        assert (
            session.get(Order, order_id).status
            == OrderStatus.APPROVAL_RECORDED.value
        )
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "order.reject_execution_risk",
                AuditEvent.target_id == str(order_id),
            )
        ) == 0


def test_approved_proposal_that_expires_before_submit_never_calls_broker(make_service):
    svc = make_service()
    order_id = _approved_order(svc)
    with svc.session_factory() as session:
        proposal = session.query(Proposal).filter_by(order_id=order_id).one()
        proposal.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    result = _submit(svc.order_submission, order_id)

    assert result.status is OrderStatus.EXPIRED
    assert svc.broker.submit_calls == 0
    with svc.session_factory() as session:
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "order.expire_approved",
                AuditEvent.target_id == str(order_id),
            )
        )
    assert (
        audit.actor,
        audit.reason,
        audit.request_id,
        audit.result_code,
    ) == (
        "operator:test",
        "order submission test",
        f"order-submission-{order_id}",
        OrderStatus.EXPIRED.value,
    )


def test_approved_expiry_rolls_back_on_audit_failure(make_service):
    service = make_service()
    order_id = _approved_order(service)
    with service.session_factory() as session:
        proposal = session.scalar(
            select(Proposal).where(Proposal.order_id == order_id)
        )
        proposal.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    listener = _fail_audit_action("order.expire_approved")
    session_type = service.session_factory.class_
    event.listen(session_type, "before_flush", listener)
    try:
        with pytest.raises(
            RuntimeError,
            match="injected order.expire_approved audit failure",
        ):
            _submit(service.order_submission, order_id)
    finally:
        event.remove(session_type, "before_flush", listener)

    with service.session_factory() as session:
        assert (
            session.get(Order, order_id).status
            == OrderStatus.APPROVAL_RECORDED.value
        )
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "order.expire_approved",
                AuditEvent.target_id == str(order_id),
            )
        ) == 0


def test_expiry_during_snapshot_prevents_submission_claim(make_service):
    class FakeNow:
        def __init__(self):
            self.current = utcnow()

        def __call__(self):
            return self.current

        def advance(self, **delta):
            self.current += timedelta(**delta)

    now = FakeNow()
    svc = make_service()
    order_id = _approved_order(svc)
    original = svc.snapshot_service.assemble_for_execution

    def slow_snapshot(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        now.advance(minutes=16)
        return snapshot

    svc.snapshot_service.assemble_for_execution = slow_snapshot
    svc.order_submission.now = now

    result = _submit(svc.order_submission, order_id)

    assert result.status is OrderStatus.EXPIRED
    assert svc.broker.submit_calls == 0
    with svc.session_factory() as session:
        assert session.get(Order, order_id).status == OrderStatus.EXPIRED.value


def test_snapshot_failure_before_claim_leaves_approval_recorded(make_service):
    svc = make_service()
    order_id = _approved_order(svc)
    svc.broker.get_positions = lambda: (_ for _ in ()).throw(ConnectionError("offline"))

    try:
        _submit(svc.order_submission, order_id)
    except RequiredDependencyUnavailable:
        pass
    else:  # pragma: no cover - makes the desired provider failure explicit
        raise AssertionError("snapshot failure should be visible to the caller")

    with svc.session_factory() as session:
        assert session.get(Order, order_id).status == OrderStatus.APPROVAL_RECORDED.value


def test_only_definitive_broker_rejection_becomes_rejected(make_service):
    class RejectingBroker(MockBroker):
        def submit_order(self, order):
            raise BrokerSubmissionRejected("insufficient_buying_power")

    broker = RejectingBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    order_id = _approved_order(svc)

    result = _submit(svc.order_submission, order_id)

    assert result.status is OrderStatus.REJECTED
    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.REJECTED.value
        assert order.last_error_code == "insufficient_buying_power"
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.request_id
                == f"order-submission-{order_id}"
            )
        ).all()
    assert {
        audit.action for audit in audits
    } >= {
        "order.submission_claim",
        "order.submission_result",
    }
    assert {
        (audit.actor, audit.reason, audit.request_id)
        for audit in audits
    } == {
        (
            "operator:test",
            "order submission test",
            f"order-submission-{order_id}",
        )
    }


def test_immediate_broker_fill_returns_unknown_until_exact_activity(
    make_service,
):
    class FilledBroker(MockBroker):
        def submit_order(self, order):
            accepted = super().submit_order(order)
            result = OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                OrderStatus.FILLED,
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("100"),
            )
            self._orders_by_key[order.idempotency_key] = result
            self._orders_by_id[accepted.broker_order_id] = result
            return result

    broker = FilledBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    order_id = _approved_order(svc)

    result = _submit(svc.order_submission, order_id)

    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value
        assert order.acceptance_state == "fill_reconcile_required"
    report = svc.reconciliation.reconcile(
        actor="test:order-submission",
        reason="order submission reconciliation",
        request_id="order-submission-reconciliation",
    )
    snapshot = svc.snapshot_service.assemble_for_execution("AAPL")

    assert report.inserted_fills == 0
    assert any(
        "requires authoritative fill activities" in item
        for item in report.broker_drift
    )
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False


@pytest.mark.parametrize(
    "terminal_status",
    [
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
    ],
)
@pytest.mark.parametrize(
    "filled_qty",
    [Decimal("0.5"), Decimal("0.000000500")],
    ids=["ordinary", "sub-micro"],
)
def test_synchronous_terminal_partial_fill_preserves_status_and_latches(
    make_service,
    terminal_status,
    filled_qty,
):
    class TerminalPartialBroker(MockBroker):
        def submit_order(self, order):
            accepted = super().submit_order(order)
            result = OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                terminal_status,
                filled_qty=filled_qty,
                avg_fill_price=Decimal("99"),
            )
            self._orders_by_key[order.idempotency_key] = result
            self._orders_by_id[accepted.broker_order_id] = result
            return result

    broker = TerminalPartialBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    order_id = _approved_order(svc)

    result = _submit(svc.order_submission, order_id)

    assert result.status is terminal_status
    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == terminal_status.value
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.broker_order_id is not None


@pytest.mark.parametrize("filled_qty", [Decimal("NaN"), Decimal("-1")])
def test_synchronous_invalid_cumulative_fill_latches_before_return(
    make_service,
    filled_qty,
):
    class InvalidCumulativeBroker(MockBroker):
        def submit_order(self, order):
            accepted = super().submit_order(order)
            result = OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                OrderStatus.SUBMITTED,
                filled_qty=filled_qty,
            )
            self._orders_by_key[order.idempotency_key] = result
            self._orders_by_id[accepted.broker_order_id] = result
            return result

    broker = InvalidCumulativeBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    order_id = _approved_order(svc)

    result = _submit(svc.order_submission, order_id)

    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert svc.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "invalid_cumulative_fill"


def test_post_send_cumulative_below_exact_local_fill_never_adopts_terminal_status(
    make_service,
):
    service = make_service()
    order_id = _approved_order(service)
    assert service.order_submission.repository.claim_submission(
        order_id,
        utcnow(),
        tuple(
            scope.key
            for scope in relevant_scopes_for_symbol("AAPL")
        ),
        actor="operator:test",
        reason="post-send exact fill reconciliation",
        request_id="post-send-exact-fill-claim",
    )
    with service.session_factory() as session:
        session.add(
            Fill(
                order_id=order_id,
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id="post-send-exact-fill",
                filled_at=utcnow(),
            )
        )
        session.commit()

    persisted_status = (
        service.order_submission.repository.record_submission_result(
            order_id,
            OrderStatus.CANCELED,
            "post-send-broker-order",
            "",
            utcnow(),
            Decimal("0"),
            actor="operator:test",
            reason="post-send exact fill reconciliation",
            request_id="post-send-exact-fill",
        )
    )

    assert persisted_status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert service.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "cumulative_fill_contradiction"


@pytest.mark.parametrize(
    (
        "delta",
        "expected_status",
        "expected_acceptance",
        "expected_error",
        "expected_drift",
    ),
    [
        (
            Decimal(0),
            OrderStatus.CANCELED,
            OrderStatus.CANCELED.value,
            "",
            False,
        ),
        (
            FILL_ECONOMIC_QUANTUM,
            OrderStatus.CANCELED,
            FILL_RECONCILIATION_REQUIRED,
            "",
            False,
        ),
        (
            -FILL_ECONOMIC_QUANTUM,
            OrderStatus.ACCEPTANCE_UNKNOWN,
            FILL_RECONCILIATION_REQUIRED,
            "cumulative_fill_contradiction",
            True,
        ),
    ],
    ids=["exact", "one-quantum-ahead", "one-quantum-behind"],
)
def test_post_send_uses_canonical_fill_quantum(
    make_service,
    delta,
    expected_status,
    expected_acceptance,
    expected_error,
    expected_drift,
):
    service = make_service()
    order_id = _approved_order(service)
    assert service.order_submission.repository.claim_submission(
        order_id,
        utcnow(),
        tuple(
            scope.key
            for scope in relevant_scopes_for_symbol("AAPL")
        ),
        actor="operator:test",
        reason="post-send canonical fill reconciliation",
        request_id="post-send-fill-quantum-claim",
    )
    authoritative_qty = Decimal("0.000000500")
    with service.session_factory() as session:
        session.add(
            Fill(
                order_id=order_id,
                ticker="AAPL",
                side="buy",
                qty=authoritative_qty,
                price=Decimal("100"),
                broker_fill_id="post-send-quantum-fill",
                filled_at=utcnow(),
            )
        )
        session.commit()

    persisted_status = (
        service.order_submission.repository.record_submission_result(
            order_id,
            OrderStatus.CANCELED,
            "post-send-quantum-broker",
            "",
            utcnow(),
            authoritative_qty + delta,
            actor="operator:test",
            reason="post-send quantum reconciliation",
            request_id="post-send-quantum",
        )
    )

    assert persisted_status is expected_status
    assert (
        service.breakers.is_tripped(BreakerScope.broker_drift())
        is expected_drift
    )
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == expected_status.value
        assert order.acceptance_state == expected_acceptance
        assert order.last_error_code == expected_error


@pytest.mark.parametrize(
    ("broker_order_id", "returned_client_id"),
    [
        (None, "expected-client-id"),
        ("", "expected-client-id"),
        ("   ", "expected-client-id"),
        ("broker-id", ""),
        ("broker-id", "different-client-id"),
    ],
)
def test_invalid_submission_identity_is_unknown_latched_and_trips_drift(
    make_service,
    broker_order_id,
    returned_client_id,
):
    class InvalidIdentityBroker(MockBroker):
        def submit_order(self, order):
            return OrderResult(
                returned_client_id,
                broker_order_id,
                OrderStatus.SUBMITTED,
            )

    broker = InvalidIdentityBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    order_id = service.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        idempotency_key="expected-client-id",
        actor="operator:test",
        reason="order identity proposal",
        request_id="order-identity-proposal",
    )["order_id"]
    service.order_application.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed identity",
            utcnow(),
            "order-submission-identity-approval",
        )
    )

    result = _submit(service.order_submission, order_id)

    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert result.broker_order_id is None
    assert service.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert order.broker_order_id is None
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "invalid_broker_identity"


@pytest.mark.parametrize(
    ("returned_ticker", "expected_status", "expected_drift"),
    [
        ("BTCUSD", OrderStatus.SUBMITTED, False),
        ("BTC/USD", OrderStatus.SUBMITTED, False),
        ("ETHUSD", OrderStatus.ACCEPTANCE_UNKNOWN, True),
        ("ETH/USD", OrderStatus.ACCEPTANCE_UNKNOWN, True),
        ("BT/CUSD", OrderStatus.ACCEPTANCE_UNKNOWN, True),
    ],
)
def test_crypto_submission_identity_accepts_equivalent_symbol_only(
    make_service,
    returned_ticker,
    expected_status,
    expected_drift,
):
    class SymbolIdentityBroker(MockBroker):
        def submit_order(self, order):
            accepted = super().submit_order(order)
            result = OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                OrderStatus.SUBMITTED,
                ticker=returned_ticker,
            )
            self._orders_by_key[order.idempotency_key] = result
            self._orders_by_id[accepted.broker_order_id] = result
            return result

    broker = SymbolIdentityBroker()
    broker.set_price("BTC/USD", Decimal("80000"))
    service = make_service(broker=broker)
    order_id = service.propose_order(
        "BTC/USD",
        "buy",
        "market",
        notional="100",
        idempotency_key=f"symbol-identity-{returned_ticker}",
        actor="operator:test",
        reason="symbol identity proposal",
        request_id="symbol-identity-proposal",
    )["order_id"]
    service.order_application.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed crypto symbol identity",
            utcnow(),
            "order-submission-crypto-identity-approval",
        )
    )

    result = _submit(service.order_submission, order_id)

    assert result.status is expected_status
    assert (
        service.breakers.is_tripped(BreakerScope.broker_drift())
        is expected_drift
    )
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == expected_status.value
        assert (
            order.acceptance_state == FILL_RECONCILIATION_REQUIRED
        ) is expected_drift


def test_submission_never_treats_local_equity_as_broker_slash_crypto(
    app_config,
    session_factory,
):
    class ReversedIdentityBroker(MockBroker):
        def submit_order(self, order):
            accepted = super().submit_order(order)
            return OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                OrderStatus.SUBMITTED,
                ticker="ACME/USD",
            )

    config = app_config.model_copy(
        update={
            "risk": app_config.risk.model_copy(
                update={"ticker_allowlist": ["ACMEUSD"]}
            )
        }
    )
    broker = ReversedIdentityBroker()
    broker.set_price("ACMEUSD", Decimal("100"))
    service = TradingService(
        broker,
        session_factory,
        config,
        FakeClock(is_open=True),
    )
    order_id = service.propose_order(
        "ACMEUSD",
        "buy",
        "market",
        notional="100",
        idempotency_key="equity-direction-submission",
        actor="operator:test",
        reason="directional identity proposal",
        request_id="directional-identity-proposal",
    )["order_id"]
    service.order_application.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed directional identity",
            utcnow(),
            "order-submission-directional-identity-approval",
        )
    )

    result = _submit(service.order_submission, order_id)

    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert service.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with service.session_factory() as session:
        order_row = session.get(Order, order_id)
        assert order_row.acceptance_state == FILL_RECONCILIATION_REQUIRED
        assert order_row.last_error_code == "invalid_broker_identity"


def test_malformed_position_payload_fails_before_submission_claim(make_service):
    service = make_service()
    order_id = _approved_order(service)
    service.broker.get_positions = lambda: (_ for _ in ()).throw(
        ValueError("invalid Alpaca position quantity")
    )

    with pytest.raises(RequiredDependencyUnavailable):
        _submit(service.order_submission, order_id)

    assert service.broker.submit_calls == 0
    with service.session_factory() as session:
        assert (
            session.get(Order, order_id).status
            == OrderStatus.APPROVAL_RECORDED.value
        )


def test_submission_service_is_public_contract():
    assert OrderSubmissionService.__name__ == "OrderSubmissionService"
