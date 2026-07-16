"""LLM analyst: structured output, citation/regime requirements, earnings guard."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trading_assistant.analyst.analyst import Analyst
from trading_assistant.analyst.models import AnalystAction
from trading_assistant.assets import AssetClass
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)


def _feat(**kw) -> MarketFeatures:
    base = dict(symbol="AAPL", asset_class=AssetClass.EQUITY, as_of=TS, regime=Regime.RANGING)
    base.update(kw)
    return MarketFeatures(**base)


def _backend(inp):
    block = SimpleNamespace(type="tool_use", name="submit_analysis", id="t1", input=inp)

    class B:
        def create(self, *, system, messages, tools, tool_choice=None):
            return SimpleNamespace(content=[block])

    return B()


_VALID = {
    "action": "buy",
    "confidence": 0.7,
    "thesis": "Oversold bounce setup in a range.",
    "cited_concepts": ["Momentum (RSI)", "Regime conditioning"],
    "regime_note": "RANGING favors mean-reversion, so oversold is actionable.",
}


def test_analyst_returns_structured_report():
    analyst = Analyst(_backend(_VALID))
    report = analyst.analyze(_feat(rsi_14=28))
    assert report.action is AnalystAction.BUY
    assert report.symbol == "AAPL"
    assert report.cited_concepts and report.regime_note


def test_missing_cited_concepts_rejected():
    bad = dict(_VALID, cited_concepts=[])
    with pytest.raises(ValidationError):
        Analyst(_backend(bad)).analyze(_feat())


def test_earnings_in_horizon_must_be_addressed():
    analyst = Analyst(_backend(_VALID))  # _VALID has no earnings_note
    with pytest.raises(ValueError):
        analyst.analyze(_feat(days_to_next_earnings=5))


def test_earnings_addressed_passes():
    inp = dict(_VALID, earnings_note="Earnings in 5d — reducing size to accept gap risk.")
    report = Analyst(_backend(inp)).analyze(_feat(days_to_next_earnings=5))
    assert report.earnings_note is not None


def test_no_tool_call_raises():
    class B:
        def create(self, *, system, messages, tools, tool_choice=None):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="no")])

    with pytest.raises(ValueError):
        Analyst(B()).analyze(_feat())
