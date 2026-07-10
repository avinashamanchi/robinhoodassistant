"""Deterministic sizing: risk math, tranche rounding, every cap, zero paths, shorts."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

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
from trading_assistant.analyst.sizing import size_trade
from trading_assistant.broker.models import Position

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)
EQUITY = Decimal("100000")


def _scen():
    return [
        Scenario(name="bear", price_target=Decimal("90"), horizon_days=30, probability=0.2),
        Scenario(name="base", price_target=Decimal("110"), horizon_days=30, probability=0.5),
        Scenario(name="bull", price_target=Decimal("130"), horizon_days=30, probability=0.3),
    ]


def _plan(action=PlanAction.BUY, **over):
    base = dict(
        symbol="AAPL", as_of=TS, action=action, confidence=0.6, thesis="t",
        cited_concepts=["Trend"], regime_note="up", reference_price=Decimal("100"),
        scenarios=_scen(),
        invalidation=Invalidation(price_level=Decimal("88"), rationale="r"),
        entry_plan=EntryPlan(type="ladder", tranches=[
            Tranche(price_level=Decimal("99"), fraction=0.5),
            Tranche(price_level=Decimal("96"), fraction=0.5),
        ]),
        exit_plan=ExitPlan(
            targets=[ExitTarget(price_level=Decimal("120"), fraction_to_sell=1.0)],
            stop=Decimal("92"),
        ),
    )
    base.update(over)
    return TradePlan(**base)


def _permissive(risk_config):
    return risk_config.model_copy(update={
        "max_notional_per_order": 1e9,
        "max_position_per_ticker": 1e9,
        "max_portfolio_exposure": 1e9,
    })


def test_risk_based_share_math(risk_config, make_snapshot):
    # risk_budget = 100000 * 0.5% = 500; weighted_entry = 97.5; stop 92 -> 5.5/share.
    # total = floor(500/5.5) = 90; tranches floor(90*0.5)=45 each.
    plan = _plan()
    sized = size_trade(plan, make_snapshot(), _permissive(risk_config), EQUITY)
    assert sized.total_shares == Decimal("90")
    assert [t.shares for t in sized.tranches] == [Decimal("45"), Decimal("45")]
    assert sized.direction == "long"


def test_per_order_notional_cap_binds(risk_config, make_snapshot):
    # Default max_notional_per_order = 500 -> floor(500/99)=5 per tranche.
    sized = size_trade(_plan(), make_snapshot(), risk_config, EQUITY)
    assert [t.shares for t in sized.tranches] == [Decimal("5"), Decimal("5")]


def test_position_cap_binds(risk_config, make_snapshot):
    cfg = _permissive(risk_config).model_copy(update={"max_position_per_ticker": 1000})
    # max added = floor(1000/100)=10 shares total.
    sized = size_trade(_plan(), make_snapshot(), cfg, EQUITY)
    assert sized.total_shares == Decimal("10")


def test_exposure_cap_binds(risk_config, make_snapshot):
    cfg = _permissive(risk_config).model_copy(update={"max_portfolio_exposure": 800})
    sized = size_trade(_plan(), make_snapshot(), cfg, EQUITY)
    assert sized.total_shares == Decimal("8")  # floor(800/100)


def test_existing_position_reduces_headroom(risk_config, make_snapshot):
    cfg = _permissive(risk_config).model_copy(update={"max_position_per_ticker": 2000})
    snap = make_snapshot(positions=[Position("AAPL", Decimal("15"), Decimal("100"), Decimal("100"))])
    # 2000/100=20 minus existing 15 -> only 5 more shares allowed.
    sized = size_trade(_plan(), snap, cfg, EQUITY)
    assert sized.total_shares == Decimal("5")


def test_no_trade_sizes_zero(risk_config, make_snapshot):
    sized = size_trade(_plan(action=PlanAction.NO_TRADE), make_snapshot(), risk_config, EQUITY)
    assert sized.total_shares == Decimal("0")
    assert "no_trade" in sized.zero_reason


def test_stop_on_wrong_side_sizes_zero(risk_config, make_snapshot):
    # stop 98 > weighted_entry 97.5 -> risk/share <= 0 (schema allows: 98 >= invalidation 88).
    plan = _plan(exit_plan=ExitPlan(
        targets=[ExitTarget(price_level=Decimal("120"), fraction_to_sell=1.0)],
        stop=Decimal("98"),
    ))
    sized = size_trade(plan, make_snapshot(), _permissive(risk_config), EQUITY)
    assert sized.total_shares == Decimal("0")
    assert "risk side" in sized.zero_reason


def test_short_sizing(risk_config, make_snapshot):
    plan = _plan(
        action=PlanAction.SELL,
        entry_plan=EntryPlan(type="single", tranches=[Tranche(price_level=Decimal("100"), fraction=1.0)]),
        invalidation=Invalidation(price_level=Decimal("112"), rationale="r"),
        exit_plan=ExitPlan(targets=[ExitTarget(price_level=Decimal("90"), fraction_to_sell=1.0)], stop=Decimal("105")),
    )
    # risk/share = stop 105 - entry 100 = 5; total = floor(500/5) = 100.
    sized = size_trade(plan, make_snapshot(), _permissive(risk_config), EQUITY)
    assert sized.direction == "short"
    assert sized.total_shares == Decimal("100")
