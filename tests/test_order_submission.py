"""Durable outbox submission behavior independent of the public HTTP shape."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from threading import Event, Thread

from trading_assistant.broker.base import BrokerSubmissionRejected
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import OrderResult, OrderStatus
from trading_assistant.assets import AssetClass
from trading_assistant.db.models import Order, Proposal, utcnow
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.orders.submission import OrderSubmissionService
from trading_assistant.risk.breakers import BreakerScope


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
    claim_reached = Event()
    release_claim = Event()
    original_claim = svc.order_application.repository.claim_submission
    outcome = {}

    def delayed_claim(*args, **kwargs):
        claim_reached.set()
        assert release_claim.wait(timeout=5)
        return original_claim(*args, **kwargs)

    svc.order_application.repository.claim_submission = delayed_claim

    def submit():
        try:
            outcome["result"] = svc.order_submission.submit(order_id)
        except BaseException as exc:  # surfaced in the test thread below
            outcome["error"] = exc

    thread = Thread(target=submit)
    thread.start()
    assert claim_reached.wait(timeout=5)
    try:
        panic = svc.reconciliation.panic("operator:avi", "submission race")
        assert panic.safe is True
    finally:
        release_claim.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["result"].status is OrderStatus.APPROVAL_RECORDED
    assert svc.broker.submit_calls == 0


def test_scoped_data_breaker_atomically_prevents_a_later_broker_submit(make_service):
    svc = make_service()
    order_id = _approved_order(svc)
    claim_reached = Event()
    release_claim = Event()
    original_claim = svc.order_application.repository.claim_submission
    outcome = {}

    def delayed_claim(*args, **kwargs):
        claim_reached.set()
        assert release_claim.wait(timeout=5)
        return original_claim(*args, **kwargs)

    svc.order_application.repository.claim_submission = delayed_claim

    def submit():
        try:
            outcome["result"] = svc.order_submission.submit(order_id)
        except BaseException as exc:
            outcome["error"] = exc

    thread = Thread(target=submit)
    thread.start()
    assert claim_reached.wait(timeout=5)
    svc.breakers.trip(
        BreakerScope.data(AssetClass.EQUITY),
        "asset race",
        "daemon",
    )
    release_claim.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["result"].status is OrderStatus.APPROVAL_RECORDED
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
        assert session.get(Order, order_id).status == OrderStatus.FILLED.value


def test_submission_service_is_public_contract():
    assert OrderSubmissionService.__name__ == "OrderSubmissionService"
