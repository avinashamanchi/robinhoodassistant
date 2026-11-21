"""Independent broker GETs can straddle the same partial execution."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.test_reconciliation_service import (
    ActivityBroker,
    _mutation,
    _submitted_order_id,
)
from trading_assistant.broker.models import BrokerFill, OrderStatus
from trading_assistant.db.lifecycle_proofs import lifecycle_proof_matches
from trading_assistant.db.models import (
    AuditEvent,
    Fill,
    Order,
    ReconciliationCursor,
    utcnow,
)
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.security.sensitive_fields import sensitive_store


class BetweenReadsBroker(ActivityBroker):
    """The status GET observes an execution after the first activities GET."""

    def __init__(self):
        super().__init__()
        self.pending = None
        self.later_activities = []
        self.reads = []
        self.refresh_error = False

    def get_order_status(self, order_id):
        remote = super().get_order_status(order_id)
        if self.pending is not None:
            self.reads.append("status")
            remote = replace(
                remote,
                status=OrderStatus.PARTIALLY_FILLED,
                filled_qty=self.pending,
                avg_fill_price=Decimal(150),
            )
            self._orders_by_id[order_id] = remote
            self._orders_by_key[remote.idempotency_key] = remote
            self.activities = self.later_activities
            self.pending = None
        return remote

    def get_fill_activities(self, after=None):
        self.reads.append("activities")
        if self.refresh_error and self.reads.count("activities") == 2:
            raise ConnectionError("stream unavailable")
        # The production reader includes one second of overlap.
        return [
            fill
            for fill in super().get_fill_activities(after)
            if after is None or fill.filled_at >= after - timedelta(seconds=1)
        ]


@pytest.fixture
def crossing(make_service):
    broker = BetweenReadsBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        broker_id = session.get(Order, order_id).broker_order_id
    now = utcnow()
    first = BrokerFill(
        broker_fill_id="first-visible",
        broker_order_id=broker_id,
        ticker="AAPL",
        side="buy",
        qty=Decimal(".3"),
        price=Decimal(95),
        filled_at=now + timedelta(seconds=2),
    )
    # The later-published execution's original time precedes the new cursor.
    late = replace(
        first,
        broker_fill_id="late-visible",
        qty=Decimal(".2"),
        price=Decimal(97),
        filled_at=now,
    )
    broker.activities = [first]
    broker.later_activities = [first, late]
    broker.pending = Decimal(".5")
    return broker, service, order_id, first, late


def _sync(service):
    context = _mutation("crossed fill reads")
    result = service.sync_open_orders(**context)
    with service.session_factory() as session:
        audit = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "orders.sync",
                AuditEvent.request_id == context["request_id"],
            )
        ).one()
        detail = json.loads(sensitive_store(session).read(audit, "detail_json"))
        expected = "reconciliation_required" if result["failed"] else "reconciled"
        assert audit.result_code == detail["campaign_observation"]["result"] == expected
        assert detail["broker_drift"] == result["broker_drift"]
        assert detail["inserted_fills"] == result["inserted_fills"]
    return result


def test_late_partial_activity_converges_with_original_window_and_exact_prices(
    crossing, make_service
):
    broker, service, order_id, first, late = crossing
    result = _sync(service)
    assert result["broker_drift"] == []
    assert result["inserted_fills"] == 2
    assert result["synced_orders"] == 1
    assert broker.reads == ["activities", "status", "activities"]
    assert not service.breakers.is_tripped(BreakerScope.broker_drift())
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert (order.status, order.acceptance_state) == (
            "partially_filled",
            "accepted",
        )
        fills = session.scalars(select(Fill).order_by(Fill.id)).all()
        assert [(f.broker_fill_id, f.qty, f.price, f.filled_at) for f in fills] == [
            (first.broker_fill_id, first.qty, first.price, first.filled_at),
            (late.broker_fill_id, late.qty, late.price, late.filled_at),
        ]
        assert (
            session.scalars(select(ReconciliationCursor)).one().last_activity_at
            == first.filled_at
        )
        assert lifecycle_proof_matches(
            session, service.session_factory, target_type="order", target_id=order_id
        )
    replay = make_service(broker=broker).reconciliation.reconcile(
        **_mutation("restart replay")
    )
    assert (replay.broker_drift, replay.inserted_fills) == ((), 0)


@pytest.mark.parametrize(
    ("fault", "detail"),
    [
        ("missing", "authoritative quantity"),
        ("unavailable", "authoritative"),
        ("mutated", "replay changed price"),
        ("invalid", "invalid fill activity"),
        ("overtook", "below authoritative"),
    ],
)
def test_fresh_activity_fault_keeps_order_blocked(
    crossing, make_service, fault, detail
):
    broker, service, order_id, first, late = crossing
    if fault == "missing":
        broker.later_activities = [first]
    elif fault == "unavailable":
        broker.refresh_error = True
    elif fault == "mutated":
        broker.later_activities = [replace(first, price=Decimal(123)), late]
    elif fault == "invalid":
        broker.later_activities = [first, replace(late, side="sell")]
    elif fault == "overtook":
        broker.later_activities = [first, replace(late, qty=Decimal(".3"))]
    result = _sync(service)
    assert any(detail in item for item in result["broker_drift"])
    assert broker.reads == ["activities", "status", "activities"]
    restarted = make_service(broker=broker)
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert restarted.breakers.is_tripped(BreakerScope.broker_drift())
    assert not snapshot.broker_reconciled
    assert not snapshot.daily_pnl_complete
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.acceptance_state == "fill_reconcile_required"
        assert lifecycle_proof_matches(
            session, service.session_factory, target_type="order", target_id=order_id
        )
        recorded = session.scalars(
            select(Fill).where(Fill.broker_fill_id == first.broker_fill_id)
        ).one()
        assert recorded.price == first.price


def test_full_refresh_page_is_incomplete_even_through_tenure_wrapper():
    from tests.test_alpaca_broker import FakeData, FakeTrading
    from trading_assistant.broker.alpaca import AlpacaBroker
    from trading_assistant.ops.tenure import TenureGuardedBroker

    trading = FakeTrading()
    calls = []

    def full_page(path, params):
        calls.append(path)
        assert len(calls) == 1
        return [
            {
                "id": str(i),
                "transaction_time": utcnow().isoformat(),
                "price": "100",
                "qty": "1",
                "side": "buy",
                "symbol": "AAPL",
                "order_id": "1",
            }
            for i in range(100)
        ]

    trading.get = full_page
    broker = TenureGuardedBroker(AlpacaBroker(trading, FakeData({})), None)
    with pytest.raises(ValueError, match="page"):
        broker.refresh_fill_activities(after=utcnow())
    assert calls == ["/account/activities/FILL"]


def test_refresh_reply_after_budget_cannot_prove_convergence(crossing, monkeypatch):
    from trading_assistant.orders import reconciliation

    broker, service, order_id, _, _ = crossing
    monkeypatch.setattr(
        reconciliation,
        "monotonic",
        lambda: 30.0 if broker.reads.count("activities") == 2 else 0.0,
        raising=False,
    )
    result = _sync(service)
    assert broker.reads == ["activities", "status", "activities"]
    assert result["failed"] > 0
    assert service.breakers.is_tripped(BreakerScope.broker_drift())
    with service.session_factory() as session:
        assert (
            session.get(Order, order_id).acceptance_state == "fill_reconcile_required"
        )


def test_late_execution_after_restart_recovers_ledger_but_preserves_breaker(
    crossing, make_service
):
    broker, service, order_id, first, late = crossing
    broker.later_activities = [first]
    assert _sync(service)["failed"] > 0
    breaker = service.breakers.get(BreakerScope.broker_drift())
    broker.activities = [first, late]
    restarted = make_service(broker=broker)
    report = restarted.reconciliation.reconcile(
        **_mutation("late execution on restart")
    )
    assert (report.broker_drift, report.inserted_fills) == ((), 1)
    assert restarted.breakers.get(BreakerScope.broker_drift()) == breaker
    assert not restarted.snapshot_service.assemble_for_execution(
        "AAPL"
    ).broker_reconciled
    with service.session_factory() as session:
        assert session.get(Order, order_id).acceptance_state == "accepted"


def test_sdk_rate_limit_cannot_retry_supplemental_read():
    from alpaca.common.exceptions import RetryException
    from alpaca.trading.client import TradingClient

    from tests.test_alpaca_broker import FakeData
    from trading_assistant.broker.alpaca import AlpacaBroker

    class RateLimitedTrading(TradingClient):
        def __init__(self):
            self._retry = 3
            self._retry_wait = 0
            self._base_url = "https://example.invalid"
            self._api_version = "v2"
            self.attempts = []

        def _get_default_headers(self):
            return {}

        def _one_request(self, method, url, opts, retry):
            self.attempts.append(method)
            if retry:
                raise RetryException()
            raise ConnectionError("rate limited")

    trading = RateLimitedTrading()
    with pytest.raises(ConnectionError):
        AlpacaBroker(trading, FakeData({})).refresh_fill_activities(after=utcnow())
    assert trading.attempts == ["GET"]
    assert trading._retry == 3


def test_fresh_activity_on_previously_terminal_order_requires_its_own_status(crossing):
    from tests.test_reconciliation_service import _persist_order

    broker, service, _, _, late = crossing
    with service.session_factory.begin() as session:
        other = _persist_order(
            session,
            Order(
                idempotency_key="settled-client",
                broker_order_id="settled-broker",
                ticker="AAPL",
                side="buy",
                order_type="market",
                qty=Decimal(1),
                status="canceled",
                acceptance_state="accepted",
            ),
        )
        session.flush()
        other_id = other.id
    broker.later_activities.append(
        replace(late, broker_order_id="settled-broker", broker_fill_id="other-fill")
    )

    result = _sync(service)

    assert any("requires a broker status" in item for item in result["broker_drift"])
    assert service.breakers.is_tripped(BreakerScope.broker_drift())
    with service.session_factory() as session:
        other = session.get(Order, other_id)
        assert other.acceptance_state == "fill_reconcile_required"
        assert lifecycle_proof_matches(
            session, service.session_factory, target_type="order", target_id=other_id
        )


def test_initial_cumulative_below_exact_ledger_is_immediately_contradictory(crossing):
    broker, service, _, _, _ = crossing
    broker.pending = Decimal(".2")

    result = _sync(service)

    assert any("below authoritative" in item for item in result["broker_drift"])
    assert broker.reads == ["activities", "status"]
    assert service.breakers.is_tripped(BreakerScope.broker_drift())


def test_interrupted_refresh_leaves_durable_latch_before_restart(
    crossing, make_service, monkeypatch
):
    broker, service, order_id, _, _ = crossing

    def interrupt_refresh(*, after):
        raise SystemExit("interrupted at the broker read")

    with monkeypatch.context() as patch:
        patch.setattr(
            broker, "refresh_fill_activities", interrupt_refresh, raising=False
        )
        with pytest.raises(SystemExit, match="interrupted"):
            _sync(service)

    restarted = make_service(broker=broker)
    snapshot = restarted.snapshot_service.assemble_for_execution("AAPL")
    assert not snapshot.broker_reconciled
    assert not snapshot.daily_pnl_complete
    with service.session_factory() as session:
        assert (
            session.get(Order, order_id).acceptance_state == "fill_reconcile_required"
        )
        assert (
            session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "orders.sync",
                    AuditEvent.request_id
                    == _mutation("crossed fill reads")["request_id"],
                )
            )
            is None
        )
    recovered = restarted.reconciliation.reconcile(
        **_mutation("interrupted refresh recovery")
    )
    assert (recovered.broker_drift, recovered.inserted_fills) == ((), 1)


def test_refresh_from_generic_exact_reader_respects_page_budget(crossing):
    broker, service, order_id, first, late = crossing
    broker.later_activities = [first] * 99 + [late]
    result = _sync(service)
    assert result["failed"] > 0
    assert result["inserted_fills"] == 1
    assert service.breakers.is_tripped(BreakerScope.broker_drift())
    with service.session_factory() as session:
        assert (
            session.get(Order, order_id).acceptance_state == "fill_reconcile_required"
        )
