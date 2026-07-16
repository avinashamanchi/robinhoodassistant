"""D1 shadow mode, D2 digest, D4 bracket orders, C4 accuracy report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select

from trading_assistant.analyst.accuracy import analyst_accuracy
from trading_assistant.analyst.analyst import Analyst
from trading_assistant.analyst.digest import compose_digest
from trading_assistant.analyst.models import (
    EntryPlan, ExitPlan, ExitTarget, Invalidation, PlanAction, Scenario, TradePlan, Tranche,
)
from trading_assistant.analyst.planning import PlanningService
from trading_assistant.analyst.shadow import ShadowRunner
from trading_assistant.analyst.store import build_scorecard_from_db
from trading_assistant.assets import AssetClass
from trading_assistant.backtest.data import DataSource
from trading_assistant.backtest.llm_runner import LLMRunConfig
from trading_assistant.backtest.synthetic import make_bars
from trading_assistant.config import Secrets
from trading_assistant.db.models import Rule, ShadowCall, TradePlanRow, utcnow
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)


def _plan(single=False):
    entry = (EntryPlan(type="single", tranches=[Tranche(price_level=Decimal("99"), fraction=1.0)])
             if single else
             EntryPlan(type="ladder", tranches=[Tranche(price_level=Decimal("99"), fraction=0.5),
                                                Tranche(price_level=Decimal("96"), fraction=0.5)]))
    return TradePlan(
        symbol="AAPL", as_of=TS, action=PlanAction.BUY, confidence=0.6, thesis="t",
        cited_concepts=["Trend"], regime_note="range", reference_price=Decimal("100"),
        scenarios=[Scenario(name="bear", price_target=Decimal("90"), horizon_days=30, probability=0.2),
                   Scenario(name="base", price_target=Decimal("110"), horizon_days=30, probability=0.5),
                   Scenario(name="bull", price_target=Decimal("130"), horizon_days=30, probability=0.3)],
        invalidation=Invalidation(price_level=Decimal("88"), rationale="r"),
        entry_plan=entry,
        exit_plan=ExitPlan(targets=[ExitTarget(price_level=Decimal("120"), fraction_to_sell=1.0)],
                           stop=Decimal("92")),
    )


class _StubAnalyst:
    def __init__(self, plan):
        self.plan = plan

    def analyze_plan(self, features, held_symbols=None, news=None):
        return self.plan


def _provider(sym):
    return MarketFeatures(symbol=sym, asset_class=AssetClass.EQUITY, as_of=TS,
                          last_close=100.0, regime=Regime.RANGING)


# ── D1 shadow mode ──────────────────────────────────────────────
def test_shadow_creates_graded_calls_without_orders(make_service):
    svc = make_service()
    planning = PlanningService(svc, _StubAnalyst(_plan()), _provider, Secrets())
    source = DataSource({s: make_bars(300, seed=i) for i, s in enumerate(["AAPL", "MSFT", "SPY"])})
    shadow = ShadowRunner(svc, planning, source, lambda sym: Decimal("110"), top_n=2)

    ids = shadow.run_once()
    assert len(ids) >= 1
    assert svc.broker.submit_calls == 0                      # zero orders
    with svc.session_factory() as s:
        assert all(s.get(TradePlanRow, i).shadow for i in ids)
        assert s.execute(select(func.count()).select_from(ShadowCall)).scalar_one() == len(ids)
        # Mature them and grade.
        for sc in s.execute(select(ShadowCall)).scalars():
            sc.grade_after = utcnow() - timedelta(days=1)
        s.commit()
    assert shadow.grade_due() == len(ids)
    with svc.session_factory() as s:
        assert build_scorecard_from_db(s).n_calls == len(ids)   # track record built, risk-free


# ── D2 digest ───────────────────────────────────────────────────
def test_digest_has_sections(make_service):
    d = compose_digest(make_service())
    for section in ("morning digest", "Equity", "Pending approvals", "Kill switch", "Scorecard"):
        assert section in d


# ── D4 bracket orders ───────────────────────────────────────────
def test_single_target_plan_uses_bracket(make_service):
    svc = make_service()
    planning = PlanningService(svc, _StubAnalyst(_plan(single=True)), _provider, Secrets())
    pid = planning.analyze("AAPL")["plan_id"]
    res = planning.approve_plan(pid)

    assert res["bracket"] is not None and res["bracket"]["bracket"] is True
    assert len(svc.broker.brackets) == 1                    # server-side OCO submitted
    with svc.session_factory() as s:
        kinds = {r.kind for r in s.execute(select(Rule).where(Rule.plan_id == pid)).scalars()}
    assert not ({"entry", "target", "stop"} & kinds)        # handled by the bracket


def test_ladder_plan_still_uses_rules(make_service):
    svc = make_service()
    planning = PlanningService(svc, _StubAnalyst(_plan(single=False)), _provider, Secrets())
    pid = planning.analyze("AAPL")["plan_id"]
    planning.approve_plan(pid)
    assert len(svc.broker.brackets) == 0                    # ladder -> daemon rules, not bracket
    with svc.session_factory() as s:
        kinds = {r.kind for r in s.execute(select(Rule).where(Rule.plan_id == pid)).scalars()}
    assert "entry" in kinds and "stop" in kinds


# ── C4 accuracy report (mock analyst) ───────────────────────────
def _analyst(action="buy", conf=0.8):
    inp = {"action": action, "confidence": conf, "thesis": "t",
           "cited_concepts": ["Trend"], "regime_note": "r"}
    block = SimpleNamespace(type="tool_use", name="submit_analysis", id="t", input=inp)

    class B:
        def create(self, *, system, messages, tools, tool_choice=None):
            return SimpleNamespace(content=[block])

    return Analyst(B())


def test_accuracy_report_shape(make_service):
    source = DataSource({"AAPL": make_bars(300, seed=1), "SPY": make_bars(300, seed=2)})
    rep = analyst_accuracy(source, ["AAPL"], _analyst(), LLMRunConfig(max_llm_calls=100), spy_symbol="SPY")
    assert set(rep) >= {"hit_rate", "brier", "calibration", "per_regime",
                        "analyst_avg_return_pct", "buy_hold_avg_return_pct", "shows_edge", "verdict"}
    assert rep["graded_calls"] >= 1
    # A single always-buy 0.8-confidence analyst can't clear the >=50-call edge bar.
    assert rep["shows_edge"] is False


# ── daemon daily tasks (D1/D2 wiring) ───────────────────────────
def test_daily_tasks_run_once_per_day(make_service):
    from trading_assistant.daemon.monitor import Monitor
    from trading_assistant.notifications.base import RecordingNotifier

    svc = make_service()
    notifier = RecordingNotifier()
    mon = Monitor(svc, notifier)   # no shadow -> digest only
    r1 = mon.run_daily_tasks()
    assert r1["ran"] is True and r1.get("digest_sent") is True
    assert len(notifier.sent) == 1
    # Same day -> no-op (idempotent).
    assert mon.run_daily_tasks()["ran"] is False
    assert len(notifier.sent) == 1
