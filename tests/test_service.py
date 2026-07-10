"""Phase 2 integration: propose creates a PENDING order and NEVER executes."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import Position
from trading_assistant.db.models import Order, RiskEvent
from trading_assistant.risk.clock import FakeClock
from trading_assistant.service import TradingService


class SpyBroker(MockBroker):
    """MockBroker that records whether an order was ever sent to the broker."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.submit_calls = 0

    def submit_order(self, order):
        self.submit_calls += 1
        return super().submit_order(order)


def _service(app_config, session_factory, broker=None, market_open=True):
    broker = broker or SpyBroker()
    broker.set_price("AAPL", Decimal("100"))
    return TradingService(
        broker, session_factory, app_config, FakeClock(is_open=market_open)
    )


def test_propose_creates_pending_and_does_not_execute(app_config, session_factory):
    svc = _service(app_config, session_factory)
    res = svc.propose_order("AAPL", "buy", "market", notional="400")

    assert res["status"] == "proposed"
    assert res["approved_by_risk"] is True
    assert res["executed"] is False
    # The core Phase-2 guarantee: nothing was sent to the broker.
    assert svc.broker.submit_calls == 0

    open_orders = svc.get_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0]["status"] == "proposed"


def test_rejected_order_is_persisted_with_reason(app_config, session_factory):
    svc = _service(app_config, session_factory)
    res = svc.propose_order("AAPL", "buy", "market", notional="600")  # > $500 limit

    assert res["status"] == "rejected"
    assert res["approved_by_risk"] is False
    assert any("per order" in r for r in res["risk_reasons"])
    assert svc.broker.submit_calls == 0

    with session_factory() as s:
        assert s.execute(select(func.count()).select_from(RiskEvent)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(Order)).scalar_one() == 1


def test_disallowed_ticker_rejected(app_config, session_factory):
    svc = _service(app_config, session_factory)
    svc.broker.set_price("TSLA", Decimal("100"))
    res = svc.propose_order("TSLA", "buy", "market", notional="100")
    assert res["status"] == "rejected"
    assert any("allowlist" in r for r in res["risk_reasons"])
    assert svc.broker.submit_calls == 0


def test_market_closed_rejects(app_config, session_factory):
    svc = _service(app_config, session_factory, market_open=False)
    res = svc.propose_order("AAPL", "buy", "market", notional="100")
    assert res["status"] == "rejected"
    assert any("market is closed" in r for r in res["risk_reasons"])


def test_snapshot_uses_broker_positions(app_config, session_factory):
    broker = SpyBroker(
        positions=[Position("AAPL", Decimal("19"), Decimal("100"), Decimal("100"))]
    )
    svc = _service(app_config, session_factory, broker=broker)
    # Existing $1900 position + $500 order -> $2400 > $2000 per-ticker limit.
    res = svc.propose_order("AAPL", "buy", "market", notional="500")
    assert res["status"] == "rejected"
    assert any("per ticker" in r for r in res["risk_reasons"])


def test_conditional_rule_crud(app_config, session_factory):
    svc = _service(app_config, session_factory)
    created = svc.create_conditional_rule(
        "AAPL", {"price_below": 175}, {"side": "buy", "notional": "50"}
    )
    assert created["state"] == "active"
    assert svc.list_rules()[0]["condition"] == {"price_below": 175}

    canceled = svc.cancel_rule(created["rule_id"])
    assert canceled["canceled"] is True
    assert svc.list_rules()[0]["state"] == "canceled"


def test_market_data_and_account_summary(app_config, session_factory):
    svc = _service(app_config, session_factory)
    md = svc.get_market_data("AAPL")
    assert md["ticker"] == "AAPL"
    assert md["last"] == "100"

    summary = svc.get_account_summary()
    assert "buying_power" in summary
    assert isinstance(summary["positions"], list)
