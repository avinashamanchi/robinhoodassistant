"""/backtests endpoints: run, list, report, UI, 404."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import trading_assistant.backtest.runner as runner
from trading_assistant.app.main import create_app


class StubAgent:
    def chat(self, message: str):
        return {"reply": "", "tool_calls": []}


@pytest.fixture
def client(make_service):
    svc = make_service()
    return TestClient(create_app(service=svc, agent=StubAgent(), api_token="")), svc


def _seed(svc, bars=420):
    run_id, _ = runner.run_synthetic_backtest(svc.session_factory, symbols=["TREND"], bars=bars)
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


def test_run_endpoint_persists(client, monkeypatch):
    c, svc = client
    orig = runner.run_synthetic_backtest
    monkeypatch.setattr(
        runner,
        "run_synthetic_backtest",
        lambda sf, symbols=None, **kw: orig(sf, symbols=["TREND"], bars=420),
    )
    res = c.post("/backtests/run", json={}).json()
    assert "run_id" in res
    assert res["report"]["disclaimer"].startswith("Simulated")
    # The run is now listable.
    assert any(b["run_id"] == res["run_id"] for b in c.get("/backtests").json()["backtests"])
