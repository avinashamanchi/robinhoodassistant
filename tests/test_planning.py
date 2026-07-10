"""Plan lifecycle: analyze -> size -> store -> approve (decompose) -> cancel + gate."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from trading_assistant.analyst.models import (
    EntryPlan,
    ExitPlan,
    ExitTarget,
    Invalidation,
    PlanAction,
    Scenario,
    TradePlan,
    Tranche,
)
from trading_assistant.analyst.planning import PlanningService
from trading_assistant.assets import AssetClass
from trading_assistant.config import Secrets, TradingMode
from trading_assistant.db.models import Rule, TradePlanRow
from trading_assistant.risk.clock import FakeClock
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)


def _plan():
    return TradePlan(
        symbol="AAPL", as_of=TS, action=PlanAction.BUY, confidence=0.6, thesis="t",
        cited_concepts=["Trend"], regime_note="range", reference_price=Decimal("100"),
        scenarios=[
            Scenario(name="bear", price_target=Decimal("90"), horizon_days=30, probability=0.2),
            Scenario(name="base", price_target=Decimal("110"), horizon_days=30, probability=0.5),
            Scenario(name="bull", price_target=Decimal("130"), horizon_days=30, probability=0.3),
        ],
        invalidation=Invalidation(price_level=Decimal("88"), rationale="r"),
        entry_plan=EntryPlan(type="ladder", tranches=[
            Tranche(price_level=Decimal("99"), fraction=0.5),
            Tranche(price_level=Decimal("96"), fraction=0.5)]),
        exit_plan=ExitPlan(
            targets=[ExitTarget(price_level=Decimal("120"), fraction_to_sell=1.0)],
            stop=Decimal("92"), trailing_stop_pct=8.0, time_stop_days=45),
    )


class _StubAnalyst:
    def __init__(self, plan):
        self.plan = plan

    def analyze_plan(self, features, held_symbols=None, news=None):
        return self.plan


def _provider(symbol):
    return MarketFeatures(symbol=symbol, asset_class=AssetClass.EQUITY, as_of=TS,
                          last_close=100.0, regime=Regime.RANGING)


def _planning(svc):
    return PlanningService(svc, _StubAnalyst(_plan()), _provider, Secrets())


def test_analyze_stores_sized_plan(make_service):
    svc = make_service()
    out = _planning(svc).analyze("AAPL")
    assert out["plan_id"] > 0
    assert out["sized"]["direction"] == "long"
    assert Decimal(out["sized"]["total_shares"]) > 0


def test_approve_decomposes_into_preapproved_rules(make_service):
    svc = make_service()
    pln = _planning(svc)
    pid = pln.analyze("AAPL")["plan_id"]
    res = pln.approve_plan(pid)
    assert res["status"] == "approved"

    with svc.session_factory() as s:
        rules = s.execute(select(Rule).where(Rule.plan_id == pid)).scalars().all()
        kinds = sorted(r.kind for r in rules)
        assert "entry" in kinds and "target" in kinds and "stop" in kinds
        assert "trailing" in kinds and "time" in kinds
        assert all(r.pre_approved for r in rules)          # armed for the daemon
        assert s.get(TradePlanRow, pid).status == "approved"


def test_cancel_plan_cancels_rules(make_service):
    svc = make_service()
    pln = _planning(svc)
    pid = pln.analyze("AAPL")["plan_id"]
    pln.approve_plan(pid)
    res = pln.cancel_plan(pid)
    assert res["status"] == "canceled" and res["rules_canceled"] >= 1
    with svc.session_factory() as s:
        assert all(r.state == "canceled"
                   for r in s.execute(select(Rule).where(Rule.plan_id == pid)).scalars())


def test_promotion_gate_blocks_live_without_track_record(make_service, app_config, session_factory):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.service import TradingService

    live_cfg = app_config.model_copy(update={
        "trading": app_config.trading.model_copy(update={"mode": TradingMode.LIVE})})
    broker = MockBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc_live = TradingService(broker, session_factory, live_cfg, FakeClock(is_open=True))
    sec = Secrets(live_trading_confirm="I_UNDERSTAND_LIVE_TRADING")
    pln = PlanningService(svc_live, _StubAnalyst(_plan()), _provider, sec)

    pid = pln.analyze("AAPL")["plan_id"]
    res = pln.approve_plan(pid)  # 0 graded calls -> gate blocks live approval
    assert "promotion gate" in res["error"]
