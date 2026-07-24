"""Broker-truth reconciliation and truthful panic reporting."""

from __future__ import annotations

from decimal import Decimal

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    BrokerFill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trading_assistant.db import models as db_models
from trading_assistant.db.models import Fill, KillSwitchState, Order, utcnow
from trading_assistant.orders.application import ApprovalCommand


def _approved_order_id(service) -> int:
    order_id = service.propose_order(
        "AAPL", "buy", "market", notional="100"
    )["order_id"]
    service.order_application.approve(
        ApprovalCommand(order_id, "operator:avi", "reviewed", utcnow())
    )
    return order_id


def _submitted_order_id(service) -> int:
    order_id = service.propose_order(
        "AAPL", "buy", "market", notional="100"
    )["order_id"]
    service.approve_order(
        order_id, actor="operator:avi", reason="reviewed for submission"
    )
    return order_id


class AcceptThenDisconnectBroker(MockBroker):
    def submit_order(self, order):
        super().submit_order(order)
        raise ConnectionError("response lost after acceptance")


class CancelFailsBroker(MockBroker):
    def cancel_order(self, order_id):
        raise ConnectionError("broker unavailable")

    def get_order_status(self, order_id):
        raise ConnectionError("broker unavailable")


def test_reconcile_unknown_finds_remote_acceptance(make_service):
    broker = AcceptThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    order_id = _approved_order_id(service)
    service.order_submission.submit(order_id)

    report = service.reconciliation.reconcile()

    assert report.resolved_unknown == 1
    with service.session_factory() as session:
        row = session.get(Order, order_id)
        assert row.status == "submitted"
        assert row.broker_order_id is not None


def test_panic_reports_unconfirmed_cancel_as_not_safe(make_service):
    broker = CancelFailsBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)

    report = service.reconciliation.panic("operator:avi", "manual drill")

    assert report.safe is False
    assert report.unconfirmed_order_ids == (order_id,)
    assert report.message != "everything halted"


def test_panic_requires_actor_and_reason_before_latching(make_service):
    service = make_service()

    with pytest.raises(ValueError, match="actor and reason"):
        service.reconciliation.panic("", "manual drill")
    with pytest.raises(ValueError, match="actor and reason"):
        service.reconciliation.panic("operator:avi", " ")

    with service.session_factory() as session:
        assert session.scalar(
            select(KillSwitchState).where(
                KillSwitchState.asset_class == "operator_global"
            )
        ) is None


def test_panic_cancels_remote_only_open_order_by_explicit_id(make_service):
    broker = MockBroker()
    remote = broker.submit_order(
        OrderRequest(
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            idempotency_key="remote-only",
            notional=Decimal("100"),
        )
    )
    service = make_service(broker=broker)

    report = service.reconciliation.panic("operator:avi", "remote cleanup")

    assert report.safe is True
    assert report.confirmed_canceled == (remote.broker_order_id,)
    assert report.unconfirmed_order_ids == ()
    assert report.remote_open_order_ids == ()


def test_panic_latches_global_breaker_before_broker_enumeration_failure(make_service):
    class EnumerationFailsBroker(MockBroker):
        def get_open_orders(self):
            raise ConnectionError("broker unavailable")

    service = make_service(broker=EnumerationFailsBroker())

    report = service.reconciliation.panic("operator:avi", "connectivity loss")

    assert report.safe is False
    with service.session_factory() as session:
        state = session.scalar(
            select(KillSwitchState).where(
                KillSwitchState.asset_class == "operator_global"
            )
        )
        assert state is not None and state.tripped is True

    blocked = service.propose_order("AAPL", "buy", "market", notional="100")
    assert blocked["status"] == "rejected"
    assert any("kill switch" in reason for reason in blocked["risk_reasons"])


def test_panic_preserves_unverified_remote_only_id_as_potentially_open(make_service):
    class VanishingRemoteBroker(MockBroker):
        def __init__(self):
            super().__init__()
            self.enumerations = 0

        def get_open_orders(self):
            self.enumerations += 1
            if self.enumerations == 1:
                return super().get_open_orders()
            return []

        def cancel_order(self, order_id):
            raise ConnectionError("cancel response unavailable")

        def get_order_status(self, order_id):
            raise ConnectionError("status unavailable")

    broker = VanishingRemoteBroker()
    remote = broker.submit_order(
        OrderRequest(
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            idempotency_key="remote-unverified",
            notional=Decimal("100"),
        )
    )
    service = make_service(broker=broker)

    report = service.reconciliation.panic("operator:avi", "remote verification")

    assert report.safe is False
    assert report.remote_open_order_ids == (remote.broker_order_id,)


def test_panic_is_unsafe_for_remote_open_order_without_explicit_id(make_service):
    class MissingIdBroker(MockBroker):
        def get_open_orders(self):
            return [
                OrderResult(
                    "missing-id",
                    None,
                    OrderStatus.SUBMITTED,
                )
            ]

        def cancel_order(self, order_id):
            raise AssertionError("must not cancel without an explicit broker id")

    service = make_service(broker=MissingIdBroker())

    report = service.reconciliation.panic("operator:avi", "missing broker id")

    assert report.safe is False
    assert "unaddressable_remote_open=true" in report.message


def test_panic_never_guesses_a_cancel_id_for_unresolved_unknown(make_service):
    class RecordingBroker(MockBroker):
        def __init__(self):
            super().__init__()
            self.cancel_calls = []

        def cancel_order(self, order_id):
            self.cancel_calls.append(order_id)
            return super().cancel_order(order_id)

    broker = RecordingBroker()
    service = make_service(broker=broker)
    with service.session_factory() as session:
        unknown = Order(
            idempotency_key="unresolved-client-id",
            ticker="AAPL",
            side="buy",
            order_type="market",
            notional=Decimal("100"),
            status=OrderStatus.ACCEPTANCE_UNKNOWN.value,
        )
        session.add(unknown)
        session.commit()
        unknown_id = unknown.id

    report = service.reconciliation.panic("operator:avi", "unknown acceptance")

    assert broker.cancel_calls == []
    assert report.safe is False
    assert report.unconfirmed_order_ids == (unknown_id,)


def test_reconcile_syncs_terminal_broker_status(make_service):
    broker = MockBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        local = session.get(Order, order_id)
        broker_order_id = local.broker_order_id
    broker.cancel_order(broker_order_id)

    report = service.reconciliation.reconcile()

    assert report.synced_orders == 1
    with service.session_factory() as session:
        assert session.get(Order, order_id).status == OrderStatus.CANCELED.value


def test_reconcile_reports_remote_open_order_without_broker_id(make_service):
    class MissingIdBroker(MockBroker):
        def get_open_orders(self):
            return [OrderResult("missing-id", None, OrderStatus.SUBMITTED)]

    report = make_service(broker=MissingIdBroker()).reconciliation.reconcile()

    assert any("missing broker order id" in drift for drift in report.broker_drift)


class ActivityBroker(MockBroker):
    def __init__(self):
        super().__init__()
        self.activities: list[BrokerFill] = []
        self.activity_calls: list[datetime | None] = []
        self.fail_activities = False

    def get_fill_activities(self, after=None):
        self.activity_calls.append(after)
        if self.fail_activities:
            raise ConnectionError("activity stream unavailable")
        return list(self.activities)


def test_fill_reconciliation_is_incremental_and_restart_idempotent(make_service):
    assert hasattr(db_models, "ReconciliationCursor")
    cursor_type = db_models.ReconciliationCursor
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key

    filled_at = datetime(2026, 7, 24, 17, 0, tzinfo=timezone.utc)
    broker.activities = [
        BrokerFill(
            broker_fill_id="activity-1",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("1"),
            price=Decimal("100"),
            filled_at=filled_at,
        )
    ]
    filled = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = filled
    broker._orders_by_key[client_order_id] = filled

    first = service.reconciliation.reconcile()
    with service.session_factory() as session:
        first_cursor_version = session.scalar(
            select(cursor_type.version).where(cursor_type.stream == "fills")
        )
    restarted = make_service(broker=broker)
    second = restarted.reconciliation.reconcile()

    assert first.inserted_fills == 1
    assert second.inserted_fills == 0
    assert broker.activity_calls == [None, filled_at]
    with service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Fill)) == 1
        cursor = session.scalar(
            select(cursor_type).where(cursor_type.stream == "fills")
        )
        assert cursor.broker == "mock"
        assert cursor.last_activity_id == "activity-1"
        assert cursor.last_activity_at == filled_at
        assert cursor.version == first_cursor_version


def test_fill_activity_network_failure_leaves_cursor_unchanged(make_service):
    assert hasattr(db_models, "ReconciliationCursor")
    cursor_type = db_models.ReconciliationCursor
    broker = ActivityBroker()
    service = make_service(broker=broker)
    order_id = _submitted_order_id(service)
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        broker_order_id = order.broker_order_id

    filled_at = datetime(2026, 7, 24, 17, 0, tzinfo=timezone.utc)
    broker.activities = [
        BrokerFill(
            broker_fill_id="activity-1",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("0.5"),
            price=Decimal("100"),
            filled_at=filled_at,
        )
    ]
    service.reconciliation.reconcile()
    with service.session_factory() as session:
        before = session.scalar(select(cursor_type).where(cursor_type.stream == "fills"))
        before_state = (
            before.last_activity_id,
            before.last_activity_at,
            before.version,
        )

    broker.fail_activities = True
    report = service.reconciliation.reconcile()

    assert any("fill activities" in drift for drift in report.broker_drift)
    with service.session_factory() as session:
        after = session.scalar(select(cursor_type).where(cursor_type.stream == "fills"))
        assert (
            after.last_activity_id,
            after.last_activity_at,
            after.version,
        ) == before_state


def test_service_compatibility_methods_serialize_reports(make_service):
    service = make_service()

    sync = service.sync_open_orders()
    panic = service.panic(actor="operator:avi", reason="serialization drill")

    assert sync["resolved_unknown"] == 0
    assert sync["unresolved_unknown"] == []
    assert sync["synced_orders"] == 0
    assert sync["inserted_fills"] == 0
    assert sync["broker_drift"] == []
    assert panic["safe"] is True
    assert panic["confirmed_canceled"] == []
    assert panic["unconfirmed_order_ids"] == []
    assert panic["remote_open_order_ids"] == []
