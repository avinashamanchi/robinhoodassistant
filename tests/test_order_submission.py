"""Durable outbox submission behavior independent of the public HTTP shape."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_assistant.broker.base import BrokerSubmissionRejected
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import OrderResult, OrderStatus
from trading_assistant.assets import AssetClass
from trading_assistant.db.models import Fill, Order, Proposal, utcnow
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.orders.submission import OrderSubmissionService
from trading_assistant.risk.breakers import (
    BreakerScope,
    relevant_scopes_for_symbol,
)


def _approved_order(svc) -> int:
    order_id = svc.propose_order("AAPL", "buy", "market", notional="100")["order_id"]
    svc.order_application.approve(
        ApprovalCommand(order_id, "operator:avi", "reviewed", utcnow())
    )
    return order_id


class AcceptThenDisconnectBroker(MockBroker):
    def submit_order(self, order):
        super().submit_order(order)
        raise ConnectionError("response lost after acceptance")


def test_accept_then_disconnect_becomes_unknown_without_duplicate(make_service):
    broker = AcceptThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    order_id = _approved_order(svc)

    result = svc.order_submission.submit(order_id)

    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert len(broker._orders_by_key) == 1
    result2 = svc.order_submission.submit(order_id)
    assert result2.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert len(broker._orders_by_key) == 1


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
    svc.order_submission.submit(order_id)

    assert checked["status"] == OrderStatus.SUBMITTING.value


def test_panic_latch_atomically_prevents_a_later_broker_submit(make_service):
    svc = make_service()
    order_id = _approved_order(svc)
    panic = svc.reconciliation.panic("operator:avi", "submission race")

    result = svc.order_submission.submit(order_id)

    assert panic.safe is True
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
    )

    result = svc.order_submission.submit(order_id)

    assert result.status is OrderStatus.REJECTED
    assert "active circuit breaker: data:equity" in result.risk_reasons
    assert svc.broker.submit_calls == 0


def test_approved_proposal_that_expires_before_submit_never_calls_broker(make_service):
    svc = make_service()
    order_id = _approved_order(svc)
    with svc.session_factory() as session:
        proposal = session.query(Proposal).filter_by(order_id=order_id).one()
        proposal.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    result = svc.order_submission.submit(order_id)

    assert result.status is OrderStatus.EXPIRED
    assert svc.broker.submit_calls == 0


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

    result = svc.order_submission.submit(order_id)

    assert result.status is OrderStatus.EXPIRED
    assert svc.broker.submit_calls == 0
    with svc.session_factory() as session:
        assert session.get(Order, order_id).status == OrderStatus.EXPIRED.value


def test_snapshot_failure_before_claim_leaves_approval_recorded(make_service):
    svc = make_service()
    order_id = _approved_order(svc)
    svc.broker.get_positions = lambda: (_ for _ in ()).throw(ConnectionError("offline"))

    try:
        svc.order_submission.submit(order_id)
    except ConnectionError:
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

    result = svc.order_submission.submit(order_id)

    assert result.status is OrderStatus.REJECTED
    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.REJECTED.value
        assert order.last_error_code == "insufficient_buying_power"


def test_immediate_broker_fill_persists_truthfully(make_service):
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

    result = svc.order_submission.submit(order_id)

    assert result.status is OrderStatus.FILLED
    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value
        assert order.acceptance_state == "fill_reconcile_required"
    report = svc.reconciliation.reconcile()
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
def test_synchronous_terminal_partial_fill_preserves_status_and_latches(
    make_service,
    terminal_status,
):
    class TerminalPartialBroker(MockBroker):
        def submit_order(self, order):
            accepted = super().submit_order(order)
            result = OrderResult(
                accepted.idempotency_key,
                accepted.broker_order_id,
                terminal_status,
                filled_qty=Decimal("0.5"),
                avg_fill_price=Decimal("99"),
            )
            self._orders_by_key[order.idempotency_key] = result
            self._orders_by_id[accepted.broker_order_id] = result
            return result

    broker = TerminalPartialBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    order_id = _approved_order(svc)

    result = svc.order_submission.submit(order_id)

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

    result = svc.order_submission.submit(order_id)

    assert result.status is OrderStatus.SUBMITTED
    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTED.value
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
    )["order_id"]
    service.order_application.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed identity",
            utcnow(),
        )
    )

    result = service.order_submission.submit(order_id)

    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert result.broker_order_id is None
    assert service.breakers.is_tripped(BreakerScope.broker_drift()) is True
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert order.broker_order_id is None
        assert order.acceptance_state == "fill_reconcile_required"
        assert order.last_error_code == "invalid_broker_identity"


def test_malformed_position_payload_fails_before_submission_claim(make_service):
    service = make_service()
    order_id = _approved_order(service)
    service.broker.get_positions = lambda: (_ for _ in ()).throw(
        ValueError("invalid Alpaca position quantity")
    )

    with pytest.raises(ValueError, match="invalid Alpaca position"):
        service.order_submission.submit(order_id)

    assert service.broker.submit_calls == 0
    with service.session_factory() as session:
        assert (
            session.get(Order, order_id).status
            == OrderStatus.APPROVAL_RECORDED.value
        )


def test_submission_service_is_public_contract():
    assert OrderSubmissionService.__name__ == "OrderSubmissionService"
