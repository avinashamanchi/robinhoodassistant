"""/backtests endpoints: run, list, report, UI, 404."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trading_assistant.backtest.runner as runner
from trading_assistant.app.main import create_app
from trading_assistant.db.models import AuditEvent

TOKEN = "test-backtests-operator-secret"
_STATIC = Path("src/trading_assistant/app/static")


class StubAgent:
    def chat(self, message: str, **context):
        return {"reply": "", "tool_calls": []}


@pytest.fixture
def client(make_service, authenticate_client):
    svc = make_service()
    app = create_app(
        service=svc,
        agent=StubAgent(),
        api_token=TOKEN,
        planning=None,
    )
    test_client, csrf = authenticate_client(
        TestClient(app, raise_server_exceptions=False),
        TOKEN,
    )
    test_client.headers.update({"X-CSRF-Token": csrf})
    return test_client, svc


def _seed(svc, bars=420):
    run_id, _ = runner.run_synthetic_backtest(
        svc.session_factory,
        symbols=["TREND"],
        bars=bars,
        actor="test:seed",
        reason="seed backtest fixture",
        request_id="backtest-seed",
    )
    return run_id


def test_list_and_report(client):
    c, svc = client
    run_id = _seed(svc)
    listed = c.get("/backtests").json()["backtests"]
    assert any(b["run_id"] == run_id for b in listed)

    rep = c.get(f"/backtests/{run_id}/report").json()
    assert rep["disclaimer"].startswith("Simulated")
    assert len(rep["rows"]) >= 1
    first = rep["rows"][0]
    assert "metrics" in first and "benchmark_buy_and_hold" in first
    assert "beat_buy_and_hold" in first


def test_report_404(client):
    c, _ = client
    assert c.get("/backtests/9999/report").status_code == 404


def test_ui_served(client):
    c, _ = client
    r = c.get("/backtests/ui")
    assert r.status_code == 200
    assert "Simulated" in r.text  # mandatory disclaimer present in the page
    script = (_STATIC / "js" / "backtests.js").read_text(
        encoding="utf-8"
    )
    assert "backtest-reason" in script
    assert "reason" in script


def test_run_endpoint_persists(client, monkeypatch):
    c, svc = client
    orig = runner.run_synthetic_backtest
    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        lambda sf, symbols=None, **kw: orig(
            sf,
            symbols=["TREND"],
            bars=420,
            **kw,
        ),
    )
    response = c.post(
        "/backtests/run",
        json={"reason": "compare synthetic strategies"},
        headers={"Idempotency-Key": "backtest-run-persist"},
    )
    res = response.json()
    assert "run_id" in res
    assert res["report"]["disclaimer"].startswith("Simulated")
    # The run is now listable.
    assert any(b["run_id"] == res["run_id"] for b in c.get("/backtests").json()["backtests"])
    with svc.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="backtest.run",
            target_id=str(res["run_id"]),
        ).one()
    assert audit.actor == "operator:local"
    assert audit.reason == "compare synthetic strategies"
    assert audit.request_id == response.headers["X-Request-ID"]
    assert audit.result_code == "succeeded"


def test_run_endpoint_rejects_blank_reason(client):
    c, _ = client

    response = c.post(
        "/backtests/run",
        json={"reason": " "},
        headers={"Idempotency-Key": "backtest-run-blank-reason"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_failed_backtest_launch_is_sanitized_and_audited(
    client, monkeypatch
):
    c, svc = client

    def explode(*args, **kwargs):
        raise RuntimeError("provider-secret-backtest-prompt")

    monkeypatch.setattr(runner, "build_synthetic_source", explode)

    response = c.post(
        "/backtests/run",
        json={"reason": "failure-path review"},
        headers={"Idempotency-Key": "backtest-run-failure"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "provider-secret-backtest-prompt" not in response.text
    with svc.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="backtest.run",
            result_code="failed",
        ).one()
    assert audit.actor == "operator:local"
    assert audit.reason == "failure-path review"
    assert audit.request_id == response.headers["X-Request-ID"]
    assert "provider-secret-backtest-prompt" not in audit.detail_json
