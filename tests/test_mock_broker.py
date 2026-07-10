"""MockBroker: deterministic quotes + idempotent order submission."""

from __future__ import annotations

from decimal import Decimal

from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)


def _order(key: str) -> OrderRequest:
    return OrderRequest(
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        idempotency_key=key,
        notional=Decimal("100"),
    )


def test_prices_are_deterministic(mock_broker):
    assert mock_broker.price_of("AAPL") == mock_broker.price_of("AAPL")
    q = mock_broker.get_quote("AAPL")
    assert q.bid <= q.last <= q.ask
    assert q.ticker == "AAPL"


def test_set_price_overrides(mock_broker):
    mock_broker.set_price("AAPL", Decimal("123.45"))
    assert mock_broker.get_quote("AAPL").last == Decimal("123.45")


def test_idempotent_submit_does_not_double_order(mock_broker):
    first = mock_broker.submit_order(_order("key-1"))
    second = mock_broker.submit_order(_order("key-1"))  # same key
    assert first.broker_order_id == second.broker_order_id
    assert first.status is OrderStatus.SUBMITTED
    # A different key creates a distinct order.
    third = mock_broker.submit_order(_order("key-2"))
    assert third.broker_order_id != first.broker_order_id


def test_status_and_cancel(mock_broker):
    result = mock_broker.submit_order(_order("key-x"))
    fetched = mock_broker.get_order_status(result.broker_order_id)
    assert fetched.broker_order_id == result.broker_order_id
    canceled = mock_broker.cancel_order(result.broker_order_id)
    assert canceled.status is OrderStatus.CANCELED
