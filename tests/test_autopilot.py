"""Autopilot: deterministic decisions + paper-only autonomous execution.

These prove the opt-in autopilot both decides (deterministic strategy) and
executes (through the real propose -> approve -> risk-engine path) without a
human, while refusing to run outside paper mode and honouring the daily cap and
position de-dupe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_assistant.assets import AssetClass
from trading_assistant.autopilot import (
    Autopilot,
    AutopilotDisabled,
    require_paper,
    sma_crossover_decision,
)
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import Position
from trading_assistant.config import TradingMode
from trading_assistant.signals.models import MarketFeatures


def _features(symbol, sma20, sma50, *, sma200=None, last_close=None):
    return MarketFeatures(
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        as_of=datetime(2026, 7, 31, tzinfo=timezone.utc),
        sma_20=sma20,
        sma_50=sma50,
        sma_200=sma200,
        last_close=last_close,
    )


def _provider(mapping):
    def prov(symbol):
        return mapping[symbol.upper()]

    return prov


# ── deterministic strategy ────────────────────────────────────────────────────
def test_decision_long_when_fast_leads_and_above_trend():
    f = _features("AAPL", 105, 100, sma200=90, last_close=110)
    assert sma_crossover_decision(f) == "long"


def test_decision_flat_when_fast_below_slow():
    assert sma_crossover_decision(_features("AAPL", 95, 100)) == "flat"


def test_decision_flat_when_below_long_term_trend():
    f = _features("AAPL", 105, 100, sma200=120, last_close=110)
    assert sma_crossover_decision(f) == "flat"


def test_decision_flat_on_incomplete_data():
    assert sma_crossover_decision(_features("AAPL", None, None)) == "flat"


# ── paper-only guard ──────────────────────────────────────────────────────────
def test_require_paper_rejects_non_paper():
    live = SimpleNamespace(trading=SimpleNamespace(mode=TradingMode.LIVE))
    with pytest.raises(AutopilotDisabled):
        require_paper(live)


def test_require_paper_allows_paper():
    paper = SimpleNamespace(trading=SimpleNamespace(mode=TradingMode.PAPER))
    require_paper(paper)  # does not raise


# ── autonomous execution through the real risk path ───────────────────────────
def test_run_once_buys_on_long_signal(make_service):
    service = make_service()  # SpyBroker, AAPL @ $100, market open, paper mock
    feats = {"AAPL": _features("AAPL", 105, 100, sma200=90, last_close=100)}
    ap = Autopilot(
        service,
        _provider(feats),
        universe=["AAPL"],
        notional_per_trade=Decimal("100"),
        max_orders_per_day=8,
    )
    results = ap.run_once()
    assert len(results) == 1
    assert results[0]["side"] == "buy"
    assert results[0]["executed"] is True
    assert service.broker.submit_calls == 1


def test_run_once_does_not_rebuy_a_held_name(make_service):
    broker = MockBroker(
        positions=[Position("AAPL", Decimal("5"), Decimal("100"), Decimal("100"))]
    )
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    feats = {"AAPL": _features("AAPL", 105, 100, sma200=90, last_close=100)}
    ap = Autopilot(
        service,
        _provider(feats),
        universe=["AAPL"],
        notional_per_trade=Decimal("100"),
        max_orders_per_day=8,
    )
    assert ap.run_once() == []


def test_run_once_exits_held_name_on_flat_signal(make_service):
    broker = MockBroker(
        positions=[Position("AAPL", Decimal("5"), Decimal("100"), Decimal("100"))]
    )
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    feats = {"AAPL": _features("AAPL", 95, 100)}  # flat
    ap = Autopilot(
        service,
        _provider(feats),
        universe=["AAPL"],
        notional_per_trade=Decimal("100"),
        max_orders_per_day=8,
    )
    results = ap.run_once()
    assert len(results) == 1
    assert results[0]["side"] == "sell"


def test_run_once_honours_daily_cap(make_service):
    service = make_service()
    service.broker.set_price("MSFT", Decimal("100"))
    feats = {
        "AAPL": _features("AAPL", 105, 100, sma200=90, last_close=100),
        "MSFT": _features("MSFT", 105, 100, sma200=90, last_close=100),
    }
    ap = Autopilot(
        service,
        _provider(feats),
        universe=["AAPL", "MSFT"],
        notional_per_trade=Decimal("100"),
        max_orders_per_day=1,
    )
    results = ap.run_once()
    assert len(results) == 1
    assert service.broker.submit_calls == 1


def test_dry_run_decides_but_places_no_orders(make_service):
    service = make_service()
    feats = {"AAPL": _features("AAPL", 105, 100, sma200=90, last_close=100)}
    ap = Autopilot(
        service,
        _provider(feats),
        universe=["AAPL"],
        notional_per_trade=Decimal("100"),
        max_orders_per_day=8,
        dry_run=True,
    )
    results = ap.run_once()
    assert len(results) == 1
    assert results[0]["dry_run"] is True
    assert results[0]["side"] == "buy"
    assert service.broker.submit_calls == 0


def test_run_once_skips_flat_names_without_position(make_service):
    service = make_service()
    feats = {"AAPL": _features("AAPL", 95, 100)}  # flat, nothing held
    ap = Autopilot(
        service,
        _provider(feats),
        universe=["AAPL"],
        notional_per_trade=Decimal("100"),
        max_orders_per_day=8,
    )
    assert ap.run_once() == []
    assert service.broker.submit_calls == 0
