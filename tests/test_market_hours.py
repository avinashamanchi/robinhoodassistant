"""A7: market-hours check driven entirely by an injectable clock."""

from __future__ import annotations

from decimal import Decimal

from trading_assistant.broker.models import OrderRequest, OrderSide, OrderType
from trading_assistant.risk.clock import FakeClock, MarketClock
from trading_assistant.risk.market_hours import is_market_open
from trading_assistant.risk.rules import check_market_hours


def _order() -> OrderRequest:
    return OrderRequest(
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        idempotency_key="k",
        notional=Decimal("100"),
    )


def test_fake_clock_satisfies_protocol():
    assert isinstance(FakeClock(), MarketClock)


def test_is_market_open_follows_clock():
    clock = FakeClock(is_open=True)
    assert is_market_open(clock) is True
    clock.set_open(False)
    assert is_market_open(clock) is False


def test_rule_rejects_when_closed(risk_config):
    assert check_market_hours(_order(), risk_config, market_open=False) is not None
    assert check_market_hours(_order(), risk_config, market_open=True) is None


def test_rule_allows_closed_when_config_permits(risk_config):
    permissive = risk_config.model_copy(update={"reject_when_market_closed": False})
    assert check_market_hours(_order(), permissive, market_open=False) is None
