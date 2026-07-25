"""AlpacaBroker + AlpacaClock mapping, with injected fake SDK clients (no network)."""

from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import QueryOrderStatus, TimeInForce

from trading_assistant.assets import AssetClass
from trading_assistant.broker.alpaca import AlpacaBroker, AlpacaClock, _TimeoutSession
from trading_assistant.broker.base import (
    BrokerAcceptanceUnknown,
    BrokerDataIntegrityError,
    BrokerSubmissionRejected,
)
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trading_assistant.orders.snapshot import PortfolioSnapshotService
from trading_assistant.risk.clock import FakeClock


def _snap(
    last,
    bid,
    ask,
    prev_close,
    *,
    timestamp=None,
    trade_timestamp=None,
    book_timestamp=None,
):
    return SimpleNamespace(
        latest_trade=SimpleNamespace(
            price=last,
            timestamp=(
                trade_timestamp
                if trade_timestamp is not None
                else timestamp
            ),
        ),
        latest_quote=SimpleNamespace(
            bid_price=bid,
            ask_price=ask,
            timestamp=(
                book_timestamp
                if book_timestamp is not None
                else timestamp
            ),
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
    def __init__(
        self,
        id,
        client_order_id,
        status,
        filled_qty="0",
        avg=None,
        *,
        symbol=None,
        asset_class=None,
    ):
        self.id = id
        self.client_order_id = client_order_id
        self.status = SimpleNamespace(value=status)
        self.filled_qty = filled_qty
        self.filled_avg_price = avg
        self.symbol = symbol
        self.asset_class = (
            SimpleNamespace(value=asset_class)
            if isinstance(asset_class, str)
            else asset_class
        )


class FakeTrading:
    def __init__(self, existing=None, lookup_error=None, activities=None):
        self._existing = existing  # simulates a prior order for the same client id
        self._lookup_error = lookup_error
        self.activities = activities or []
        self.activity_request = None
        self.submit_calls = 0
        self.last_request = None
        self._by_id = {}
        self.open_orders = []
        self.order_filter = None

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

    def get_orders(self, filter):
        self.order_filter = filter
        return self.open_orders

    def cancel_order_by_id(self, oid):
        self._by_id[oid] = FakeOrder(oid, "c", "canceled")

    def get_account(self):
        return SimpleNamespace(buying_power="10000", equity="12000", cash="10000")

    def get_all_positions(self):
        return [
            SimpleNamespace(
                symbol="AAPL",
                qty="10",
                avg_entry_price="90",
                current_price="100",
                unrealized_intraday_pl="25.50",
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
    assert q.book_as_of == exchange_time
    assert q.trade_as_of == exchange_time


@pytest.mark.parametrize(
    ("last", "bid", "ask"),
    [
        (None, "100.9", "101.1"),
        ("0", "100.9", "101.1"),
        ("101", None, "101.1"),
        ("101", "0", "101.1"),
        ("101", "100.9", None),
        ("101", "100.9", "0"),
    ],
)
def test_get_quote_rejects_missing_or_zero_price_components(last, bid, ask):
    exchange_time = datetime(2026, 7, 23, 15, 4, tzinfo=timezone.utc)
    broker = AlpacaBroker(
        FakeTrading(),
        FakeData(
            {
                "AAPL": _snap(
                    last,
                    bid,
                    ask,
                    "99",
                    timestamp=exchange_time,
                )
            }
        ),
    )

    with pytest.raises(ValueError, match="invalid Alpaca quote"):
        broker.get_quote("AAPL")


@pytest.mark.parametrize("missing", ["trade", "book"])
def test_get_quote_rejects_missing_trade_or_book_payload(missing):
    exchange_time = datetime(2026, 7, 23, 15, 4, tzinfo=timezone.utc)
    snapshot = _snap(
        "101",
        "100.9",
        "101.1",
        "99",
        timestamp=exchange_time,
    )
    if missing == "trade":
        snapshot.latest_trade = None
    else:
        snapshot.latest_quote = None
    broker = AlpacaBroker(FakeTrading(), FakeData({"AAPL": snapshot}))

    with pytest.raises(ValueError, match="invalid Alpaca quote"):
        broker.get_quote("AAPL")


def test_get_quote_preserves_book_and_trade_timestamps_separately():
    trade_time = datetime(2026, 7, 23, 15, 3, 40, tzinfo=timezone.utc)
    book_time = datetime(2026, 7, 23, 15, 4, tzinfo=timezone.utc)
    broker = AlpacaBroker(
        FakeTrading(),
        FakeData(
            {
                "AAPL": _snap(
                    "101",
                    "100.9",
                    "101.1",
                    "99",
                    trade_timestamp=trade_time,
                    book_timestamp=book_time,
                )
            }
        ),
    )

    quote = broker.get_quote("AAPL")

    assert quote.book_as_of == book_time
    assert quote.trade_as_of == trade_time
    assert quote.as_of == trade_time


@pytest.mark.parametrize("missing_timestamp", ["trade", "book"])
def test_get_quote_rejects_missing_component_timestamp(missing_timestamp):
    exchange_time = datetime(2026, 7, 23, 15, 4, tzinfo=timezone.utc)
    broker = AlpacaBroker(
        FakeTrading(),
        FakeData(
            {
                "AAPL": _snap(
                    "101",
                    "100.9",
                    "101.1",
                    "99",
                    trade_timestamp=(
                        None
                        if missing_timestamp == "trade"
                        else exchange_time
                    ),
                    book_timestamp=(
                        None
                        if missing_timestamp == "book"
                        else exchange_time
                    ),
                )
            }
        ),
    )

    with pytest.raises(ValueError, match="timestamp"):
        broker.get_quote("AAPL")


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
    assert quote.book_as_of == exchange_time
    assert quote.trade_as_of == exchange_time


def test_get_account_and_positions_map():
    broker = AlpacaBroker(FakeTrading(), FakeData({}))
    acct = broker.get_account()
    assert acct.buying_power == Decimal("10000")
    assert acct.is_valid is True
    pos = broker.get_positions()
    assert pos[0].ticker == "AAPL" and pos[0].qty == Decimal("10")
    assert pos[0].unrealized_intraday_pnl == Decimal("25.50")


@pytest.mark.parametrize(
    ("raw_symbol", "raw_asset_class", "expected_symbol"),
    [
        ("BTCUSD", "crypto", "BTC/USD"),
        ("BTC/USD", "crypto", "BTC/USD"),
        ("ACMEUSD", "us_equity", "ACMEUSD"),
    ],
)
def test_get_positions_canonicalizes_only_metadata_identified_crypto(
    raw_symbol,
    raw_asset_class,
    expected_symbol,
):
    trading = FakeTrading()
    trading.get_all_positions = lambda: [
        SimpleNamespace(
            symbol=raw_symbol,
            asset_class=SimpleNamespace(value=raw_asset_class),
            qty="2",
            avg_entry_price="90",
            current_price="100",
            unrealized_intraday_pl="5",
        )
    ]

    position = AlpacaBroker(trading, FakeData({})).get_positions()[0]

    assert position.ticker == expected_symbol


@pytest.mark.parametrize("second_qty", ["2", "3"], ids=["identical", "conflicting"])
def test_get_positions_rejects_duplicate_canonical_crypto_symbols(
    second_qty,
):
    trading = FakeTrading()
    trading.get_all_positions = lambda: [
        SimpleNamespace(
            symbol="BTCUSD",
            asset_class=SimpleNamespace(value="crypto"),
            qty="2",
            avg_entry_price="60000",
            current_price="68000",
            unrealized_intraday_pl="1000",
        ),
        SimpleNamespace(
            symbol="BTC/USD",
            asset_class=SimpleNamespace(value="crypto"),
            qty=second_qty,
            avg_entry_price="60000",
            current_price="68000",
            unrealized_intraday_pl="1000",
        ),
    ]

    with pytest.raises(
        BrokerDataIntegrityError,
        match="duplicate Alpaca position.*BTC/USD",
    ):
        AlpacaBroker(trading, FakeData({})).get_positions()


def test_compact_crypto_position_is_single_canonical_snapshot_exposure(
    session_factory,
    app_config,
):
    captured_at = datetime.now(timezone.utc)
    trading = FakeTrading()
    trading.get_all_positions = lambda: [
        SimpleNamespace(
            symbol="BTCUSD",
            asset_class=SimpleNamespace(value="crypto"),
            qty="2",
            avg_entry_price="60000",
            current_price="68000",
            unrealized_intraday_pl="1000",
        )
    ]
    crypto_data = FakeCryptoData(
        {
            "BTC/USD": _snap(
                "70000",
                "69990",
                "70010",
                "69000",
                timestamp=captured_at,
            )
        }
    )
    broker = AlpacaBroker(trading, FakeData({}), crypto_data)
    snapshots = PortfolioSnapshotService(
        session_factory,
        broker,
        lambda _asset_class: FakeClock(is_open=True),
        lambda: {},
        risk_config_for_asset=lambda asset_class: (
            app_config.crypto_risk
            if asset_class is AssetClass.CRYPTO
            else app_config.risk
        ),
        now=lambda: captured_at,
    )

    snapshot = snapshots.assemble_for_execution("BTC/USD")

    assert set(snapshot.positions) == {"BTC/USD"}
    assert snapshot.position_value("BTC/USD") == Decimal("140000")
    assert snapshot.gross_exposure() == Decimal("140000")
    assert snapshot.quote_fresh is True
    assert crypto_data.requested == ["BTC/USD"]


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ("qty", None),
        ("qty", "NaN"),
        ("qty", "Infinity"),
        ("avg_entry_price", None),
        ("avg_entry_price", "NaN"),
        ("avg_entry_price", "Infinity"),
        ("avg_entry_price", "0"),
        ("current_price", None),
        ("current_price", "NaN"),
        ("current_price", "-Infinity"),
        ("current_price", "0"),
    ],
)
def test_get_positions_rejects_missing_or_invalid_risk_fields(
    field,
    raw_value,
):
    trading = FakeTrading()
    values = {
        "symbol": "AAPL",
        "qty": "10",
        "avg_entry_price": "90",
        "current_price": "100",
        "unrealized_intraday_pl": "25.50",
    }
    values[field] = raw_value
    trading.get_all_positions = lambda: [SimpleNamespace(**values)]

    with pytest.raises(ValueError, match="invalid Alpaca position"):
        AlpacaBroker(trading, FakeData({})).get_positions()


@pytest.mark.parametrize("symbol", [None, "", "   "])
def test_get_positions_rejects_missing_symbol(symbol):
    trading = FakeTrading()
    trading.get_all_positions = lambda: [
        SimpleNamespace(
            symbol=symbol,
            qty="10",
            avg_entry_price="90",
            current_price="100",
            unrealized_intraday_pl="25.50",
        )
    ]

    with pytest.raises(ValueError, match="invalid Alpaca position"):
        AlpacaBroker(trading, FakeData({})).get_positions()


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ("buying_power", None),
        ("equity", None),
        ("cash", None),
        ("buying_power", "NaN"),
        ("equity", "Infinity"),
        ("cash", "-Infinity"),
        ("buying_power", "0"),
        ("equity", "-1"),
        ("cash", "0"),
    ],
)
def test_get_account_marks_missing_nonfinite_or_nonpositive_fields_invalid(
    field,
    raw_value,
):
    trading = FakeTrading()
    values = {
        "buying_power": "10000",
        "equity": "12000",
        "cash": "10000",
    }
    values[field] = raw_value
    trading.get_account = lambda: SimpleNamespace(**values)

    account = AlpacaBroker(trading, FakeData({})).get_account()

    assert account.is_valid is False


@pytest.mark.parametrize("raw_pnl", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_position_pnl_maps_to_missing(raw_pnl):
    trading = FakeTrading()
    trading.get_all_positions = lambda: [
        SimpleNamespace(
            symbol="AAPL",
            qty="10",
            avg_entry_price="90",
            current_price="100",
            unrealized_intraday_pl=raw_pnl,
        )
    ]

    position = AlpacaBroker(trading, FakeData({})).get_positions()[0]

    assert position.unrealized_intraday_pnl is None


def test_fill_activities_preserve_broker_ids_prices_and_timestamps():
    transaction_time = "2026-07-20T13:31:16.178437Z"
    trading = FakeTrading(
        activities=[
            {
                "id": "activity-1",
                "transaction_time": transaction_time,
                "price": "999999.999999999",
                "qty": "0.123456789",
                "side": "sell_short",
                "symbol": "BTCUSD",
                "order_id": "order-1",
            }
        ]
    )
    broker = AlpacaBroker(trading, FakeData({}))

    boundary = datetime(2026, 7, 19, tzinfo=timezone.utc)
    fills = broker.get_fill_activities(after=boundary)

    assert trading.activity_request[0] == "/account/activities/FILL"
    requested_after = datetime.fromisoformat(
        trading.activity_request[1]["after"].replace("Z", "+00:00")
    )
    assert requested_after == boundary - timedelta(seconds=1)
    assert fills[0].broker_fill_id == "activity-1"
    assert fills[0].broker_order_id == "order-1"
    assert fills[0].side == "sell"
    assert fills[0].ticker == "BTCUSD"
    assert fills[0].qty == Decimal("0.123456789")
    assert fills[0].price == Decimal("999999.999999999")
    assert fills[0].filled_at == datetime(
        2026, 7, 20, 13, 31, 16, 178437, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("raw_symbol", "raw_asset_class", "expected_symbol"),
    [
        ("BTCUSD", "crypto", "BTC/USD"),
        ("BTC/USD", "crypto", "BTC/USD"),
        ("ACMEUSD", "us_equity", "ACMEUSD"),
    ],
)
def test_fill_activities_canonicalize_only_metadata_identified_crypto(
    raw_symbol,
    raw_asset_class,
    expected_symbol,
):
    activity = {
        "id": f"activity-{raw_symbol}",
        "transaction_time": "2026-07-20T13:31:16Z",
        "price": "100",
        "qty": "1",
        "side": "buy",
        "symbol": raw_symbol,
        "asset_class": raw_asset_class,
        "order_id": "order-1",
    }

    fill = AlpacaBroker(
        FakeTrading(activities=[activity]),
        FakeData({}),
    ).get_fill_activities()[0]

    assert fill.ticker == expected_symbol


@pytest.mark.parametrize(
    "raw_id",
    [None, "", "   ", pytest.param(..., id="missing")],
)
def test_fill_activities_reject_missing_or_blank_activity_identity(raw_id):
    activity = {
        "id": raw_id,
        "transaction_time": "2026-07-20T13:31:16Z",
        "price": "100",
        "qty": "1",
        "side": "buy",
        "symbol": "AAPL",
        "order_id": "order-1",
    }
    if raw_id is ...:
        activity.pop("id")
    trading = FakeTrading(activities=[activity])

    with pytest.raises(
        BrokerDataIntegrityError,
        match="fill activity identity",
    ):
        AlpacaBroker(trading, FakeData({})).get_fill_activities()


def test_fill_activities_reject_unknown_side():
    trading = FakeTrading(
        activities=[
            {
                "id": "activity-invalid-side",
                "transaction_time": "2026-07-20T13:31:16Z",
                "price": "100",
                "qty": "1",
                "side": "exercise",
                "symbol": "AAPL",
                "order_id": "order-1",
            }
        ]
    )

    with pytest.raises(ValueError, match="side"):
        AlpacaBroker(trading, FakeData({})).get_fill_activities()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qty", "0.0000000001"),
        ("qty", "1000000"),
        ("price", "1.0000000001"),
        ("price", "1000000"),
    ],
)
def test_fill_activities_reject_unpersistable_economics(field, value):
    activity = {
        "id": f"activity-invalid-{field}",
        "transaction_time": "2026-07-20T13:31:16Z",
        "price": "100",
        "qty": "1",
        "side": "buy",
        "symbol": "AAPL",
        "order_id": "order-1",
    }
    activity[field] = value

    with pytest.raises(
        BrokerDataIntegrityError,
        match="fill quantity or price",
    ):
        AlpacaBroker(
            FakeTrading(activities=[activity]),
            FakeData({}),
        ).get_fill_activities()


@pytest.mark.parametrize(
    "filled_qty",
    [
        "NaN",
        "Infinity",
        "-Infinity",
        "-0.000001",
        "0.0000000001",
    ],
)
def test_order_mapping_rejects_invalid_cumulative_filled_qty(filled_qty):
    prior = FakeOrder(
        "brk-invalid-cumulative",
        "k1",
        "canceled",
        filled_qty=filled_qty,
    )
    broker = AlpacaBroker(FakeTrading(existing=prior), FakeData({}))

    with pytest.raises(ValueError, match="filled_qty"):
        broker.get_order_by_client_id("k1")


def test_order_mapping_does_not_stringify_missing_broker_identity():
    prior = FakeOrder(
        None,
        "k1",
        "new",
    )
    broker = AlpacaBroker(FakeTrading(existing=prior), FakeData({}))

    mapped = broker.get_order_by_client_id("k1")

    assert mapped is not None
    assert mapped.broker_order_id is None


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


def test_submit_order_preserves_malformed_broker_truth_and_broker_id():
    class MalformedSubmitTrading(FakeTrading):
        def submit_order(self, order_data):
            self.submit_calls += 1
            self.last_request = order_data
            return FakeOrder(
                "brk-malformed-simple",
                order_data.client_order_id,
                "new",
                filled_qty="NaN",
            )

    broker = AlpacaBroker(MalformedSubmitTrading(), FakeData({}))

    with pytest.raises(BrokerDataIntegrityError) as exc_info:
        broker.submit_order(_order())

    assert exc_info.value.broker_order_id == "brk-malformed-simple"


def test_submit_bracket_preserves_malformed_broker_truth_and_broker_id():
    class MalformedBracketTrading(FakeTrading):
        def submit_order(self, order_data):
            self.submit_calls += 1
            self.last_request = order_data
            return FakeOrder(
                "brk-malformed-bracket",
                order_data.client_order_id,
                "new",
                filled_qty="Infinity",
            )

    broker = AlpacaBroker(MalformedBracketTrading(), FakeData({}))
    order = _order(
        key="malformed-bracket",
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
    )

    with pytest.raises(BrokerDataIntegrityError) as exc_info:
        broker.submit_bracket(order, Decimal("110"), Decimal("95"))

    assert exc_info.value.broker_order_id == "brk-malformed-bracket"


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


@pytest.mark.parametrize(
    ("raw_symbol", "raw_asset_class", "expected_symbol"),
    [
        ("BTCUSD", "crypto", "BTC/USD"),
        ("BTC/USD", "crypto", "BTC/USD"),
        ("ACMEUSD", "us_equity", "ACMEUSD"),
    ],
)
def test_order_status_canonicalizes_only_metadata_identified_crypto(
    raw_symbol,
    raw_asset_class,
    expected_symbol,
):
    trading = FakeTrading()
    trading._by_id["brk-status"] = FakeOrder(
        "brk-status",
        "client-status",
        "new",
        symbol=raw_symbol,
        asset_class=raw_asset_class,
    )

    result = AlpacaBroker(trading, FakeData({})).get_order_status(
        "brk-status"
    )

    assert result.ticker == expected_symbol


def test_get_open_orders_requests_open_only_and_maps_results():
    trading = FakeTrading()
    trading.open_orders = [
        FakeOrder("brk-open", "client-open", "partially_filled", filled_qty="0.5")
    ]
    broker = AlpacaBroker(trading, FakeData({}))

    result = broker.get_open_orders()

    assert trading.order_filter.status is QueryOrderStatus.OPEN
    assert result[0].broker_order_id == "brk-open"
    assert result[0].status is OrderStatus.PARTIALLY_FILLED


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


def test_get_quote_retries_on_transient_connection_error(caplog):
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
                raise ReqConnErr(
                    "provider-secret-transient-connection"
                )
            exchange_time = datetime(
                2026, 7, 23, 15, 4, tzinfo=timezone.utc
            )
            return {
                request.symbol_or_symbols: _snap(
                    "101",
                    "100.9",
                    "101.1",
                    "99",
                    timestamp=exchange_time,
                )
            }

    data = FlakyData()
    broker = AlpacaBroker(FakeTrading(), data)
    q = broker.get_quote("AAPL")
    assert data.calls == 2               # retried once
    assert q.last == Decimal("101")
    assert "provider-secret-transient-connection" not in caplog.text


def test_submit_order_does_not_retry_post_send_connection_loss():
    from requests.exceptions import ConnectionError as ReqConnErr

    class FlakyTrading(FakeTrading):
        def __init__(self):
            super().__init__()
            self._first = True

        def submit_order(self, order_data):
            if self._first:  # first POST: connection dropped mid-flight
                self._first = False
                raise ReqConnErr(
                    "provider-secret-submit-connection"
                )
            return super().submit_order(order_data)

    trading = FlakyTrading()
    broker = AlpacaBroker(trading, FakeData({}))
    with pytest.raises(
        BrokerAcceptanceUnknown,
        match="broker submission acceptance is unknown",
    ) as caught:
        broker.submit_order(_order())
    assert "provider-secret-submit-connection" not in str(caught.value)
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


@pytest.mark.parametrize(
    "raw_is_open",
    ["false", 0, 1, None],
    ids=["string-false", "integer-zero", "integer-one", "none"],
)
def test_alpaca_clock_rejects_non_boolean_market_state_without_provider_detail(
    raw_is_open,
):
    marker = "provider-secret-invalid-market-state"
    clock = AlpacaClock(
        SimpleNamespace(
            provider_marker=marker,
            get_clock=lambda: SimpleNamespace(is_open=raw_is_open),
        )
    )

    with pytest.raises(BrokerDataIntegrityError) as raised:
        clock.is_open()

    assert str(raised.value) == "invalid Alpaca market clock state"
    assert marker not in str(raised.value)
    assert type(raw_is_open).__name__ not in str(raised.value)


def _market_session(
    session_date: date,
    *,
    opens_at: dt_time = dt_time(9, 30),
    closes_at: dt_time = dt_time(16),
):
    return SimpleNamespace(
        date=session_date,
        open=datetime.combine(session_date, opens_at),
        close=datetime.combine(session_date, closes_at),
    )


def test_alpaca_clock_uses_exchange_calendar_for_most_recent_open():
    trading = SimpleNamespace(
        get_calendar=lambda request: [
            _market_session(date(2026, 7, 2)),
            # July 3 is absent: exchange holiday.
            _market_session(date(2026, 7, 6)),
        ]
    )
    clock = AlpacaClock(trading)
    before_july_6_open = datetime(2026, 7, 6, 12, tzinfo=timezone.utc)

    assert clock.most_recent_open(before_july_6_open) == datetime(
        2026, 7, 2, 13, 30, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("observed_at", "later_provider_state", "expected_open"),
    [
        (
            datetime(2026, 7, 24, 13, 29, 59, tzinfo=timezone.utc),
            True,
            False,
        ),
        (
            datetime(2026, 7, 24, 19, 59, 59, tzinfo=timezone.utc),
            False,
            True,
        ),
    ],
    ids=["pre-open-provider-post-open", "pre-close-provider-post-close"],
)
def test_alpaca_clock_historical_state_uses_one_calendar_observation_not_later_current_clock(
    observed_at,
    later_provider_state,
    expected_open,
):
    calls = {"calendar": 0, "clock": 0}

    def get_calendar(_request):
        calls["calendar"] += 1
        return [
            _market_session(date(2026, 7, 23)),
            _market_session(date(2026, 7, 24)),
        ]

    def get_clock():
        calls["clock"] += 1
        return SimpleNamespace(is_open=later_provider_state)

    clock = AlpacaClock(
        SimpleNamespace(get_calendar=get_calendar, get_clock=get_clock)
    )

    observation = clock.observe(observed_at)

    assert observation.is_open is expected_open
    assert observation.most_recent_open == (
        datetime(2026, 7, 24, 13, 30, tzinfo=timezone.utc)
        if expected_open
        else datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    )
    assert calls == {"calendar": 1, "clock": 0}


def test_alpaca_clock_historical_state_ignores_malformed_later_current_state():
    clock = AlpacaClock(
        SimpleNamespace(
            get_clock=lambda: SimpleNamespace(is_open="provider-secret-state"),
            get_calendar=lambda _request: [
                _market_session(date(2026, 7, 23)),
                _market_session(date(2026, 7, 24)),
            ],
        )
    )

    assert clock.is_open(
        datetime(2026, 7, 24, 18, tzinfo=timezone.utc)
    ) is True

    with pytest.raises(BrokerDataIntegrityError):
        clock.is_open()


@pytest.mark.parametrize(
    ("observed_at", "expected_open"),
    [
        (
            datetime(2026, 3, 6, 14, 30, tzinfo=timezone.utc),
            datetime(2026, 3, 6, 14, 30, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc),
        ),
    ],
    ids=["standard-time", "daylight-time"],
)
def test_alpaca_clock_calendar_observation_applies_exchange_timezone_dst(
    observed_at,
    expected_open,
):
    sessions = [
        _market_session(date(2026, 3, 6)),
        _market_session(date(2026, 3, 9)),
    ]

    def get_calendar(request):
        return [
            session
            for session in sessions
            if request.start <= session.date <= request.end
        ]

    clock = AlpacaClock(
        SimpleNamespace(get_calendar=get_calendar)
    )

    observation = clock.observe(observed_at)

    assert observation.is_open is True
    assert observation.most_recent_open == expected_open


def test_alpaca_clock_calendar_observation_treats_omitted_holiday_as_closed():
    clock = AlpacaClock(
        SimpleNamespace(
            get_calendar=lambda _request: [
                _market_session(date(2026, 7, 2)),
                # July 3 is omitted by the official exchange calendar.
            ]
        )
    )

    observation = clock.observe(
        datetime(2026, 7, 3, 15, tzinfo=timezone.utc)
    )

    assert observation.is_open is False
    assert observation.most_recent_open == datetime(
        2026, 7, 2, 13, 30, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "sessions",
    [
        [
            SimpleNamespace(
                open=datetime(2026, 7, 24, 9, 30),
                close=datetime(2026, 7, 24, 16),
            )
        ],
        [
            SimpleNamespace(
                date=date(2026, 7, 24),
                open="provider-secret-open",
                close=datetime(2026, 7, 24, 16),
            )
        ],
        [
            SimpleNamespace(
                date=date(2026, 7, 24),
                open=datetime(2026, 7, 24, 9, 30),
                close="provider-secret-close",
            )
        ],
        [
            _market_session(
                date(2026, 7, 24),
                opens_at=dt_time(16),
                closes_at=dt_time(9, 30),
            )
        ],
        [
            SimpleNamespace(
                date=date(2026, 7, 24),
                open=datetime(2026, 7, 23, 9, 30),
                close=datetime(2026, 7, 23, 16),
            )
        ],
        [
            _market_session(date(2026, 7, 24)),
            _market_session(date(2026, 7, 24)),
        ],
    ],
    ids=[
        "missing-date",
        "malformed-open",
        "malformed-close",
        "reversed",
        "date-mismatch",
        "duplicate-date",
    ],
)
def test_alpaca_clock_rejects_malformed_calendar_sessions_without_detail(
    sessions,
):
    clock = AlpacaClock(
        SimpleNamespace(get_calendar=lambda _request: sessions)
    )

    with pytest.raises(BrokerDataIntegrityError) as raised:
        clock.observe(datetime(2026, 7, 24, 18, tzinfo=timezone.utc))

    assert str(raised.value) == "invalid Alpaca market calendar"
    assert "provider-secret" not in str(raised.value)


def test_alpaca_clock_rejects_non_aware_observation_without_detail():
    clock = AlpacaClock(
        SimpleNamespace(
            get_calendar=lambda _request: [
                _market_session(date(2026, 7, 24))
            ]
        )
    )

    with pytest.raises(BrokerDataIntegrityError) as raised:
        clock.observe(datetime(2026, 7, 24, 18))

    assert str(raised.value) == "invalid Alpaca market calendar"
