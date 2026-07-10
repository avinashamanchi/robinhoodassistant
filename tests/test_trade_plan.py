"""TradePlan schema validators."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

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

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)


def _scen(p=(0.2, 0.5, 0.3)):
    return [
        Scenario(name="bear", price_target=Decimal("90"), horizon_days=30, probability=p[0]),
        Scenario(name="base", price_target=Decimal("110"), horizon_days=30, probability=p[1]),
        Scenario(name="bull", price_target=Decimal("130"), horizon_days=30, probability=p[2]),
    ]


def _plan(**over):
    base = dict(
        symbol="AAPL", as_of=TS, action=PlanAction.BUY, confidence=0.6, thesis="t",
        cited_concepts=["Trend"], regime_note="up", reference_price=Decimal("100"),
        scenarios=_scen(),
        invalidation=Invalidation(price_level=Decimal("88"), rationale="below support"),
        entry_plan=EntryPlan(type="ladder", tranches=[
            Tranche(price_level=Decimal("99"), fraction=0.5),
            Tranche(price_level=Decimal("96"), fraction=0.5),
        ]),
        exit_plan=ExitPlan(
            targets=[ExitTarget(price_level=Decimal("115"), fraction_to_sell=0.5),
                     ExitTarget(price_level=Decimal("130"), fraction_to_sell=0.5)],
            stop=Decimal("92"), trailing_stop_pct=8.0, time_stop_days=45,
        ),
    )
    base.update(over)
    return TradePlan(**base)


def test_valid_plan():
    p = _plan()
    assert p.action is PlanAction.BUY and len(p.scenarios) == 3


def test_probabilities_must_sum_to_one():
    with pytest.raises(ValidationError):
        _plan(scenarios=_scen((0.2, 0.2, 0.2)))


def test_buy_tranche_cannot_chase():
    with pytest.raises(ValidationError):
        _plan(entry_plan=EntryPlan(type="ladder", tranches=[
            Tranche(price_level=Decimal("101"), fraction=0.5),   # above ref 100
            Tranche(price_level=Decimal("96"), fraction=0.5),
        ]))


def test_buy_tranches_must_descend():
    with pytest.raises(ValidationError):
        _plan(entry_plan=EntryPlan(type="ladder", tranches=[
            Tranche(price_level=Decimal("96"), fraction=0.5),
            Tranche(price_level=Decimal("99"), fraction=0.5),    # ascending -> invalid
        ]))


def test_entry_fractions_must_sum_to_one():
    with pytest.raises(ValidationError):
        _plan(entry_plan=EntryPlan(type="ladder", tranches=[
            Tranche(price_level=Decimal("99"), fraction=0.3),
            Tranche(price_level=Decimal("96"), fraction=0.3),
        ]))


def test_buy_stop_must_be_tighter_than_invalidation():
    with pytest.raises(ValidationError):
        _plan(exit_plan=ExitPlan(
            targets=[ExitTarget(price_level=Decimal("115"), fraction_to_sell=1.0)],
            stop=Decimal("80"),   # looser (below) invalidation 88 -> invalid
        ))


def test_exit_targets_cannot_exceed_full_position():
    with pytest.raises(ValidationError):
        _plan(exit_plan=ExitPlan(
            targets=[ExitTarget(price_level=Decimal("115"), fraction_to_sell=0.7),
                     ExitTarget(price_level=Decimal("130"), fraction_to_sell=0.7)],
            stop=Decimal("92"),
        ))


def test_no_trade_is_valid():
    p = _plan(action=PlanAction.NO_TRADE)
    assert p.action is PlanAction.NO_TRADE


def test_short_plan_valid():
    p = _plan(
        action=PlanAction.SELL,
        entry_plan=EntryPlan(type="ladder", tranches=[
            Tranche(price_level=Decimal("101"), fraction=0.5),   # above ref, ascending
            Tranche(price_level=Decimal("104"), fraction=0.5),
        ]),
        invalidation=Invalidation(price_level=Decimal("112"), rationale="above resistance"),
        exit_plan=ExitPlan(
            targets=[ExitTarget(price_level=Decimal("90"), fraction_to_sell=1.0)],
            stop=Decimal("108"),   # <= invalidation 112 -> tighter, valid for short
        ),
    )
    assert p.action is PlanAction.SELL
