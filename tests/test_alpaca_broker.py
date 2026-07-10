"""AlpacaBroker + AlpacaClock mapping, with injected fake SDK clients (no network)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_assistant.broker.alpaca import AlpacaBroker, AlpacaClock
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)


def _snap(last, bid, ask, prev_close):
    return SimpleNamespace(
        latest_trade=SimpleNamespace(price=last),
        latest_quote=SimpleNamespace(bid_price=bid, ask_price=ask),
        previous_daily_bar=SimpleNamespace(close=prev_close),
    )


class FakeData:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    def get_stock_snapshot(self, request):
        sym = request.symbol_or_symbols
        return {sym: self._snapshots[sym]}


class FakeOrder:
    def __init__(self, id, client_order_id, status, filled_qty="0", avg=None):
        self.id = id
        self.client_order_id = client_order_id
        self.status = SimpleNamespace(value=status)
        self.filled_qty = filled_qty
        self.filled_avg_price = avg


class FakeTrading:
    def __init__(self, existing=None):
        self._existing = existing  # simulates a prior order for the same client id
        self.submit_calls = 0
        self.last_request = None
        self._by_id = {}

    def get_order_by_client_order_id(self, cid):
        if self._existing is not None:
            return self._existing
        raise Exception("order not found")

    def submit_order(self, order_data):
        self.submit_calls += 1
        self.last_request = order_data
        order = FakeOrder("brk-1", order_data.client_order_id, "new")
        self._by_id["brk-1"] = order
        return order

    def get_order_by_id(self, oid):
        return self._by_id[oid]

    def cancel_order_by_id(self, oid):
        self._by_id[oid] = FakeOrder(oid, "c", "canceled")

    def get_account(self):
        return SimpleNamespace(buying_power="10000", equity="12000", cash="10000")

    def get_all_positions(self):
        return [
            SimpleNamespace(
                symbol="AAPL", qty="10", avg_entry_price="90", current_price="100"
            )
        ]


def _order(key="k1", order_type=OrderType.MARKET, limit_price=None):
    return OrderRequest(
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=order_type,
        idempotency_key=key,
        notional=Decimal("100") if order_type is OrderType.MARKET else None,
        qty=Decimal("1") if order_type is OrderType.LIMIT else None,
        limit_price=limit_price,
    )


def test_get_quote_maps_snapshot():
    data = FakeData({"AAPL": _snap("101", "100.9", "101.1", "99")})
    broker = AlpacaBroker(FakeTrading(), data)
    q = broker.get_quote("aapl")
    assert q.ticker == "AAPL"
    assert q.last == Decimal("101")
    assert q.bid == Decimal("100.9")
    assert q.ask == Decimal("101.1")
    assert q.prev_close == Decimal("99")


def test_get_account_and_positions_map():
    broker = AlpacaBroker(FakeTrading(), FakeData({}))
    acct = broker.get_account()
    assert acct.buying_power == Decimal("10000")
    pos = broker.get_positions()
    assert pos[0].ticker == "AAPL" and pos[0].qty == Decimal("10")


def test_submit_market_order_builds_request_and_maps_result():
    trading = FakeTrading()
    broker = AlpacaBroker(trading, FakeData({}))
    result = broker.submit_order(_order())
    assert trading.submit_calls == 1
    assert trading.last_request.client_order_id == "k1"
    assert trading.last_request.symbol == "AAPL"
    assert result.broker_order_id == "brk-1"
    assert result.status is OrderStatus.SUBMITTED  # "new" -> SUBMITTED


def test_idempotent_submit_does_not_resubmit():
    prior = FakeOrder("brk-existing", "k1", "filled", filled_qty="1", avg="100")
    trading = FakeTrading(existing=prior)
    broker = AlpacaBroker(trading, FakeData({}))
    result = broker.submit_order(_order(key="k1"))
    # Existing order found -> we must NOT submit again.
    assert trading.submit_calls == 0
    assert result.broker_order_id == "brk-existing"
    assert result.status is OrderStatus.FILLED


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("new", OrderStatus.SUBMITTED),
        ("partially_filled", OrderStatus.PARTIALLY_FILLED),
        ("filled", OrderStatus.FILLED),
        ("canceled", OrderStatus.CANCELED),
        ("expired", OrderStatus.EXPIRED),
        ("rejected", OrderStatus.REJECTED),
        ("something_new", OrderStatus.SUBMITTED),  # unknown -> safe default
    ],
)
def test_status_mapping(raw, expected):
    from trading_assistant.broker.alpaca import _map_status

    assert _map_status(SimpleNamespace(value=raw)) is expected


def test_alpaca_clock_maps():
    clock = AlpacaClock(
        SimpleNamespace(
            get_clock=lambda: SimpleNamespace(
                is_open=True, next_open="2026-01-02T09:30", next_close="2026-01-02T16:00"
            )
        )
    )
    assert clock.is_open() is True
    assert clock.next_open() == "2026-01-02T09:30"
    assert clock.next_close() == "2026-01-02T16:00"
