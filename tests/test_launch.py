"""Launch features: health/heartbeat (D3), preflight helpers (B3), and a
full order lifecycle integration (B2)."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from trading_assistant.app.main import create_app
from trading_assistant.config import Secrets
from trading_assistant.db.models import Fill, Order


class _StubAgent:
    def chat(self, message, **context):
        return {"reply": "ok", "tool_calls": []}


def _approve(svc, order_id):
    return svc.approve_order(
        order_id,
        actor="operator:test",
        reason="launch test",
        request_id="launch-test-approval",
    )


def _propose(svc, **kwargs):
    return svc.propose_order(
        **kwargs,
        actor="operator:test",
        reason="launch test proposal",
        request_id="launch-test-proposal",
    )


def _sync(svc):
    return svc.sync_open_orders(
        actor="operator:test",
        reason="launch test broker reconciliation",
        request_id="launch-test-sync",
    )


# ── D3 health + heartbeat ───────────────────────────────────────
def test_health_reflects_heartbeat(make_service):
    svc = make_service()
    assert svc.health()["daemon_alive"] is False        # no heartbeat yet
    svc.write_heartbeat("daemon")
    h = svc.health()
    assert h["db_ok"] is True and h["daemon_alive"] is True
    assert h["heartbeat_age_seconds"] < 5


def test_only_liveness_endpoint_is_anonymous(make_service):
    app = create_app(service=make_service(), agent=_StubAgent(), api_token="tok", planning=None)
    client = TestClient(app)

    assert client.get("/health/live").status_code == 200
    assert client.get("/health").status_code == 401


# ── B3 preflight helpers (keyless) ──────────────────────────────
def test_preflight_config_and_live_checks(app_config):
    from trading_assistant import preflight

    assert preflight._config_parses().status == "PASS"
    assert preflight._live_off(app_config, Secrets()).status == "PASS"


def test_preflight_env_flags_missing_token():
    from trading_assistant import preflight

    r = preflight._env_present(Secrets(app_api_token="short"))
    assert r.status == "FAIL" and "APP_API_TOKEN" in r.detail


def test_preflight_reconciliation_reports_position_drift(make_service):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import Position
    from trading_assistant import preflight

    broker = MockBroker(
        positions=[
            Position("AAPL", Decimal("2"), Decimal("100"), Decimal("100"))
        ]
    )
    result = preflight._reconciliation(make_service(broker=broker))

    assert result.status == "FAIL"
    assert "AAPL" in result.detail


def test_preflight_reconciliation_sanitizes_provider_exception_text():
    from trading_assistant import preflight

    class ExplodingService:
        def sync_open_orders(self, **context):
            raise RuntimeError("provider-secret-preflight-detail")

    result = preflight._reconciliation(ExplodingService())

    assert result.status == "FAIL"
    assert result.detail == "RuntimeError"
    assert "provider-secret-preflight-detail" not in result.detail


# ── B2 full order lifecycle ─────────────────────────────────────
def test_order_lifecycle_propose_approve_fill(make_service):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import (
        BrokerFill,
        OrderResult,
        OrderStatus,
    )
    from trading_assistant.db.models import utcnow

    class LifecycleBroker(MockBroker):
        activities = []

        def get_fill_activities(self, after=None):
            return list(self.activities)

    broker = LifecycleBroker()
    svc = make_service(broker=broker)  # AAPL @ 100
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        notional="400",
    )["order_id"]
    assert svc.get_order_status(oid)["status"] == "proposed"

    approve = _approve(svc, oid)
    assert approve["executed"] is True and approve["status"] == "submitted"

    with svc.session_factory() as session:
        order = session.get(Order, oid)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
    remote = OrderResult(
        client_order_id,
        broker_order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("4"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_order_id] = remote
    broker._orders_by_key[client_order_id] = remote
    broker.activities = [
        BrokerFill(
            broker_fill_id="lifecycle-fill-1",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("4"),
            price=Decimal("100"),
            filled_at=utcnow(),
        )
    ]

    sync = _sync(svc)
    assert sync["newly_filled"] == 1

    with svc.session_factory() as s:
        assert s.execute(select(func.count()).select_from(Fill)).scalar_one() == 1
        assert s.get(Order, oid).status == "filled"
    # The execution shows up in the log feed the UI reads.
    assert "risk_events" in svc.get_log()


# ── fill/status sync from broker (Alpaca reconciliation) ────────
def test_sync_ingests_fills_and_advances_status(make_service):
    from decimal import Decimal

    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import (
        BrokerFill,
        OrderResult,
        OrderStatus,
    )
    from trading_assistant.db.models import Fill, Order, utcnow

    class FillableBroker(MockBroker):
        fill = None
        activities = []

        def get_order_status(self, oid):
            r = super().get_order_status(oid)
            if self.fill:
                return OrderResult(r.idempotency_key, oid, OrderStatus.FILLED,
                                   filled_qty=self.fill[0], avg_fill_price=self.fill[1])
            return r

        def get_fill_activities(self, after=None):
            return list(self.activities)

    broker = FillableBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        notional="400",
    )["order_id"]
    _approve(svc, oid)                          # -> SUBMITTED with broker_order_id

    broker.fill = (Decimal("4"), Decimal("100"))
    with svc.session_factory() as session:
        broker_order_id = session.get(Order, oid).broker_order_id
    broker.activities = [
        BrokerFill(
            broker_fill_id="launch-fill-1",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("4"),
            price=Decimal("100"),
            filled_at=utcnow(),
        )
    ]
    r = _sync(svc)
    assert r["newly_filled"] == 1
    with svc.session_factory() as s:
        assert s.get(Order, oid).status == "filled"
        assert s.execute(select(func.count()).select_from(Fill)).scalar_one() == 1
    # Idempotent — nothing left open to sync, no duplicate fill.
    assert _sync(svc)["synced"] == 0


def test_sync_reports_broker_status_failures(make_service):
    from trading_assistant.broker.mock import MockBroker

    class StatusFailureBroker(MockBroker):
        fail_status = False

        def get_order_status(self, order_id):
            if self.fail_status:
                raise ConnectionError("broker unavailable")
            return super().get_order_status(order_id)

    broker = StatusFailureBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        qty="1",
    )["order_id"]
    _approve(svc, oid)
    broker.fail_status = True

    result = _sync(svc)

    assert result["failed"] == 1
    assert result["synced"] == 0


def test_sync_reports_submitted_outbox_without_broker_id(make_service):
    from trading_assistant.db.models import Order

    svc = make_service()
    with svc.session_factory() as session:
        session.add(
            Order(
                idempotency_key="unknown-acceptance",
                ticker="AAPL",
                side="buy",
                order_type="limit",
                qty=Decimal("1"),
                limit_price=Decimal("95"),
                status="submitted",
                broker_order_id=None,
            )
        )
        session.commit()

    result = _sync(svc)

    assert result["failed"] == 1


def test_sync_replaces_synthetic_fill_with_exact_broker_activity(make_service):
    from datetime import timedelta

    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import BrokerFill, OrderResult, OrderStatus
    from trading_assistant.db.models import Fill, Order

    class ActivityBroker(MockBroker):
        def get_fill_activities(self, after=None):
            return [
                BrokerFill(
                    broker_fill_id="activity-1",
                    broker_order_id=self.order_id,
                    ticker="AAPL",
                    side="buy",
                    qty=Decimal("2"),
                    price=Decimal("332.03"),
                    filled_at=self.exact_time,
                )
            ]

    broker = ActivityBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        qty="2",
    )["order_id"]
    _approve(svc, oid)
    with svc.session_factory() as session:
        order = session.get(Order, oid)
        broker.order_id = order.broker_order_id
        broker.exact_time = (
            order.submission_started_at + timedelta(seconds=1)
        )
        synthetic = Fill(
            order_id=oid,
            ticker="AAPL",
            side="buy",
            qty=Decimal("2"),
            price=Decimal("333"),
            broker_fill_id=f"{order.broker_order_id}:2",
        )
        session.add(synthetic)
        session.commit()
        client_id = order.idempotency_key
    filled = OrderResult(
        client_id,
        broker.order_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("2"),
        avg_fill_price=Decimal("332.03"),
    )
    broker._orders_by_id[broker.order_id] = filled
    broker._orders_by_key[client_id] = filled

    result = _sync(svc)

    assert result["newly_filled"] == 1
    with svc.session_factory() as session:
        fills = session.execute(select(Fill).where(Fill.order_id == oid)).scalars().all()
        assert len(fills) == 1
        assert fills[0].broker_fill_id == "activity-1"
        assert fills[0].price == Decimal("332.030000")
        assert fills[0].filled_at == broker.exact_time


def test_sync_preserves_exact_incremental_activity_prices(make_service):
    """Exact broker activities, not cumulative averages, are the P&L authority."""
    from datetime import timedelta

    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import (
        BrokerFill,
        OrderResult,
        OrderStatus,
    )
    from trading_assistant.db.models import Fill, utcnow

    class ExactActivityBroker(MockBroker):
        cumulative = (OrderStatus.SUBMITTED, Decimal("0"), None)
        activities = []

        def get_order_status(self, oid):
            original = super().get_order_status(oid)
            status, qty, avg = self.cumulative
            return OrderResult(
                original.idempotency_key,
                oid,
                status,
                filled_qty=qty,
                avg_fill_price=avg,
            )

        def get_fill_activities(self, after=None):
            return list(self.activities)

    broker = ExactActivityBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = _propose(
        svc,
        ticker="AAPL",
        side="buy",
        order_type="market",
        qty="3",
    )["order_id"]
    _approve(svc, oid)
    with svc.session_factory() as session:
        broker_order_id = session.get(Order, oid).broker_order_id

    first_at = utcnow()
    broker.cumulative = (
        OrderStatus.PARTIALLY_FILLED,
        Decimal("1"),
        Decimal("100"),
    )
    first = BrokerFill(
        broker_fill_id="incremental-fill-1",
        broker_order_id=broker_order_id,
        ticker="AAPL",
        side="buy",
        qty=Decimal("1"),
        price=Decimal("100"),
        filled_at=first_at,
    )
    broker.activities = [first]
    _sync(svc)
    broker.cumulative = (OrderStatus.FILLED, Decimal("3"), Decimal("110"))
    broker.activities = [
        first,
        BrokerFill(
            broker_fill_id="incremental-fill-2",
            broker_order_id=broker_order_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("2"),
            price=Decimal("115"),
            filled_at=first_at + timedelta(seconds=1),
        ),
    ]
    _sync(svc)

    with svc.session_factory() as s:
        buys = (
            s.execute(select(Fill).where(Fill.order_id == oid).order_by(Fill.id))
            .scalars()
            .all()
        )
        assert [(row.qty, row.price) for row in buys] == [
            (Decimal("1.000000"), Decimal("100.000000")),
            (Decimal("2.000000"), Decimal("115.000000")),
        ]
        # Selling all three at 120 realizes 20 + 10 = 30 using the exact FIFO lots.
        s.add(
                Fill(
                    ticker="AAPL",
                    side="sell",
                    qty=Decimal("3"),
                    price=Decimal("120"),
                    broker_fill_id="incremental-fill-closing-sale",
                    filled_at=utcnow(),
                )
        )
        s.commit()
        assert svc._realized_pnl_today(s) == Decimal("30.000000")
