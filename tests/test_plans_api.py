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
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)


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
    def chat(self, message):
        return {"reply": "", "tool_calls": []}


@pytest.fixture
def client(make_service):
    svc = make_service()
    provider = lambda sym: MarketFeatures(symbol=sym, asset_class=AssetClass.EQUITY,
                                          as_of=TS, last_close=100.0, regime=Regime.RANGING)
    planning = PlanningService(svc, _StubAnalyst(), provider, Secrets())
    source = DataSource({s: make_bars(300, seed=i)
                         for i, s in enumerate(["AAPL", "MSFT", "SPY"])})
    app = create_app(service=svc, agent=_StubAgent(), planning=planning, screen_source=source)
    return TestClient(app), svc


def test_analyze_and_plan_flow(client):
    c, _ = client
    res = c.post("/analyze", json={"symbol": "AAPL"}).json()
    pid = res["plan_id"]
    assert res["sized"]["direction"] == "long"

    assert any(p["plan_id"] == pid for p in c.get("/plans").json()["plans"])
    detail = c.get(f"/plans/{pid}").json()
    assert detail["plan"]["action"] == "buy" and "sized" in detail

    approve = c.post(f"/plans/{pid}/approve").json()
    assert approve["status"] == "approved" and approve["rules_created"] >= 1

    cancel = c.post(f"/plans/{pid}/cancel").json()
    assert cancel["status"] == "canceled"


def test_plan_404(client):
    c, _ = client
    assert c.get("/plans/9999").status_code == 404


def test_screen_endpoint(client):
    c, _ = client
    rows = c.post("/screen").json()["candidates"]
    # Universe is the allowlist; only AAPL/MSFT exist in the source.
    assert {r["symbol"] for r in rows} <= {"AAPL", "MSFT"}
    assert all("score" in r for r in rows)


def test_plans_ui_served(client):
    c, _ = client
    r = c.get("/plans/ui")
    assert r.status_code == 200 and "Trade Plans" in r.text
