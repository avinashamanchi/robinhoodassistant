"""/analyze, /plans, /plans/{id}/approve|cancel, /screen endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from trading_assistant.analyst.models import (
    EntryPlan, ExitPlan, ExitTarget, Invalidation, PlanAction, Scenario, TradePlan, Tranche,
)
from trading_assistant.analyst.planning import PlanningService
from trading_assistant.app.main import create_app
from trading_assistant.assets import AssetClass
from trading_assistant.backtest.data import DataSource
from trading_assistant.backtest.synthetic import make_bars
from trading_assistant.broker.alpaca import AlpacaClock
from trading_assistant.config import Secrets
from trading_assistant.db.models import (
    AuditEvent,
    CircuitBreakerState,
    Fill,
    RiskEvent,
    TradePlanRow,
)
from trading_assistant.dependencies import RequiredDependencyUnavailable
from trading_assistant.risk.clock import FakeClock
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)
CLOCK_NOW = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)
TOKEN = "test-plans-operator-secret"
_STATIC = Path("src/trading_assistant/app/static")


def _seed_clock_loss(service) -> None:
    with service.session_factory() as session:
        session.add_all(
            [
                Fill(
                    ticker="AAPL",
                    side="buy",
                    qty=Decimal("100"),
                    price=Decimal("100"),
                    broker_fill_id="plans-clock-loss-open",
                    filled_at=CLOCK_NOW - timedelta(hours=2),
                ),
                Fill(
                    ticker="AAPL",
                    side="sell",
                    qty=Decimal("100"),
                    price=Decimal("50"),
                    broker_fill_id="plans-clock-loss-close",
                    filled_at=CLOCK_NOW - timedelta(hours=1),
                ),
            ]
        )
        session.commit()


def _install_equity_clock(service, clock) -> None:
    service.clock = clock
    service._clocks[AssetClass.EQUITY] = clock
    service.snapshot_service.now = lambda: CLOCK_NOW
    service.broker._now = lambda: CLOCK_NOW


def _malformed_alpaca_calendar_clock(raw_session_open, marker: str):
    client = SimpleNamespace(
        provider_marker=marker,
        get_clock=lambda: SimpleNamespace(is_open=True),
        get_calendar=lambda _request: [
            SimpleNamespace(
                date=CLOCK_NOW.date(),
                open=raw_session_open,
                close=datetime(2026, 7, 24, 16),
            )
        ],
    )
    return AlpacaClock(client)


def _race_alpaca_clock(later_current_state: bool):
    calls = {"clock": 0, "calendar": 0}

    def get_clock():
        calls["clock"] += 1
        return SimpleNamespace(is_open=later_current_state)

    def get_calendar(_request):
        calls["calendar"] += 1
        return [
            SimpleNamespace(
                date=datetime(2026, 7, 23).date(),
                open=datetime(2026, 7, 23, 9, 30),
                close=datetime(2026, 7, 23, 16),
            ),
            SimpleNamespace(
                date=datetime(2026, 7, 24).date(),
                open=datetime(2026, 7, 24, 9, 30),
                close=datetime(2026, 7, 24, 16),
            ),
        ]

    return (
        AlpacaClock(
            SimpleNamespace(
                get_clock=get_clock,
                get_calendar=get_calendar,
            )
        ),
        calls,
    )


def _plan():
    return TradePlan(
        symbol="AAPL", as_of=TS, action=PlanAction.BUY, confidence=0.6, thesis="t",
        cited_concepts=["Trend"], regime_note="range", reference_price=Decimal("100"),
        scenarios=[
            Scenario(name="bear", price_target=Decimal("90"), horizon_days=30, probability=0.2),
            Scenario(name="base", price_target=Decimal("110"), horizon_days=30, probability=0.5),
            Scenario(name="bull", price_target=Decimal("130"), horizon_days=30, probability=0.3)],
        invalidation=Invalidation(price_level=Decimal("88"), rationale="r"),
        entry_plan=EntryPlan(type="single", tranches=[Tranche(price_level=Decimal("99"), fraction=1.0)]),
        exit_plan=ExitPlan(targets=[ExitTarget(price_level=Decimal("120"), fraction_to_sell=1.0)],
                           stop=Decimal("92")),
    )


class _StubAnalyst:
    def analyze_plan(self, features, held_symbols=None, news=None):
        return _plan()


class _StubAgent:
    def chat(self, message, **context):
        return {"reply": "", "tool_calls": []}


def test_planning_startup_internal_failure_is_not_hidden(
    make_service,
    monkeypatch,
):
    marker = "internal-planning-startup-secret"

    def fail_planning_init(self, *args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(PlanningService, "__init__", fail_planning_init)

    with pytest.raises(RuntimeError, match=marker):
        create_app(
            service=make_service(),
            agent=_StubAgent(),
            planning=None,
            api_token=TOKEN,
        )


@pytest.fixture
def client(make_service, authenticate_client):
    svc = make_service()
    provider = lambda sym: MarketFeatures(symbol=sym, asset_class=AssetClass.EQUITY,
                                          as_of=TS, last_close=100.0, regime=Regime.RANGING)
    planning = PlanningService(svc, _StubAnalyst(), provider, Secrets())
    source = DataSource({s: make_bars(300, seed=i)
                         for i, s in enumerate(["AAPL", "MSFT", "SPY"])})
    app = create_app(
        service=svc,
        agent=_StubAgent(),
        planning=planning,
        screen_source=source,
        api_token=TOKEN,
    )
    test_client, csrf = authenticate_client(TestClient(app), TOKEN)
    test_client.headers.update({"X-CSRF-Token": csrf})
    return test_client, svc


def test_analyze_and_plan_flow(client):
    c, svc = client
    response = c.post(
        "/analyze",
        json={
            "symbol": "AAPL",
            "reason": "review AAPL planning inputs",
        },
    )
    res = response.json()
    pid = res["plan_id"]
    assert res["sized"]["direction"] == "long"
    with svc.session_factory() as session:
        create_audit = (
            session.query(AuditEvent)
            .filter_by(action="plan.create", target_id=str(pid))
            .one()
        )
    assert create_audit.actor == "operator:local"
    assert create_audit.reason == "review AAPL planning inputs"
    assert create_audit.request_id == response.headers["X-Request-ID"]

    assert any(p["plan_id"] == pid for p in c.get("/plans").json()["plans"])
    detail = c.get(f"/plans/{pid}").json()
    assert detail["plan"]["action"] == "buy" and "sized" in detail

    approve_response = c.post(
        f"/plans/{pid}/approve", json={"reason": "reviewed plan"}
    )
    approve = approve_response.json()
    # Single-target plan -> server-side bracket (0 daemon rules) OR rules armed.
    assert approve["status"] == "approved"
    assert approve.get("bracket") is not None or approve["rules_created"] >= 1
    with svc.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter_by(action="plan.approve", target_id=str(pid))
            .one()
        )
    assert audit.actor == "operator:local"
    assert audit.reason == "reviewed plan"
    assert audit.request_id == approve_response.headers["X-Request-ID"]

    cancel_response = c.post(
        f"/plans/{pid}/cancel", json={"reason": "review complete"}
    )
    cancel = cancel_response.json()
    assert cancel["status"] == "canceled"
    with svc.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter_by(action="plan.cancel", target_id=str(pid))
            .one()
        )
    assert audit.actor == "operator:local"
    assert audit.reason == "review complete"
    assert audit.request_id == cancel_response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/analyze", {"symbol": "AAPL", "reason": " "}),
        ("/propose", {"n": 1, "reason": ""}),
    ],
)
def test_plan_persistence_routes_reject_blank_reason(client, path, body):
    c, _ = client

    response = c.post(path, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_plan_404(client):
    c, _ = client
    assert c.get("/plans/9999").status_code == 404


def test_plan_cancel_missing_target_has_stable_error(client):
    c, _ = client
    response = c.post(
        "/plans/9999/cancel",
        json={"reason": "reviewed missing plan"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "plan_not_found"


def test_screen_endpoint(client):
    c, _ = client
    response = c.post("/screen")
    rows = response.json()["candidates"]
    # Universe is the allowlist; only AAPL/MSFT exist in the source.
    assert {r["symbol"] for r in rows} <= {"AAPL", "MSFT"}
    assert all("score" in r for r in rows)
    assert response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/screen", None),
        (
            "/propose",
            {
                "n": 1,
                "reason": "screen dependency outage probe",
            },
        ),
    ],
)
def test_screen_dependency_outage_returns_hardened_503(
    client,
    authenticate_client,
    monkeypatch,
    path,
    body,
):
    from trading_assistant.analyst import screener

    authenticated, _ = client
    def fail_screen(*args, **kwargs):
        raise RequiredDependencyUnavailable

    monkeypatch.setattr(screener, "screen_source", fail_screen)
    isolated, csrf = authenticate_client(
        TestClient(
            authenticated.app,
            raise_server_exceptions=False,
        ),
        TOKEN,
    )
    response = isolated.post(
        path,
        json=body,
        headers={"X-CSRF-Token": csrf},
    )

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


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/screen", None),
        (
            "/propose",
            {
                "n": 1,
                "reason": "screen internal failure probe",
            },
        ),
    ],
)
def test_screen_internal_failure_remains_hardened_500(
    client,
    authenticate_client,
    monkeypatch,
    path,
    body,
):
    from trading_assistant.analyst import screener

    authenticated, _ = client
    marker = "internal-screen-invariant"

    def fail_screen(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(screener, "screen_source", fail_screen)
    isolated, csrf = authenticate_client(
        TestClient(
            authenticated.app,
            raise_server_exceptions=False,
        ),
        TOKEN,
    )
    response = isolated.post(
        path,
        json=body,
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
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_plans_ui_served(client):
    c, _ = client
    r = c.get("/plans/ui")
    assert r.status_code == 200 and "Trade plans" in r.text


def test_plans_ui_approval_posts_a_nonempty_review_reason(client):
    script = (_STATIC / "js" / "plans.js").read_text(encoding="utf-8")

    assert "plan-approval-reason" in script
    assert "plan-cancel-reason" in script
    assert "analysis-reason" in script
    assert "proposal-reason" in script
    assert "if (!reason)" in script
    assert "reason" in script
    assert "api(" in script


def test_propose_generates_plans(client):
    c, svc = client
    response = c.post(
        "/propose",
        json={"n": 3, "reason": "review screened candidates"},
    )
    res = response.json()
    assert "proposed" in res and "UNPROVEN" in res["note"]
    made = [p for p in res["proposed"] if "plan_id" in p]
    assert made  # at least one plan created from the top screener candidates
    # Those plans are now in the queue to approve.
    plan_ids = {p["plan_id"] for p in made}
    listed = {p["plan_id"] for p in c.get("/plans").json()["plans"]}
    assert plan_ids <= listed
    with svc.session_factory() as session:
        audits = session.query(AuditEvent).filter(
            AuditEvent.action == "plan.create",
            AuditEvent.target_id.in_({str(plan_id) for plan_id in plan_ids}),
        ).all()
    assert len(audits) == len(plan_ids)
    assert {
        (audit.actor, audit.reason, audit.request_id)
        for audit in audits
    } == {
        (
            "operator:local",
            "review screened candidates",
            response.headers["X-Request-ID"],
        )
    }


def test_propose_returns_fixed_failure_code_without_provider_class_or_text(
    client,
    monkeypatch,
):
    c, svc = client

    class ProviderSecretAnalysisFailure(RuntimeError):
        pass

    def fail_analysis(self, *args, **kwargs):
        try:
            raise ProviderSecretAnalysisFailure(
                "provider-secret-propose-analysis"
            )
        except ProviderSecretAnalysisFailure as exc:
            raise RequiredDependencyUnavailable from exc

    monkeypatch.setattr(PlanningService, "analyze", fail_analysis)

    response = c.post(
        "/propose",
        json={"n": 2, "reason": "provider failure sanitization probe"},
    )

    assert response.status_code == 200
    assert response.json()["proposed"]
    assert {
        row["error"] for row in response.json()["proposed"]
    } == {"analysis_failed"}
    exposed = str(response.json())
    assert "ProviderSecretAnalysisFailure" not in exposed
    assert "provider-secret-propose-analysis" not in exposed
    with svc.session_factory() as session:
        audits = session.query(AuditEvent).filter_by(
            action="plan.create",
            request_id=response.headers["X-Request-ID"],
            result_code="dependency_unavailable",
        ).all()
    assert len(audits) == len(response.json()["proposed"])
    assert {
        (
            audit.actor,
            audit.reason,
            audit.target_type,
        )
        for audit in audits
    } == {
        (
            "operator:local",
            "provider failure sanitization probe",
            "trade_plan",
        )
    }
    assert {
        audit.detail_json for audit in audits
    } == {
        json.dumps({"stage": "analysis"}, sort_keys=True)
    }
    assert {
        audit.target_id for audit in audits
    } == {
        row["symbol"] for row in response.json()["proposed"]
    }
    assert "provider-secret-propose-analysis" not in str(audits)


def test_analyze_returns_stable_error_without_provider_class_or_text(
    client,
    monkeypatch,
):
    c, svc = client

    class ProviderSecretPlanFailure(RuntimeError):
        pass

    def fail_analysis(self, *args, **kwargs):
        try:
            raise ProviderSecretPlanFailure(
                "provider-secret-plan-analysis"
            )
        except ProviderSecretPlanFailure as exc:
            raise RequiredDependencyUnavailable from exc

    monkeypatch.setattr(PlanningService, "analyze", fail_analysis)

    response = c.post(
        "/analyze",
        json={
            "symbol": "AAPL",
            "reason": "provider failure plan probe",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "analysis_failed"
    exposed = response.text
    assert "ProviderSecretPlanFailure" not in exposed
    assert "provider-secret-plan-analysis" not in exposed
    with svc.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="plan.create",
            request_id=response.headers["X-Request-ID"],
        ).one()
    assert audit.actor == "operator:local"
    assert audit.reason == "provider failure plan probe"
    assert audit.target_type == "trade_plan"
    assert audit.target_id == "AAPL"
    assert audit.result_code == "dependency_unavailable"
    assert json.loads(audit.detail_json) == {"stage": "analysis"}
    assert "provider-secret-plan-analysis" not in str(audit)


@pytest.mark.parametrize(
    ("path", "body", "clock_method", "expected_status"),
    [
        (
            "/analyze",
            {
                "symbol": "AAPL",
                "reason": "analyze with required market clock",
            },
            "observe",
            503,
        ),
        (
            "/propose",
            {
                "n": 2,
                "reason": "propose with required market clock",
            },
            "observe",
            200,
        ),
    ],
)
def test_required_snapshot_clock_outage_preserves_route_contract_and_redacts_provider(
    client,
    monkeypatch,
    caplog,
    path,
    body,
    clock_method,
    expected_status,
):
    c, svc = client
    marker = f"provider-secret-{clock_method}-planning"

    def fail_clock(*args, **kwargs):
        raise ConnectionError(marker)

    monkeypatch.setattr(svc.clock, clock_method, fail_clock)

    response = c.post(path, json=body)

    assert response.status_code == expected_status
    request_id = response.headers["X-Request-ID"]
    if path == "/analyze":
        assert response.json() == {
            "error": {
                "code": "analysis_failed",
                "message": "Analysis could not be completed",
                "request_id": request_id,
            }
        }
        expected_targets = {"AAPL"}
    else:
        proposed = response.json()["proposed"]
        assert proposed
        assert {row["error"] for row in proposed} == {
            "analysis_failed"
        }
        expected_targets = {row["symbol"] for row in proposed}
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"

    with svc.session_factory() as session:
        failures = session.query(AuditEvent).filter_by(
            action="plan.create",
            request_id=request_id,
            result_code="dependency_unavailable",
        ).all()
        persisted_plans = session.query(TradePlanRow).count()
        risk_text = "\n".join(
            event.reason for event in session.query(RiskEvent).all()
        )
        breaker_text = "\n".join(
            state.reason
            for state in session.query(CircuitBreakerState).all()
        )
    assert {failure.target_id for failure in failures} == expected_targets
    assert {
        (
            failure.actor,
            failure.reason,
            failure.target_type,
            failure.detail_json,
        )
        for failure in failures
    } == {
        (
            "operator:local",
            body["reason"],
            "trade_plan",
            json.dumps({"stage": "analysis"}, sort_keys=True),
        )
    }
    assert persisted_plans == 0
    exposed = "\n".join(
        (
            response.text,
            "\n".join(failure.detail_json for failure in failures),
            risk_text,
            breaker_text,
            caplog.text,
        )
    )
    assert marker not in exposed
    assert "ConnectionError" not in exposed


@pytest.mark.parametrize(
    ("path", "body", "expected_status"),
    [
        (
            "/analyze",
            {
                "symbol": "AAPL",
                "reason": "reject future boundary analysis",
            },
            503,
        ),
        (
            "/propose",
            {
                "n": 2,
                "reason": "reject future boundary proposals",
            },
            200,
        ),
    ],
)
def test_planning_workflows_reject_future_market_boundary_with_large_loss(
    client,
    caplog,
    path,
    body,
    expected_status,
):
    c, svc = client
    _seed_clock_loss(svc)
    future_boundary = CLOCK_NOW + timedelta(microseconds=1)
    _install_equity_clock(
        svc,
        FakeClock(
            is_open=True,
            most_recent_open=future_boundary,
        ),
    )

    response = c.post(path, json=body)

    assert response.status_code == expected_status
    request_id = response.headers["X-Request-ID"]
    if path == "/analyze":
        assert response.json()["error"]["code"] == "analysis_failed"
        expected_targets = {"AAPL"}
    else:
        proposed = response.json()["proposed"]
        assert proposed
        assert {row["error"] for row in proposed} == {
            "analysis_failed"
        }
        expected_targets = {row["symbol"] for row in proposed}
    with svc.session_factory() as session:
        failures = session.query(AuditEvent).filter_by(
            action="plan.create",
            request_id=request_id,
            result_code="dependency_unavailable",
        ).all()
        assert session.query(TradePlanRow).count() == 0
        risk_text = "\n".join(
            event.reason for event in session.query(RiskEvent).all()
        )
        breaker_text = "\n".join(
            state.reason
            for state in session.query(CircuitBreakerState).all()
        )
    assert {failure.target_id for failure in failures} == expected_targets
    assert {
        (failure.actor, failure.reason, failure.detail_json)
        for failure in failures
    } == {
        (
            "operator:local",
            body["reason"],
            json.dumps({"stage": "analysis"}, sort_keys=True),
        )
    }
    exposed = "\n".join(
        (
            response.text,
            "\n".join(failure.detail_json for failure in failures),
            risk_text,
            breaker_text,
            caplog.text,
        )
    )
    assert future_boundary.isoformat() not in exposed


@pytest.mark.parametrize(
    ("path", "body", "expected_status"),
    [
        (
            "/analyze",
            {
                "symbol": "AAPL",
                "reason": "reject invalid clock analysis",
            },
            503,
        ),
        (
            "/propose",
            {
                "n": 2,
                "reason": "reject invalid clock proposals",
            },
            200,
        ),
    ],
)
@pytest.mark.parametrize(
    "raw_session_open",
    ["provider-secret-invalid-planning-clock", 0, 1, None],
    ids=["secret-string", "integer-zero", "integer-one", "none"],
)
def test_planning_workflows_reject_malformed_alpaca_calendar_and_redact_provider(
    client,
    caplog,
    path,
    body,
    expected_status,
    raw_session_open,
):
    c, svc = client
    marker = "provider-secret-invalid-planning-clock"
    _install_equity_clock(
        svc,
        _malformed_alpaca_calendar_clock(raw_session_open, marker),
    )

    response = c.post(path, json=body)

    assert response.status_code == expected_status
    request_id = response.headers["X-Request-ID"]
    if path == "/analyze":
        assert response.json()["error"]["code"] == "analysis_failed"
        expected_targets = {"AAPL"}
    else:
        proposed = response.json()["proposed"]
        assert proposed
        assert {row["error"] for row in proposed} == {
            "analysis_failed"
        }
        expected_targets = {row["symbol"] for row in proposed}
    with svc.session_factory() as session:
        failures = session.query(AuditEvent).filter_by(
            action="plan.create",
            request_id=request_id,
            result_code="dependency_unavailable",
        ).all()
        assert session.query(TradePlanRow).count() == 0
    assert {failure.target_id for failure in failures} == expected_targets
    assert {
        (failure.actor, failure.reason, failure.detail_json)
        for failure in failures
    } == {
        (
            "operator:local",
            body["reason"],
            json.dumps({"stage": "analysis"}, sort_keys=True),
        )
    }
    exposed = "\n".join(
        (
            response.text,
            "\n".join(failure.detail_json for failure in failures),
            caplog.text,
        )
    )
    assert marker not in exposed
    assert "BrokerDataIntegrityError" not in exposed
    assert "invalid Alpaca market calendar" not in exposed


@pytest.mark.parametrize(
    ("path", "body", "expected_calendar_calls"),
    [
        (
            "/analyze",
            {
                "symbol": "AAPL",
                "reason": "analyze exact pre-close observation",
            },
            1,
        ),
        (
            "/propose",
            {
                "n": 2,
                "reason": "propose exact pre-close observations",
            },
            2,
        ),
    ],
)
def test_planning_workflows_use_calendar_not_later_post_close_state(
    client,
    path,
    body,
    expected_calendar_calls,
):
    c, svc = client
    clock, calls = _race_alpaca_clock(False)
    _install_equity_clock(svc, clock)

    response = c.post(path, json=body)

    assert response.status_code == 200
    assert calls == {
        "clock": 0,
        "calendar": expected_calendar_calls,
    }
    if path == "/analyze":
        assert response.json()["plan_id"] > 0
    else:
        assert response.json()["proposed"]
        assert all(
            "error" not in candidate
            for candidate in response.json()["proposed"]
        )


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/analyze",
            {
                "symbol": "AAPL",
                "reason": "internal plan invariant probe",
            },
        ),
        (
            "/propose",
            {
                "n": 1,
                "reason": "internal plan invariant probe",
            },
        ),
    ],
)
def test_analysis_internal_failure_is_hardened_500_without_false_audit(
    client,
    authenticate_client,
    monkeypatch,
    path,
    body,
):
    authenticated, svc = client
    marker = "internal-plan-invariant-secret"

    def fail_analysis(self, *args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(PlanningService, "analyze", fail_analysis)
    isolated, csrf = authenticate_client(
        TestClient(authenticated.app, raise_server_exceptions=False),
        TOKEN,
    )
    response = isolated.post(
        path,
        json=body,
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
    with svc.session_factory() as session:
        assert session.query(AuditEvent).filter_by(
            request_id=response.headers["X-Request-ID"],
        ).count() == 0


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/analyze",
            {
                "symbol": "AAPL",
                "reason": "analysis audit failure probe",
            },
        ),
        (
            "/propose",
            {
                "n": 1,
                "reason": "analysis audit failure probe",
            },
        ),
    ],
)
def test_analysis_dependency_audit_failure_is_hardened_500(
    client,
    authenticate_client,
    monkeypatch,
    path,
    body,
):
    authenticated, svc = client
    marker = "internal-analysis-audit-secret"

    def fail_analysis(self, *args, **kwargs):
        raise RequiredDependencyUnavailable

    def fail_audit(**kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(PlanningService, "analyze", fail_analysis)
    monkeypatch.setattr(svc, "_audit_dependency_failure", fail_audit)
    isolated, csrf = authenticate_client(
        TestClient(authenticated.app, raise_server_exceptions=False),
        TOKEN,
    )
    response = isolated.post(
        path,
        json=body,
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert marker not in response.text
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    with svc.session_factory() as session:
        assert session.query(AuditEvent).filter_by(
            request_id=response.headers["X-Request-ID"],
        ).count() == 0


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/analyze",
            {
                "symbol": "AAPL",
                "reason": "atomic analysis rollback probe",
            },
        ),
        (
            "/propose",
            {
                "n": 1,
                "reason": "atomic analysis rollback probe",
            },
        ),
    ],
)
def test_plan_create_audit_failure_rolls_back_and_returns_500(
    client,
    authenticate_client,
    path,
    body,
):
    authenticated, svc = client
    marker = "injected plan.create audit failure"

    def fail_plan_create_audit(session, flush_context, instances):
        if any(
            isinstance(row, AuditEvent)
            and row.action == "plan.create"
            for row in session.new
        ):
            raise RuntimeError(marker)

    session_type = svc.session_factory.class_
    event.listen(session_type, "before_flush", fail_plan_create_audit)
    try:
        isolated, csrf = authenticate_client(
            TestClient(authenticated.app, raise_server_exceptions=False),
            TOKEN,
        )
        response = isolated.post(
            path,
            json=body,
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        event.remove(
            session_type,
            "before_flush",
            fail_plan_create_audit,
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert marker not in response.text
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    with svc.session_factory() as session:
        assert session.query(TradePlanRow).count() == 0
        assert session.query(AuditEvent).filter_by(
            request_id=response.headers["X-Request-ID"],
        ).count() == 0
