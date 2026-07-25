"""FastAPI endpoints: pending/approve/reject/positions/log/killswitch, chat, rate limit."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from trading_assistant.app.main import create_app
from trading_assistant.app.ratelimit import RateLimiter
from trading_assistant.broker.base import BrokerDataIntegrityError
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import BrokerFill, OrderStatus, Quote
from trading_assistant.db.models import (
    AuditEvent,
    CircuitBreakerState,
    Order,
    Proposal,
    RiskEvent,
    Rule,
    RuleGroup,
    utcnow,
)
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


def _unsafe_local_state(
    *,
    live_or_unknown_order_ids=(),
    latched_order_ids=(),
    unsafe_fill_ids=(),
    active_rule_ids=(),
    unsafe_rule_group_ids=(),
    unknown_categories=(),
):
    return {
        "live_or_unknown_order_ids": list(
            live_or_unknown_order_ids
        ),
        "latched_order_ids": list(latched_order_ids),
        "unsafe_fill_ids": list(unsafe_fill_ids),
        "active_rule_ids": list(active_rule_ids),
        "unsafe_rule_group_ids": list(unsafe_rule_group_ids),
        "unknown_categories": list(unknown_categories),
    }


def _assert_dependency_unavailable(response):
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "dependency_unavailable",
            "message": "Required dependency is unavailable",
            "request_id": response.headers["X-Request-ID"],
        }
    }
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def _assert_internal_error(response):
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": response.headers["X-Request-ID"],
        }
    }
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


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


@pytest.mark.parametrize("path", ["/positions", "/holdings"])
def test_required_broker_read_outage_returns_hardened_503(
    make_service,
    authenticate_client,
    path,
):
    marker = "provider-secret-required-broker-read"

    class BrokerReadOutage(MockBroker):
        def get_positions(self):
            raise ConnectionError(marker)

    service = make_service(broker=BrokerReadOutage())
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, _csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.get(path)

    _assert_dependency_unavailable(response)
    assert marker not in response.text


@pytest.mark.parametrize(
    ("path", "method_name"),
    [
        ("/positions", "get_positions"),
        ("/holdings", "get_combined_holdings"),
    ],
)
def test_required_read_internal_failure_remains_hardened_500(
    make_service,
    authenticate_client,
    monkeypatch,
    path,
    method_name,
):
    service = make_service()
    marker = f"internal-{method_name}-invariant"

    def fail_internal(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(service, method_name, fail_internal)
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, _csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.get(path)

    _assert_internal_error(response)
    assert marker not in response.text


def test_approval_quote_outage_preserves_outbox_and_retries_safely(
    make_service,
    authenticate_client,
):
    service = make_service()
    order_id = _propose(service)
    marker = "provider-secret-approval-health"
    original_get_quote = service.broker.get_quote
    quote_available = False

    def intermittent_quote(ticker):
        if not quote_available:
            raise ConnectionError(marker)
        return original_get_quote(ticker)

    service.broker.get_quote = intermittent_quote
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    unavailable = client.post(
        f"/approve/{order_id}",
        json={"reason": "human reviewed before dependency outage"},
        headers={"X-CSRF-Token": csrf},
    )

    _assert_dependency_unavailable(unavailable)
    assert marker not in unavailable.text
    assert service.broker.submit_calls == 0
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        failure = session.query(AuditEvent).filter_by(
            action="order.submit",
            request_id=unavailable.headers["X-Request-ID"],
        ).one()
        approval_count = session.query(AuditEvent).filter_by(
            action="order.approve",
            target_id=str(order_id),
        ).count()
    assert order.status == OrderStatus.APPROVAL_RECORDED.value
    assert approval_count == 1
    assert (
        failure.actor,
        failure.reason,
        failure.result_code,
        failure.target_id,
    ) == (
        "operator:local",
        "human reviewed before dependency outage",
        "dependency_unavailable",
        str(order_id),
    )
    assert marker not in failure.detail_json
    assert "ConnectionError" not in failure.detail_json

    quote_available = True
    retried = client.post(
        f"/approve/{order_id}",
        json={"reason": "retry approved outbox after quote recovery"},
        headers={"X-CSRF-Token": csrf},
    )

    assert retried.status_code == 200
    assert retried.json()["status"] == OrderStatus.SUBMITTED.value
    assert service.broker.submit_calls == 1
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        approval_count = session.query(AuditEvent).filter_by(
            action="order.approve",
            target_id=str(order_id),
        ).count()
    assert order.status == OrderStatus.SUBMITTED.value
    assert approval_count == 1


def test_approval_clock_outage_preserves_outbox_audits_exact_context_and_retries(
    make_service,
    authenticate_client,
    caplog,
):
    service = make_service()
    order_id = _propose(service)
    marker = "provider-secret-approval-market-clock"
    clock_available = False
    original_is_open = service.clock.is_open

    def intermittent_is_open(at=None):
        if not clock_available:
            raise ConnectionError(marker)
        return original_is_open(at)

    service.clock.is_open = intermittent_is_open
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )
    reason = "human approved before market clock outage"

    unavailable = client.post(
        f"/approve/{order_id}",
        json={"reason": reason},
        headers={"X-CSRF-Token": csrf},
    )

    _assert_dependency_unavailable(unavailable)
    assert service.broker.submit_calls == 0
    request_id = unavailable.headers["X-Request-ID"]
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        failures = session.query(AuditEvent).filter_by(
            action="order.submit",
            request_id=request_id,
            result_code="dependency_unavailable",
        ).all()
        approval_count = session.query(AuditEvent).filter_by(
            action="order.approve",
            target_id=str(order_id),
        ).count()
        risk_text = "\n".join(
            event.reason for event in session.query(RiskEvent).all()
        )
        breaker_text = "\n".join(
            state.reason
            for state in session.query(CircuitBreakerState).all()
        )
    assert order.status == OrderStatus.APPROVAL_RECORDED.value
    assert approval_count == 1
    assert len(failures) == 1
    failure = failures[0]
    assert (
        failure.actor,
        failure.reason,
        failure.target_type,
        failure.target_id,
        failure.detail_json,
    ) == (
        "operator:local",
        reason,
        "order",
        str(order_id),
        json.dumps({"stage": "execution_health"}, sort_keys=True),
    )
    exposed = "\n".join(
        (
            unavailable.text,
            failure.detail_json,
            risk_text,
            breaker_text,
            caplog.text,
        )
    )
    assert marker not in exposed
    assert "ConnectionError" not in exposed

    clock_available = True
    retried = client.post(
        f"/approve/{order_id}",
        json={"reason": "retry approved outbox after clock recovery"},
        headers={"X-CSRF-Token": csrf},
    )

    assert retried.status_code == 200
    assert retried.json()["status"] == OrderStatus.SUBMITTED.value
    assert service.broker.submit_calls == 1
    with service.session_factory() as session:
        assert (
            session.get(Order, order_id).status
            == OrderStatus.SUBMITTED.value
        )
        assert session.query(AuditEvent).filter_by(
            action="order.approve",
            target_id=str(order_id),
        ).count() == 1


def test_approval_internal_failure_is_hardened_500_without_dependency_audit(
    make_service,
    authenticate_client,
    monkeypatch,
):
    service = make_service()
    order_id = _propose(service)
    marker = "internal-order-submission-invariant"

    def fail_internal(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(
        service.order_submission,
        "submit",
        fail_internal,
    )
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.post(
        f"/approve/{order_id}",
        json={"reason": "approval internal failure probe"},
        headers={"X-CSRF-Token": csrf},
    )

    _assert_internal_error(response)
    assert marker not in response.text
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        dependency_audits = session.query(AuditEvent).filter_by(
            action="order.submit",
            request_id=response.headers["X-Request-ID"],
            result_code="dependency_unavailable",
        ).count()
    assert order.status == OrderStatus.APPROVAL_RECORDED.value
    assert dependency_audits == 0


def test_killswitch_reset_endpoint(client):
    c, svc, _ = client
    observed = svc.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="drill",
        actor="daemon",
        request_id="api-killswitch-drill",
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
        request_id="api-initial-loss",
    )
    retripped = svc.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="new loss evidence",
        actor="daemon:second",
        request_id="api-new-loss",
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
    current = svc.breakers.get(BreakerScope.loss(AssetClass.EQUITY))
    assert current is not None
    assert current.generation == retripped.generation


@pytest.mark.parametrize("quote_failure", ["unavailable", "stale"])
def test_killswitch_reset_quote_failure_is_audited_hardened_503(
    make_service,
    authenticate_client,
    quote_failure,
):
    service = make_service()
    observed = service.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="dependency outage setup",
        actor="daemon:test",
        request_id="dependency-reset-setup",
    )
    marker = "provider-secret-reset-health"
    original_get_quote = service.broker.get_quote

    def fail_quote(ticker):
        if quote_failure == "unavailable":
            raise ConnectionError(marker)
        current = original_get_quote(ticker)
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        return replace(
            current,
            as_of=stale_at,
            book_as_of=stale_at,
            trade_as_of=stale_at,
        )

    service.broker.get_quote = fail_quote
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.post(
        "/killswitch/reset",
        json={
            "asset_class": "equity",
            "reason": "verify dependency health before reset",
            "expected_generation": observed.generation,
        },
        headers={"X-CSRF-Token": csrf},
    )

    _assert_dependency_unavailable(response)
    assert marker not in response.text
    assert service.breakers.is_tripped(
        BreakerScope.loss(AssetClass.EQUITY)
    ) is True
    with service.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="circuit_breaker.reset",
            request_id=response.headers["X-Request-ID"],
        ).one()
    assert (
        audit.actor,
        audit.reason,
        audit.result_code,
        audit.target_id,
    ) == (
        "operator:local",
        "verify dependency health before reset",
        "dependency_unavailable",
        BreakerScope.loss(AssetClass.EQUITY).key,
    )
    assert marker not in audit.detail_json
    assert "ConnectionError" not in audit.detail_json


def test_killswitch_reset_clock_outage_keeps_generation_and_audits_exact_context(
    make_service,
    authenticate_client,
    caplog,
):
    service = make_service()
    observed = service.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="clock dependency outage setup",
        actor="daemon:test",
        request_id="clock-dependency-reset-setup",
    )
    marker = "provider-secret-reset-market-calendar"

    def fail_calendar(at=None):
        raise ConnectionError(marker)

    service.clock.most_recent_open = fail_calendar
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )
    reason = "verify market clock before breaker reset"

    response = client.post(
        "/killswitch/reset",
        json={
            "asset_class": "equity",
            "reason": reason,
            "expected_generation": observed.generation,
        },
        headers={"X-CSRF-Token": csrf},
    )

    _assert_dependency_unavailable(response)
    current = service.breakers.get(
        BreakerScope.loss(AssetClass.EQUITY)
    )
    assert current is not None
    assert current.tripped is True
    assert current.generation == observed.generation
    with service.session_factory() as session:
        failures = session.query(AuditEvent).filter_by(
            action="circuit_breaker.reset",
            request_id=response.headers["X-Request-ID"],
            result_code="dependency_unavailable",
        ).all()
        risk_text = "\n".join(
            event.reason for event in session.query(RiskEvent).all()
        )
        breaker_text = "\n".join(
            state.reason
            for state in session.query(CircuitBreakerState).all()
        )
    assert len(failures) == 1
    failure = failures[0]
    assert (
        failure.actor,
        failure.reason,
        failure.target_type,
        failure.target_id,
        failure.detail_json,
    ) == (
        "operator:local",
        reason,
        "circuit_breaker",
        BreakerScope.loss(AssetClass.EQUITY).key,
        json.dumps(
            {
                "asset_class": "equity",
                "expected_generation": observed.generation,
                "stage": "health_collection",
            },
            sort_keys=True,
        ),
    )
    exposed = "\n".join(
        (
            response.text,
            failure.detail_json,
            risk_text,
            breaker_text,
            caplog.text,
        )
    )
    assert marker not in exposed
    assert "ConnectionError" not in exposed


@pytest.mark.parametrize(
    "incomplete_field",
    [
        "daily_pnl_complete",
        "broker_reconciled",
        "account_complete",
        "pending_exposure_complete",
        "quote_fresh",
        "market_clock_complete",
    ],
)
def test_killswitch_reset_rejects_every_incomplete_health_field(
    make_service,
    authenticate_client,
    monkeypatch,
    incomplete_field,
):
    service = make_service()
    observed = service.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="incomplete health setup",
        actor="daemon:test",
        request_id=f"incomplete-reset-setup-{incomplete_field}",
    )
    complete = service.snapshot_service.assemble_for_execution("AAPL")
    monkeypatch.setattr(
        service.snapshot_service,
        "assemble_for_execution",
        lambda *_args, **_kwargs: replace(
            complete,
            **{incomplete_field: False},
        ),
    )
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.post(
        "/killswitch/reset",
        json={
            "asset_class": "equity",
            "reason": f"reject incomplete {incomplete_field}",
            "expected_generation": observed.generation,
        },
        headers={"X-CSRF-Token": csrf},
    )

    _assert_dependency_unavailable(response)
    current = service.breakers.get(
        BreakerScope.loss(AssetClass.EQUITY)
    )
    assert current is not None
    assert current.tripped is True
    assert current.generation == observed.generation
    with service.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="circuit_breaker.reset",
            request_id=response.headers["X-Request-ID"],
            result_code="dependency_unavailable",
        ).one()
    assert audit.reason == f"reject incomplete {incomplete_field}"


def test_reset_snapshot_internal_failure_is_500_without_dependency_audit(
    make_service,
    authenticate_client,
    monkeypatch,
):
    service = make_service()
    observed = service.breakers.trip(
        BreakerScope.loss(AssetClass.EQUITY),
        reason="internal reset setup",
        actor="daemon:test",
        request_id="internal-reset-setup",
    )
    marker = "internal-reset-snapshot-invariant"

    def fail_internal(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(
        service.snapshot_service,
        "assemble_for_execution",
        fail_internal,
    )
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.post(
        "/killswitch/reset",
        json={
            "asset_class": "equity",
            "reason": "reset internal failure probe",
            "expected_generation": observed.generation,
        },
        headers={"X-CSRF-Token": csrf},
    )

    _assert_internal_error(response)
    assert marker not in response.text
    current = service.breakers.get(
        BreakerScope.loss(AssetClass.EQUITY)
    )
    assert current is not None
    assert current.tripped is True
    assert current.generation == observed.generation
    with service.session_factory() as session:
        dependency_audits = session.query(AuditEvent).filter_by(
            action="circuit_breaker.reset",
            request_id=response.headers["X-Request-ID"],
            result_code="dependency_unavailable",
        ).count()
    assert dependency_audits == 0

def test_unexpected_mutation_exception_remains_hardened_internal_error(
    make_service,
    authenticate_client,
    monkeypatch,
):
    service = make_service()
    marker = "unexpected-internal-secret"

    def fail_reset(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(service, "reset_killswitch", fail_reset)
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.post(
        "/killswitch/reset",
        json={
            "asset_class": "equity",
            "reason": "unexpected failure classification probe",
            "expected_generation": 1,
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": response.headers["X-Request-ID"],
        }
    }
    assert marker not in response.text
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


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
    assert receipt["local_enumeration"] == "confirmed"
    assert receipt["remote_enumeration"] == "confirmed"
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
        "local_enumeration": "confirmed",
        "remote_enumeration": "unknown",
        "confirmed_canceled": [],
        "unconfirmed_order_ids": [],
        "remote_open_order_ids": [],
        "unsafe_local_state": _unsafe_local_state(),
        "message": (
            "panic incomplete: safety could not be confirmed; "
            "broker_enumeration=unconfirmed "
            "unaddressable_remote_open=false "
            "local_unconfirmed=[] remote_open=[]"
        ),
    }
    assert "raw provider outage" not in response.text


def test_panic_exception_returns_sanitized_incomplete_receipt_and_headers(
    make_service, authenticate_client
):
    service = make_service()

    def explode(**context):
        raise RuntimeError("raw provider panic dependency secret")

    service.panic = explode
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    c, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = c.post(
        "/panic",
        json={"reason": "exception safety drill"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "panic_incomplete",
        "message": "Panic could not confirm a safe state",
        "request_id": response.headers["X-Request-ID"],
    }
    assert response.json()["receipt"] == {
        "safe": False,
        "local_enumeration": "confirmed",
        "remote_enumeration": "unknown",
        "confirmed_canceled": [],
        "unconfirmed_order_ids": [],
        "remote_open_order_ids": [],
        "unsafe_local_state": _unsafe_local_state(),
        "message": "panic incomplete: safety could not be confirmed",
    }
    assert "raw provider panic dependency secret" not in response.text
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_panic_exception_fallback_enumerates_every_local_live_unknown_order(
    make_service,
    authenticate_client,
):
    service = make_service()
    statuses = (
        OrderStatus.APPROVED,
        OrderStatus.APPROVAL_RECORDED,
        OrderStatus.SUBMITTING,
        OrderStatus.ACCEPTANCE_UNKNOWN,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
    )
    with service.session_factory() as session:
        rows = [
            Order(
                idempotency_key=f"panic-fallback-{status.value}",
                ticker="AAPL",
                side="buy",
                order_type="market",
                notional=Decimal("100"),
                status=status.value,
            )
            for status in statuses
        ]
        session.add_all(rows)
        session.commit()
        expected_ids = sorted(row.id for row in rows)

    def explode(**context):
        raise RuntimeError("provider-secret-panic-fallback")

    service.panic = explode
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(TestClient(app), TOKEN)

    response = client.post(
        "/panic",
        json={"reason": "enumerate local fail-closed truth"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 503
    receipt = response.json()["receipt"]
    assert receipt == {
        "safe": False,
        "local_enumeration": "confirmed",
        "remote_enumeration": "unknown",
        "confirmed_canceled": [],
        "unconfirmed_order_ids": expected_ids,
        "remote_open_order_ids": [],
        "unsafe_local_state": _unsafe_local_state(
            live_or_unknown_order_ids=expected_ids,
        ),
        "message": "panic incomplete: safety could not be confirmed",
    }
    assert "provider-secret-panic-fallback" not in response.text


def test_panic_exception_fallback_reports_unknown_local_enumeration_on_db_failure(
    make_service,
    authenticate_client,
):
    service = make_service()

    def explode(**context):
        raise RuntimeError("provider-secret-panic-fallback")

    service.panic = explode
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(TestClient(app), TOKEN)

    class BrokenSessionFactory:
        def __call__(self):
            raise RuntimeError("database-secret-panic-fallback")

    service.session_factory = BrokenSessionFactory()
    response = client.post(
        "/panic",
        json={"reason": "database enumeration failure drill"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 503
    assert response.json()["receipt"] == {
        "safe": False,
        "local_enumeration": "unknown",
        "remote_enumeration": "unknown",
        "confirmed_canceled": [],
        "unconfirmed_order_ids": [],
        "remote_open_order_ids": [],
        "unsafe_local_state": _unsafe_local_state(
            unknown_categories=(
                "live_or_unknown_orders",
                "latched_orders",
                "unsafe_fills",
                "active_rules",
                "unsafe_rule_groups",
            ),
        ),
        "message": "panic incomplete: safety could not be confirmed",
    }
    assert "provider-secret-panic-fallback" not in response.text
    assert "database-secret-panic-fallback" not in response.text


def test_panic_rule_audit_failure_returns_unsafe_receipt_without_false_claim(
    make_service,
    authenticate_client,
):
    service = make_service()
    created = service.create_conditional_rule(
        "AAPL",
        {"price_below": "90"},
        {"side": "buy", "notional": "100"},
        actor="operator:setup",
        reason="prepare panic audit failure",
        request_id="panic-audit-failure-setup",
    )
    with service.session_factory() as session:
        rule = session.get(Rule, created["rule_id"])
        group_id = rule.group_id
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(TestClient(app), TOKEN)

    def fail_rule_panic_audit(session, flush_context, instances):
        if any(
            isinstance(row, AuditEvent)
            and row.action == "rule.panic_cancel"
            for row in session.new
        ):
            raise RuntimeError("injected panic cancellation audit failure")

    session_type = service.session_factory.class_
    event.listen(
        session_type,
        "before_flush",
        fail_rule_panic_audit,
    )
    try:
        response = client.post(
            "/panic",
            json={"reason": "panic audit failure drill"},
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        event.remove(
            session_type,
            "before_flush",
            fail_rule_panic_audit,
        )

    assert response.status_code == 503
    receipt = response.json()["receipt"]
    assert receipt["safe"] is False
    assert receipt["confirmed_canceled"] == []
    assert receipt["remote_enumeration"] == "unknown"
    assert service.breakers.is_tripped(
        BreakerScope.operator_global()
    ) is True
    with service.session_factory() as session:
        assert session.get(Rule, created["rule_id"]).state == "active"
        assert session.get(RuleGroup, group_id).state == "active"


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


def test_reconcile_dependency_outage_is_audited_hardened_503(
    make_service,
    authenticate_client,
):
    marker = "provider-secret-position-reconciliation"

    class PositionOutageBroker(MockBroker):
        def get_positions(self):
            raise ConnectionError(marker)

    service = make_service(broker=PositionOutageBroker())
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.post(
        "/reconcile",
        json={"reason": "verify broker positions after outage"},
        headers={"X-CSRF-Token": csrf},
    )

    _assert_dependency_unavailable(response)
    assert marker not in response.text
    with service.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="positions.reconcile",
            request_id=response.headers["X-Request-ID"],
        ).one()
    assert (
        audit.actor,
        audit.reason,
        audit.result_code,
        audit.target_id,
    ) == (
        "operator:local",
        "verify broker positions after outage",
        "dependency_unavailable",
        "all",
    )
    assert marker not in audit.detail_json
    assert "ConnectionError" not in audit.detail_json


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


def test_sync_dependency_outage_is_audited_hardened_503(
    make_service,
    authenticate_client,
):
    marker = "provider-secret-order-sync"

    class FillActivityOutageBroker(MockBroker):
        def get_fill_activities(self, *, after=None):
            raise ConnectionError(marker)

    service = make_service(broker=FillActivityOutageBroker())
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.post(
        "/sync",
        json={"reason": "manual sync dependency probe"},
        headers={"X-CSRF-Token": csrf},
    )

    _assert_dependency_unavailable(response)
    assert marker not in response.text
    with service.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="orders.sync",
            request_id=response.headers["X-Request-ID"],
        ).one()
    assert (
        audit.actor,
        audit.reason,
        audit.result_code,
        audit.target_id,
    ) == (
        "operator:local",
        "manual sync dependency probe",
        "dependency_unavailable",
        "all",
    )
    assert marker not in audit.detail_json
    assert "ConnectionError" not in audit.detail_json


def test_sync_cursor_conflict_is_stable_409_without_dependency_audit(
    make_service,
    authenticate_client,
    monkeypatch,
):
    class FillActivityBroker(MockBroker):
        activity = None

        def get_fill_activities(self, *, after=None):
            return [] if self.activity is None else [self.activity]

    broker = FillActivityBroker()
    service = make_service(broker=broker)
    order_id = _propose(service)
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )
    approved = client.post(
        f"/approve/{order_id}",
        json={"reason": "approve cursor conflict probe"},
        headers={"X-CSRF-Token": csrf},
    )
    assert approved.status_code == 200
    broker.activity = BrokerFill(
        broker_fill_id="cursor-conflict-fill",
        broker_order_id=approved.json()["broker_order_id"],
        ticker="AAPL",
        side="buy",
        qty=Decimal("1"),
        price=Decimal("100"),
        filled_at=utcnow(),
    )
    monkeypatch.setattr(
        service.reconciliation,
        "_cursor_snapshot",
        lambda: (None, None, 99),
    )

    response = client.post(
        "/sync",
        json={"reason": "cursor conflict probe"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "reconciliation_conflict",
            "message": "Reconciliation state changed; retry with fresh state",
            "request_id": response.headers["X-Request-ID"],
        }
    }
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    with service.session_factory() as session:
        dependency_audits = session.query(AuditEvent).filter_by(
            action="orders.sync",
            request_id=response.headers["X-Request-ID"],
            result_code="dependency_unavailable",
        ).count()
    assert dependency_audits == 0


def test_sync_internal_failure_is_hardened_500_without_dependency_audit(
    make_service,
    authenticate_client,
    monkeypatch,
):
    service = make_service()
    marker = "internal-reconciliation-invariant"

    def fail_internal(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(
        service.reconciliation,
        "reconcile",
        fail_internal,
    )
    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )

    response = client.post(
        "/sync",
        json={"reason": "sync internal failure probe"},
        headers={"X-CSRF-Token": csrf},
    )

    _assert_internal_error(response)
    assert marker not in response.text
    with service.session_factory() as session:
        dependency_audits = session.query(AuditEvent).filter_by(
            action="orders.sync",
            request_id=response.headers["X-Request-ID"],
            result_code="dependency_unavailable",
        ).count()
    assert dependency_audits == 0


def test_sync_sanitizes_provider_integrity_text_everywhere(
    client,
    caplog,
):
    c, service, _ = client
    order_id = _propose(service)
    approved = c.post(
        f"/approve/{order_id}",
        json={"reason": "approve provider sanitization probe"},
    )
    assert approved.status_code == 200
    broker_order_id = approved.json()["broker_order_id"]
    marker = "PROVIDER-SECRET-RECONCILIATION-MARKER"

    def invalid_activities(after=None):
        raise BrokerDataIntegrityError(
            marker,
            broker_order_id=broker_order_id,
        )

    def invalid_open_orders():
        raise BrokerDataIntegrityError(
            marker,
            broker_order_id=broker_order_id,
        )

    service.broker.get_fill_activities = invalid_activities
    service.broker.get_open_orders = invalid_open_orders
    response = c.post(
        "/sync",
        json={"reason": "sanitize provider reconciliation failure"},
    )

    assert response.status_code == 200
    assert marker not in response.text
    assert marker not in caplog.text
    with service.session_factory() as session:
        audits = session.query(AuditEvent).all()
        risk_events = session.query(RiskEvent).all()
        breaker = session.get(
            CircuitBreakerState,
            BreakerScope.broker_drift().key,
        )
    assert breaker is not None and breaker.tripped is True
    assert marker not in breaker.reason
    assert all(
        marker not in audit.reason
        and marker not in audit.detail_json
        and marker not in audit.result_code
        for audit in audits
    )
    assert all(marker not in event_row.reason for event_row in risk_events)


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
    assert "local_enumeration" in page
    assert "remote_enumeration" in page
