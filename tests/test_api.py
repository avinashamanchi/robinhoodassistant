"""FastAPI endpoints: pending/approve/reject/positions/log/killswitch, chat, rate limit."""

from __future__ import annotations

from decimal import Decimal
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from trading_assistant.app.main import create_app
from trading_assistant.app.ratelimit import RateLimiter
from trading_assistant.broker.mock import MockBroker
from trading_assistant.db.models import AuditEvent, Proposal, utcnow
from trading_assistant.assets import AssetClass
from trading_assistant.risk.breakers import BreakerScope

TOKEN = "test-api-operator-secret"


class StubAgent:
    def __init__(self):
        self.calls = 0
        self.last_context = None

    def chat(self, message: str, **context):
        self.calls += 1
        self.last_context = context
        return {"reply": f"echo: {message}", "tool_calls": []}


@pytest.fixture
def client(make_service, authenticate_client):
    svc = make_service()
    agent = StubAgent()
    app = create_app(
        service=svc,
        agent=agent,
        api_token=TOKEN,
        chat_rate=RateLimiter(max_requests=2, window_seconds=60),
        approve_rate=RateLimiter(max_requests=100, window_seconds=60),
    )
    test_client, csrf = authenticate_client(TestClient(app), TOKEN)
    test_client.headers.update({"X-CSRF-Token": csrf})
    return test_client, svc, agent


def _propose(svc, notional="100"):
    return svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional=notional,
        actor="operator:test-setup",
        reason="API test proposal setup",
        request_id="api-test-proposal",
    )["order_id"]


def test_index_served(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "Trading Assistant" in r.text


def test_index_mutations_collect_honest_operator_reasons(client):
    c, _, _ = client
    page = c.get("/").text

    assert 'window.prompt("Reason for rejecting this order:")' in page
    assert 'window.prompt("Reason for panic shutdown:")' in page
    assert 'jsonPost({ reason })' in page


def test_pending_approve_flow(client):
    c, svc, _ = client
    order_id = _propose(svc)

    pending = c.get("/pending").json()["pending"]
    assert len(pending) == 1 and pending[0]["order_id"] == order_id

    approve_response = c.post(
        f"/approve/{order_id}", json={"reason": "reviewed in API"}
    )
    approve = approve_response.json()
    assert approve["executed"] is True
    assert svc.broker.submit_calls == 1
    with svc.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(action="order.approve").one()
        assert audit.actor == "operator:local"
        assert audit.reason == "reviewed in API"
        assert audit.request_id == approve_response.headers["X-Request-ID"]

    # No longer pending.
    assert c.get("/pending").json()["pending"] == []


def test_double_approve_returns_409(client):
    c, svc, _ = client
    order_id = _propose(svc)
    assert c.post(f"/approve/{order_id}", json={"reason": "first review"}).status_code == 200
    assert c.post(f"/approve/{order_id}", json={"reason": "duplicate review"}).status_code == 409


def test_expired_approval_returns_stable_409(client):
    c, svc, _ = client
    order_id = _propose(svc)
    with svc.session_factory() as session:
        proposal = session.query(Proposal).filter_by(order_id=order_id).one()
        proposal.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    response = c.post(
        f"/approve/{order_id}",
        json={"reason": "reviewed after proposal expiry"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "approval_conflict",
            "message": "Order approval is no longer current",
            "request_id": response.headers["X-Request-ID"],
        }
    }


def test_approve_requires_non_empty_reason(client):
    c, svc, _ = client
    order_id = _propose(svc)
    assert c.post(f"/approve/{order_id}", json={"reason": " "}).status_code == 422


def test_reject_endpoint(client):
    c, svc, _ = client
    order_id = _propose(svc)
    assert (
        c.post(f"/reject/{order_id}", json={"reason": " "}).status_code
        == 422
    )
    response = c.post(
        f"/reject/{order_id}",
        json={"reason": "thesis invalidated"},
    )
    assert response.json()["status"] == "rejected"
    with svc.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter_by(action="order.reject", target_id=str(order_id))
            .one()
        )
    assert audit.actor == "operator:local"
    assert audit.reason == "thesis invalidated"
    assert audit.request_id == response.headers["X-Request-ID"]


def test_live_order_cancel_requires_reason_and_audits_identity(client):
    c, svc, _ = client
    order_id = _propose(svc)
    assert (
        c.post(
            f"/approve/{order_id}",
            json={"reason": "approved before cancel drill"},
        ).status_code
        == 200
    )

    assert (
        c.post(
            f"/orders/{order_id}/cancel",
            json={"reason": " "},
        ).status_code
        == 422
    )
    response = c.post(
        f"/orders/{order_id}/cancel",
        json={"reason": "operator canceled stale intent"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    with svc.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter_by(action="order.cancel", target_id=str(order_id))
            .one()
        )
    assert audit.actor == "operator:local"
    assert audit.reason == "operator canceled stale intent"
    assert audit.request_id == response.headers["X-Request-ID"]
    with svc.session_factory() as session:
        sync_audit = (
            session.query(AuditEvent)
            .filter_by(action="orders.sync")
            .order_by(AuditEvent.id.desc())
            .first()
        )
    assert sync_audit is not None
    assert sync_audit.actor == audit.actor
    assert sync_audit.reason == audit.reason
    assert sync_audit.request_id == audit.request_id


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/reject/999", "order_not_found"),
        ("/orders/999/cancel", "order_not_found"),
    ],
)
def test_missing_mutation_target_has_stable_error(client, path, code):
    c, _, _ = client

    response = c.post(path, json={"reason": "reviewed missing target"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["request_id"] == response.headers[
        "X-Request-ID"
    ]


def test_positions_and_log(client):
    c, svc, _ = client
    _propose(svc, notional="600")  # rejected -> creates a risk_event
    assert "positions" in c.get("/positions").json()
    log = c.get("/log").json()
    assert len(log["risk_events"]) >= 1


def test_killswitch_reset_endpoint(client):
    c, svc, _ = client
    observed = svc.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="drill",
        actor="daemon",
    )
    health_response = c.get("/health").json()
    assert health_response["killswitch_generation"]["equity"] == (
        observed.generation
    )

    response = c.post(
        "/killswitch/reset",
        json={
            "asset_class": "equity",
            "reason": "account and broker checks are healthy",
            "expected_generation": observed.generation,
        },
    )

    assert response.status_code == 200
    assert response.json()["tripped"] is False
    assert response.json()["generation"] == observed.generation + 1
    with svc.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter_by(action="circuit_breaker.reset")
            .order_by(AuditEvent.id.desc())
            .first()
        )
    assert audit is not None
    assert audit.actor == "operator:local"
    assert audit.reason == "account and broker checks are healthy"
    assert audit.request_id == response.headers["X-Request-ID"]
    health = json.loads(audit.detail_json)["prior_health"]
    assert set(health) >= {
        "captured_at",
        "daily_pnl_complete",
        "daily_total_pnl",
        "broker_reconciled",
        "account_equity",
        "quote_fresh",
    }
    assert "compatibility_facade" not in health


def test_killswitch_reset_returns_conflict_for_stale_generation(client):
    c, svc, _ = client
    observed = svc.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="initial loss",
        actor="daemon:first",
    )
    retripped = svc.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="new loss evidence",
        actor="daemon:second",
    )

    response = c.post(
        "/killswitch/reset",
        json={
            "asset_class": "equity",
            "reason": "stale operator reset",
            "expected_generation": observed.generation,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "breaker_conflict"
    assert response.json()["error"]["request_id"] == response.headers[
        "X-Request-ID"
    ]
    assert svc.breakers.is_tripped(
        BreakerScope.loss(AssetClass.EQUITY)
    ) is True


def test_panic_endpoint_supplies_actor_and_requires_reason(client):
    c, svc, _ = client

    assert c.post("/panic", json={"reason": " "}).status_code == 422
    response = c.post("/panic", json={"reason": "manual API drill"})

    assert response.status_code == 200
    assert response.json()["safe"] is True
    with svc.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter_by(action="circuit_breaker.trip")
            .order_by(AuditEvent.id.desc())
            .first()
        )
    assert audit is not None
    assert audit.actor == "operator:local"
    assert audit.request_id == response.headers["X-Request-ID"]


def test_unsafe_panic_returns_non_2xx_truthful_receipt(
    make_service, authenticate_client
):
    class UnconfirmedCancelBroker(MockBroker):
        def cancel_order(self, order_id):
            raise ConnectionError("provider cancellation detail")

        def get_order_status(self, order_id):
            raise ConnectionError("provider status detail")

    service = make_service(broker=UnconfirmedCancelBroker())
    order_id = service.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        actor="operator:test-setup",
        reason="unsafe panic proposal setup",
        request_id="unsafe-panic-proposal",
    )["order_id"]
    approved = service.approve_order(
        order_id,
        actor="operator:test-setup",
        reason="create live order for unsafe panic regression",
        request_id="unsafe-panic-setup",
    )
    broker_order_id = approved["broker_order_id"]
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    c, csrf = authenticate_client(TestClient(app), TOKEN)

    response = c.post(
        "/panic",
        json={"reason": "cancel all live orders"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "panic_incomplete"
    assert (
        response.json()["error"]["request_id"]
        == response.headers["X-Request-ID"]
    )
    receipt = response.json()["receipt"]
    assert receipt["safe"] is False
    assert receipt["confirmed_canceled"] == []
    assert receipt["unconfirmed_order_ids"] == [order_id]
    assert receipt["remote_open_order_ids"] == [broker_order_id]
    assert "provider cancellation detail" not in response.text
    assert "provider status detail" not in response.text


def test_panic_dependency_failure_returns_stable_non_2xx_receipt(
    make_service, authenticate_client
):
    class EnumerationFailureBroker(MockBroker):
        def get_open_orders(self):
            raise ConnectionError("raw provider outage")

    service = make_service(broker=EnumerationFailureBroker())
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    c, csrf = authenticate_client(TestClient(app), TOKEN)

    response = c.post(
        "/panic",
        json={"reason": "dependency failure drill"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "panic_incomplete"
    assert response.json()["receipt"] == {
        "safe": False,
        "confirmed_canceled": [],
        "unconfirmed_order_ids": [],
        "remote_open_order_ids": [],
        "message": (
            "panic incomplete: safety could not be confirmed; "
            "broker_enumeration=unconfirmed "
            "unaddressable_remote_open=false "
            "local_unconfirmed=[] remote_open=[]"
        ),
    }
    assert "raw provider outage" not in response.text


def test_reconcile_requires_reason_and_audits_operator_identity(client):
    c, svc, _ = client

    assert c.post("/reconcile", json={"reason": " "}).status_code == 422
    response = c.post(
        "/reconcile",
        json={"reason": "reviewed broker and local positions"},
    )

    assert response.status_code == 200
    with svc.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter_by(action="positions.reconcile")
            .order_by(AuditEvent.id.desc())
            .first()
        )
    assert audit is not None
    assert audit.actor == "operator:local"
    assert audit.reason == "reviewed broker and local positions"
    assert audit.request_id == response.headers["X-Request-ID"]


def test_sync_requires_reason_and_audits_operator_identity(client):
    c, svc, _ = client

    assert c.post("/sync", json={"reason": " "}).status_code == 422
    response = c.post(
        "/sync",
        json={"reason": "manual broker status refresh"},
    )

    assert response.status_code == 200
    with svc.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter_by(action="orders.sync")
            .order_by(AuditEvent.id.desc())
            .first()
        )
    assert audit is not None
    assert audit.actor == "operator:local"
    assert audit.reason == "manual broker status refresh"
    assert audit.request_id == response.headers["X-Request-ID"]


def test_chat_and_rate_limit(client):
    c, svc, agent = client
    response = c.post("/chat", json={"message": "hi"})
    assert response.json()["reply"] == "echo: hi"
    assert agent.last_context == {
        "actor": "operator:local",
        "reason": "hi",
        "request_id": response.headers["X-Request-ID"],
    }
    c.post("/chat", json={"message": "again"})       # 2nd allowed (limit=2)
    r = c.post("/chat", json={"message": "third"})   # 3rd blocked
    assert r.status_code == 429


def test_index_only_reports_panic_success_for_explicit_safe_receipt(client):
    c, _, _ = client
    page = c.get("/").text

    assert "r.data.safe === true" in page
