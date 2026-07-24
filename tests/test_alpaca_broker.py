"""AlpacaBroker + AlpacaClock mapping, with injected fake SDK clients (no network)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import TimeInForce

from trading_assistant.broker.alpaca import AlpacaBroker, AlpacaClock, _TimeoutSession
from trading_assistant.broker.base import BrokerAcceptanceUnknown, BrokerSubmissionRejected
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)


def _snap(last, bid, ask, prev_close, *, timestamp=None):
    return SimpleNamespace(
        latest_trade=SimpleNamespace(price=last, timestamp=timestamp),
        latest_quote=SimpleNamespace(
            bid_price=bid, ask_price=ask, timestamp=timestamp
        ),
        previous_daily_bar=SimpleNamespace(close=prev_close),
    )


class FakeData:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    def get_stock_snapshot(self, request):
        sym = request.symbol_or_symbols
        return {sym: self._snapshots[sym]}


class FakeCryptoData:
    def __init__(self, snapshots):
        self._snapshots = snapshots
        self.requested = []

    def get_crypto_snapshot(self, request):
        sym = request.symbol_or_symbols
        self.requested.append(sym)
        return {sym: self._snapshots[sym]}


def _api_error(status_code: int) -> APIError:
    response = requests.Response()
    response.status_code = status_code
    response._content = b'{"code":40410000,"message":"order not found"}'
    request = requests.Request("GET", "https://paper-api.alpaca.markets/v2/orders").prepare()
    response.request = request
    return APIError(
        '{"code":40410000,"message":"order not found"}',
        requests.HTTPError(response=response, request=request),
    )


class FakeOrder:
    def __init__(self, id, client_order_id, status, filled_qty="0", avg=None):
        self.id = id
        self.client_order_id = client_order_id
        self.status = SimpleNamespace(value=status)
        self.filled_qty = filled_qty
        self.filled_avg_price = avg


class FakeTrading:
    def __init__(self, existing=None, lookup_error=None, activities=None):
        self._existing = existing  # simulates a prior order for the same client id
        self._lookup_error = lookup_error
        self.activities = activities or []
        self.activity_request = None
        self.submit_calls = 0
        self.last_request = None
        self._by_id = {}

    def get_order_by_client_id(self, cid):
        if self._lookup_error is not None:
            raise self._lookup_error
        if self._existing is not None:
            return self._existing
        raise _api_error(404)

    def get(self, path, data=None):
        self.activity_request = (path, data)
        return self.activities

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
    exchange_time = datetime(2026, 7, 23, 15, 4, tzinfo=timezone.utc)
    data = FakeData(
        {"AAPL": _snap("101", "100.9", "101.1", "99", timestamp=exchange_time)}
    )
    broker = AlpacaBroker(FakeTrading(), data)
    q = broker.get_quote("aapl")
    assert q.ticker == "AAPL"
    assert q.last == Decimal("101")
    assert q.bid == Decimal("100.9")
    assert q.ask == Decimal("101.1")
    assert q.prev_close == Decimal("99")
    assert q.as_of == exchange_time


def test_get_crypto_quote_routes_to_crypto_client_and_preserves_timestamp():
    exchange_time = datetime(2026, 7, 23, 15, 5, tzinfo=timezone.utc)
    crypto = FakeCryptoData(
        {
            "BTC/USD": _snap(
                "68000", "67990", "68010", "67000", timestamp=exchange_time
            )
        }
    )
    broker = AlpacaBroker(FakeTrading(), FakeData({}), crypto)
    quote = broker.get_quote("btc/usd")

    assert crypto.requested == ["BTC/USD"]
    assert quote.ticker == "BTC/USD"
    assert quote.last == Decimal("68000")
    assert quote.as_of == exchange_time


def test_get_account_and_positions_map():
    broker = AlpacaBroker(FakeTrading(), FakeData({}))
    acct = broker.get_account()
    assert acct.buying_power == Decimal("10000")
    pos = broker.get_positions()
    assert pos[0].ticker == "AAPL" and pos[0].qty == Decimal("10")


def test_fill_activities_preserve_broker_ids_prices_and_timestamps():
    transaction_time = "2026-07-20T13:31:16.178437Z"
    trading = FakeTrading(
        activities=[
            {
                "id": "activity-1",
                "transaction_time": transaction_time,
                "price": "332.03",
                "qty": "2",
                "side": "sell_short",
                "symbol": "AAPL",
                "order_id": "order-1",
            }
        ]
    )
    broker = AlpacaBroker(trading, FakeData({}))

    fills = broker.get_fill_activities(
        after=datetime(2026, 7, 19, tzinfo=timezone.utc)
    )

    assert trading.activity_request[0] == "/account/activities/FILL"
    assert fills[0].broker_fill_id == "activity-1"
    assert fills[0].broker_order_id == "order-1"
    assert fills[0].side == "sell"
    assert fills[0].price == Decimal("332.03")
    assert fills[0].filled_at == datetime(
        2026, 7, 20, 13, 31, 16, 178437, tzinfo=timezone.utc
    )


def test_submit_market_order_builds_request_and_maps_result():
    trading = FakeTrading()
    broker = AlpacaBroker(trading, FakeData({}))
    result = broker.submit_order(_order())
    assert trading.submit_calls == 1
    assert trading.last_request.client_order_id == "k1"
    assert trading.last_request.symbol == "AAPL"
    assert trading.last_request.time_in_force is TimeInForce.DAY
    assert result.broker_order_id == "brk-1"
    assert result.status is OrderStatus.SUBMITTED  # "new" -> SUBMITTED


def test_submit_crypto_order_uses_gtc_time_in_force():
    trading = FakeTrading()
    broker = AlpacaBroker(trading, FakeData({}), FakeCryptoData({}))
    order = OrderRequest(
        ticker="BTC/USD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        idempotency_key="crypto-k1",
        notional=Decimal("25"),
    )

    broker.submit_order(order)

    assert trading.last_request.symbol == "BTC/USD"
    assert trading.last_request.time_in_force is TimeInForce.GTC


def test_crypto_bracket_order_is_rejected_locally():
    broker = AlpacaBroker(FakeTrading(), FakeData({}), FakeCryptoData({}))
    order = OrderRequest(
        ticker="BTC/USD",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        idempotency_key="crypto-bracket",
        qty=Decimal("0.001"),
        limit_price=Decimal("68000"),
    )

    with pytest.raises(ValueError, match="crypto.*bracket"):
        broker.submit_bracket(order, Decimal("70000"), Decimal("65000"))


def test_idempotent_submit_does_not_resubmit():
    prior = FakeOrder("brk-existing", "k1", "filled", filled_qty="1", avg="100")
    trading = FakeTrading(existing=prior)
    broker = AlpacaBroker(trading, FakeData({}))
    result = broker.submit_order(_order(key="k1"))
    # Existing order found -> we must NOT submit again.
    assert trading.submit_calls == 0
    assert result.broker_order_id == "brk-existing"
    assert result.status is OrderStatus.FILLED


def test_get_order_by_client_id_returns_none_or_prior_order_without_submitting():
    prior = FakeOrder("brk-existing", "k1", "filled", filled_qty="1", avg="100")
    missing = AlpacaBroker(FakeTrading(), FakeData({}))
    existing = AlpacaBroker(FakeTrading(existing=prior), FakeData({}))

    assert missing.get_order_by_client_id("not-found") is None
    found = existing.get_order_by_client_id("k1")
    assert found is not None
    assert found.broker_order_id == "brk-existing"
    assert existing._trading.submit_calls == 0


def test_alpaca_definitive_validation_rejection_is_typed():
    class RejectingTrading(FakeTrading):
        def submit_order(self, order_data):
            raise _api_error(422)

    broker = AlpacaBroker(RejectingTrading(), FakeData({}))

    with pytest.raises(BrokerSubmissionRejected):
        broker.submit_order(_order())


def test_alpaca_post_send_connection_loss_is_acceptance_unknown():
    class DisconnectingTrading(FakeTrading):
        def submit_order(self, order_data):
            raise ConnectionError("response lost")

    broker = AlpacaBroker(DisconnectingTrading(), FakeData({}))

    with pytest.raises(BrokerAcceptanceUnknown):
        broker.submit_order(_order())


def test_idempotency_lookup_failure_is_fail_closed():
    trading = FakeTrading(lookup_error=_api_error(500))
    broker = AlpacaBroker(trading, FakeData({}))

    with pytest.raises(APIError):
        broker.submit_order(_order())

    assert trading.submit_calls == 0


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


def test_get_quote_retries_on_transient_connection_error():
    """A stale keep-alive socket raises ConnectionError on first use; the broker
    must retry (fresh socket) instead of crashing the caller. This is the exact
    failure that made 'approve' 500 in the long-running app."""
    from requests.exceptions import ConnectionError as ReqConnErr

    class FlakyData:
        def __init__(self):
            self.calls = 0

        def get_stock_snapshot(self, request):
            self.calls += 1
            if self.calls == 1:  # first call: stale connection
                raise ReqConnErr("Remote end closed connection without response")
            return {request.symbol_or_symbols: _snap("101", "100.9", "101.1", "99")}

    data = FlakyData()
    broker = AlpacaBroker(FakeTrading(), data)
    q = broker.get_quote("AAPL")
    assert data.calls == 2               # retried once
    assert q.last == Decimal("101")


def test_submit_order_does_not_retry_post_send_connection_loss():
    from requests.exceptions import ConnectionError as ReqConnErr

    class FlakyTrading(FakeTrading):
        def __init__(self):
            super().__init__()
            self._first = True

        def submit_order(self, order_data):
            if self._first:  # first POST: connection dropped mid-flight
                self._first = False
                raise ReqConnErr("Remote end closed connection without response")
            return super().submit_order(order_data)

    trading = FlakyTrading()
    broker = AlpacaBroker(trading, FakeData({}))
    with pytest.raises(BrokerAcceptanceUnknown):
        broker.submit_order(_order())
    assert trading.submit_calls == 0  # no second POST after an uncertain send


def test_non_transient_error_is_not_retried():
    class ExplodingData:
        def __init__(self):
            self.calls = 0

        def get_stock_snapshot(self, request):
            self.calls += 1
            raise ValueError("bad symbol")

    data = ExplodingData()
    broker = AlpacaBroker(FakeTrading(), data)
    with pytest.raises(ValueError):
        broker.get_quote("AAPL")
    assert data.calls == 1               # not retried — only transient errors retry


def test_timeout_session_applies_default_to_every_request(monkeypatch):
    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    session = _TimeoutSession(7.5)
    session.request("GET", "https://example.test")
    assert seen["timeout"] == 7.5

    session.request("GET", "https://example.test", timeout=2)
    assert seen["timeout"] == 2


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


def test_alpaca_clock_uses_exchange_calendar_for_most_recent_open():
    trading = SimpleNamespace(
        get_calendar=lambda request: [
            SimpleNamespace(
                date=datetime(2026, 7, 2).date(),
                open=datetime(2026, 7, 2, 9, 30),
            ),
            # July 3 is absent: exchange holiday.
            SimpleNamespace(
                date=datetime(2026, 7, 6).date(),
                open=datetime(2026, 7, 6, 9, 30),
            ),
        ]
    )
    clock = AlpacaClock(trading)
    before_july_6_open = datetime(2026, 7, 6, 12, tzinfo=timezone.utc)

    assert clock.most_recent_open(before_july_6_open) == datetime(
        2026, 7, 2, 13, 30, tzinfo=timezone.utc
    )
