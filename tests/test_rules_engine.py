"""Conditional-rule trigger evaluation (pure)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_assistant.broker.models import Quote
from trading_assistant.daemon import rules_engine


def _q(last: str) -> Quote:
    p = Decimal(last)
    source_time = datetime.now(timezone.utc)
    return Quote(
        "AAPL",
        bid=p,
        ask=p,
        last=p,
        as_of=source_time,
        book_as_of=source_time,
        trade_as_of=source_time,
    )


def test_price_below():
    assert rules_engine.evaluate({"price_below": 175}, _q("100")) is True
    assert rules_engine.evaluate({"price_below": 50}, _q("100")) is False


def test_price_above():
    assert rules_engine.evaluate({"price_above": 50}, _q("100")) is True
    assert rules_engine.evaluate({"price_above": 175}, _q("100")) is False


def test_unknown_condition_never_fires():
    assert rules_engine.evaluate({"mystery": 1}, _q("100")) is False


def test_describe():
    assert "below 175" in rules_engine.describe({"price_below": 175})
