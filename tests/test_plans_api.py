"""/analyze, /plans, /plans/{id}/approve|cancel, /screen endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from trading_assistant.analyst.models import (
    EntryPlan, ExitPlan, ExitTarget, Invalidation, PlanAction, Scenario, TradePlan, Tranche,
)
from trading_assistant.analyst.planning import PlanningService
from trading_assistant.app.main import create_app
from trading_assistant.assets import AssetClass
from trading_assistant.backtest.data import DataSource
from trading_assistant.backtest.synthetic import make_bars
from trading_assistant.config import Secrets
from trading_assistant.db.models import AuditEvent
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)
TOKEN = "test-plans-operator-secret"


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
    marker = "provider-secret-screen-source"

    def fail_screen(*args, **kwargs):
        raise ConnectionError(marker)

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
    assert marker not in response.text
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_plans_ui_served(client):
    c, _ = client
    r = c.get("/plans/ui")
    assert r.status_code == 200 and "Trade Plans" in r.text


def test_plans_ui_approval_posts_a_nonempty_review_reason(client):
    c, _ = client
    page = c.get("/plans/ui").text

    assert 'window.prompt("Review reason for plan approval:")' in page
    assert "if (!reason)" in page
    assert "jsonPost({ reason })" in page
    assert 'window.prompt("Reason for canceling this plan:")' in page
    assert 'window.prompt("Reason for analyzing this symbol:")' in page
    assert 'window.prompt("Reason for generating screened plans:")' in page
    assert "X-CSRF-Token" in page
    assert "X-API-Key" not in page


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
    c, _ = client

    class ProviderSecretAnalysisFailure(RuntimeError):
        pass

    def fail_analysis(self, *args, **kwargs):
        raise ProviderSecretAnalysisFailure(
            "provider-secret-propose-analysis"
        )

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


def test_analyze_returns_stable_error_without_provider_class_or_text(
    client,
    monkeypatch,
):
    c, _ = client

    class ProviderSecretPlanFailure(RuntimeError):
        pass

    def fail_analysis(self, *args, **kwargs):
        raise ProviderSecretPlanFailure(
            "provider-secret-plan-analysis"
        )

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
