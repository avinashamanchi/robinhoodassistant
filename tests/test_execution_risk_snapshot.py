from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event

import trading_assistant.orders.snapshot as snapshot_module
from trading_assistant.assets import AssetClass
from trading_assistant.broker.alpaca import AlpacaClock
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    Account,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
)
from trading_assistant.db.models import AccountRiskState, Fill, Order, utcnow
from trading_assistant.dependencies import (
    RequiredDependencyUnavailable,
    RequiredQuoteUnavailable,
)
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.risk.engine import RiskEngine


NOW = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)


class _FailingProviderClock:
    def __init__(self, failure_method: str, marker: str) -> None:
        self.failure_method = failure_method
        self.marker = marker

    def is_open(self, at=None):
        if self.failure_method == "is_open":
            raise ConnectionError(self.marker)
        return True

    def most_recent_open(self, at=None):
        if self.failure_method == "most_recent_open":
            raise ConnectionError(self.marker)
        return NOW - timedelta(hours=2)

    def next_open(self, at=None):  # pragma: no cover - contract guard
        raise AssertionError("snapshot must not request the next market open")

    def next_close(self, at=None):  # pragma: no cover - contract guard
        raise AssertionError("snapshot must not request the next market close")


class _ObservedClock:
    def __init__(self, boundary: datetime) -> None:
        self.boundary = boundary
        self.observations: list[tuple[str, datetime | None]] = []

    def is_open(self, at=None):
        self.observations.append(("is_open", at))
        return True

    def most_recent_open(self, at=None):
        self.observations.append(("most_recent_open", at))
        return self.boundary

    def next_open(self, at=None):  # pragma: no cover - contract guard
        raise AssertionError("snapshot must not request the next market open")

    def next_close(self, at=None):  # pragma: no cover - contract guard
        raise AssertionError("snapshot must not request the next market close")


def _seed_realized_loss(service) -> None:
    with service.session_factory() as session:
        session.add_all(
            [
                Fill(
                    ticker="AAPL",
                    side="buy",
                    qty=Decimal("100"),
                    price=Decimal("100"),
                    broker_fill_id="clock-loss-open",
                    filled_at=NOW - timedelta(hours=2),
                ),
                Fill(
                    ticker="AAPL",
                    side="sell",
                    qty=Decimal("100"),
                    price=Decimal("50"),
                    broker_fill_id="clock-loss-close",
                    filled_at=NOW - timedelta(hours=1),
                ),
            ]
        )
        session.commit()


def _submit(submission, order_id):
    return submission.submit(
        order_id,
        actor="operator:test",
        reason="execution risk snapshot test",
        request_id=f"execution-risk-submit-{order_id}",
    )


def order(
    ticker: str = "AAPL",
    notional: str | None = "100",
    *,
    side: OrderSide = OrderSide.BUY,
    qty: str | None = None,
    order_type: OrderType = OrderType.MARKET,
    limit_price: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        ticker=ticker,
        side=side,
        order_type=order_type,
        idempotency_key=(
            f"risk-{ticker}-{side.value}-{notional}-{qty}-{limit_price}"
        ),
        notional=Decimal(notional) if notional is not None else None,
        qty=Decimal(qty) if qty is not None else None,
        limit_price=(
            Decimal(limit_price) if limit_price is not None else None
        ),
    )


def _quote_with_component_times(
    *,
    bid: Decimal = Decimal("99.90"),
    ask: Decimal = Decimal("100.10"),
    last: Decimal = Decimal("100"),
    book_as_of: datetime | None = NOW,
    trade_as_of: datetime | None = NOW,
) -> Quote:
    return Quote(
        "AAPL",
        bid,
        ask,
        last,
        as_of=NOW,
        book_as_of=book_as_of,
        trade_as_of=trade_as_of,
    )


@pytest.mark.parametrize("failure", ["unavailable", "stale"])
def test_required_execution_quote_failure_raises_typed_dependency(
    make_service,
    failure,
):
    service = make_service()
    if failure == "unavailable":
        service.broker.get_quote = lambda _ticker: (_ for _ in ()).throw(
            ConnectionError("provider quote unavailable")
        )
    else:
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        service.broker.get_quote = lambda _ticker: Quote(
            "AAPL",
            Decimal("99.90"),
            Decimal("100.10"),
            Decimal("100"),
            as_of=stale_at,
            book_as_of=stale_at,
            trade_as_of=stale_at,
        )

    with pytest.raises(RequiredQuoteUnavailable) as raised:
        service.snapshot_service.assemble_for_execution("AAPL")

    assert type(raised.value).__name__ == "RequiredQuoteUnavailable"


@pytest.mark.parametrize(
    "failure_method",
    ["is_open", "most_recent_open"],
)
def test_market_clock_provider_failure_is_typed_when_required_and_explicitly_incomplete_when_optional(
    make_service,
    risk_config,
    caplog,
    failure_method,
):
    service = make_service()
    marker = f"provider-secret-market-clock-{failure_method}"
    service._clocks[AssetClass.EQUITY] = _FailingProviderClock(
        failure_method,
        marker,
    )

    with pytest.raises(RequiredDependencyUnavailable) as raised:
        service.snapshot_service.assemble_for_execution("AAPL")

    assert str(raised.value) == "required dependency unavailable"
    assert raised.value.__cause__ is None

    with service.session_factory() as session:
        snapshot = service.assemble_snapshot(
            session,
            ["AAPL"],
            AssetClass.EQUITY,
            required_dependencies=False,
        )

    assert snapshot.market_clock_complete is False
    assert snapshot.market_open is False
    assert snapshot.daily_pnl_complete is False
    assert snapshot.realized_pnl_today == Decimal(0)
    result = RiskEngine(risk_config).check(order(), snapshot)
    assert "market clock snapshot is incomplete" in result.reasons
    assert marker not in result.reason_text()
    assert marker not in caplog.text


def test_snapshot_captures_one_observation_before_provider_io_and_passes_it_to_clock(
    make_service,
):
    events: list[str] = []
    service = make_service(quote_now=lambda: NOW)
    original_account = service.broker.get_account
    original_positions = service.broker.get_positions
    original_quote = service.broker.get_quote
    clock = _ObservedClock(NOW - timedelta(hours=3))
    now_calls = 0

    def observed_now():
        nonlocal now_calls
        now_calls += 1
        events.append("observation")
        return NOW

    def observed_account():
        events.append("account")
        return original_account()

    def observed_positions():
        events.append("positions")
        return original_positions()

    def observed_quote(ticker):
        events.append("quote")
        return original_quote(ticker)

    service.snapshot_service.now = observed_now
    service.broker.get_account = observed_account
    service.broker.get_positions = observed_positions
    service.broker.get_quote = observed_quote
    service._clocks[AssetClass.EQUITY] = clock

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")

    assert now_calls == 1
    assert events[0] == "observation"
    assert snapshot.as_of == NOW
    assert clock.observations == [
        ("is_open", NOW),
        ("most_recent_open", NOW),
    ]


@pytest.mark.parametrize(
    "boundary",
    [
        NOW + timedelta(microseconds=1),
        NOW - timedelta(days=16),
    ],
    ids=["future", "implausibly-stale"],
)
def test_required_snapshot_rejects_invalid_market_boundary_from_single_observation(
    make_service,
    boundary,
):
    service = make_service(quote_now=lambda: NOW)
    service.snapshot_service.now = lambda: NOW
    service._clocks[AssetClass.EQUITY] = _ObservedClock(boundary)
    _seed_realized_loss(service)

    with pytest.raises(RequiredDependencyUnavailable) as raised:
        service.snapshot_service.assemble_for_execution("AAPL")

    assert str(raised.value) == "required dependency unavailable"
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "boundary",
    [
        NOW + timedelta(microseconds=1),
        NOW - timedelta(days=16),
    ],
    ids=["future", "implausibly-stale"],
)
def test_optional_snapshot_never_computes_pnl_from_invalid_market_boundary(
    make_service,
    risk_config,
    monkeypatch,
    boundary,
):
    service = make_service(quote_now=lambda: NOW)
    service.snapshot_service.now = lambda: NOW
    service._clocks[AssetClass.EQUITY] = _ObservedClock(boundary)
    _seed_realized_loss(service)
    calls: list[datetime | None] = []
    original = snapshot_module.realized_pnl_today

    def observe_pnl(*args, **kwargs):
        calls.append(kwargs.get("boundary"))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        snapshot_module,
        "realized_pnl_today",
        observe_pnl,
    )
    with service.session_factory() as session:
        snapshot = service.assemble_snapshot(
            session,
            ["AAPL"],
            required_dependencies=False,
        )

    assert calls == []
    assert snapshot.market_clock_complete is False
    assert snapshot.daily_pnl_complete is False
    assert snapshot.market_open is False
    assert snapshot.realized_pnl_today == Decimal(0)
    result = RiskEngine(risk_config).check(order(), snapshot)
    assert result.rejected is True
    assert "market clock snapshot is incomplete" in result.reasons
    assert "daily P&L snapshot is incomplete" in result.reasons


def test_correct_past_market_boundary_includes_large_loss_and_rejects_risk(
    make_service,
    risk_config,
):
    service = make_service(quote_now=lambda: NOW)
    service.snapshot_service.now = lambda: NOW
    service._clocks[AssetClass.EQUITY] = _ObservedClock(
        NOW - timedelta(hours=3)
    )
    _seed_realized_loss(service)

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")
    result = RiskEngine(risk_config).check(order(), snapshot)

    assert snapshot.market_clock_complete is True
    assert snapshot.daily_pnl_complete is True
    assert snapshot.realized_pnl_today == Decimal("-5000")
    assert result.rejected is True
    assert "daily total-loss limit reached" in result.reasons


@pytest.mark.parametrize(
    "raw_is_open",
    ["false", 0, 1, None],
    ids=["string-false", "integer-zero", "integer-one", "none"],
)
def test_invalid_alpaca_market_state_is_required_dependency_failure(
    make_service,
    raw_is_open,
):
    service = make_service(quote_now=lambda: NOW)
    service.snapshot_service.now = lambda: NOW
    service._clocks[AssetClass.EQUITY] = AlpacaClock(
        SimpleNamespace(
            get_clock=lambda: SimpleNamespace(is_open=raw_is_open),
            get_calendar=lambda _request: [
                SimpleNamespace(open=NOW - timedelta(hours=3))
            ],
        )
    )

    with pytest.raises(RequiredDependencyUnavailable) as raised:
        service.snapshot_service.assemble_for_execution("AAPL")

    assert str(raised.value) == "required dependency unavailable"
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "raw_is_open",
    ["false", 0, 1, None],
    ids=["string-false", "integer-zero", "integer-one", "none"],
)
def test_invalid_alpaca_market_state_is_explicitly_incomplete_when_optional(
    make_service,
    risk_config,
    raw_is_open,
):
    service = make_service(quote_now=lambda: NOW)
    service.snapshot_service.now = lambda: NOW
    service._clocks[AssetClass.EQUITY] = AlpacaClock(
        SimpleNamespace(
            get_clock=lambda: SimpleNamespace(is_open=raw_is_open),
            get_calendar=lambda _request: [
                SimpleNamespace(open=NOW - timedelta(hours=3))
            ],
        )
    )

    with service.session_factory() as session:
        snapshot = service.assemble_snapshot(
            session,
            ["AAPL"],
            required_dependencies=False,
        )

    assert snapshot.market_clock_complete is False
    assert snapshot.daily_pnl_complete is False
    assert snapshot.market_open is False
    assert RiskEngine(risk_config).check(order(), snapshot).rejected is True


def _pending(
    session_factory,
    *,
    key: str,
    side: OrderSide,
    status: OrderStatus,
    notional: str | None = None,
    qty: str | None = None,
    order_type: OrderType = OrderType.MARKET,
    limit_price: str | None = None,
) -> int:
    with session_factory() as session:
        row = Order(
            idempotency_key=key,
            ticker="AAPL",
            side=side.value,
            order_type=order_type.value,
            status=status.value,
            notional=Decimal(notional) if notional is not None else None,
            qty=Decimal(qty) if qty is not None else None,
            limit_price=(
                Decimal(limit_price) if limit_price is not None else None
            ),
        )
        session.add(row)
        session.commit()
        return row.id


def test_execution_rejects_stale_quote(risk_config, make_snapshot):
    snapshot = make_snapshot(prices={"AAPL": Decimal("100")})
    snapshot = replace(snapshot, quote_fresh=False)

    result = RiskEngine(risk_config).check(order(), snapshot)

    assert "quote is stale" in result.reasons


def test_execution_rejects_insufficient_buying_power(risk_config, make_snapshot):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")}, buying_power=Decimal("50")
    )

    result = RiskEngine(risk_config).check(order(), snapshot)

    assert "insufficient buying power" in result.reasons


def test_quantity_market_buy_uses_higher_ask_for_buying_power(
    risk_config, make_snapshot
):
    snapshot = make_snapshot(
        buying_power=Decimal("100.50"),
    )
    snapshot = replace(
        snapshot,
        quotes={
            "AAPL": Quote(
                "AAPL",
                bid=Decimal("100"),
                ask=Decimal("101"),
                last=Decimal("100"),
                as_of=NOW,
                book_as_of=NOW,
                trade_as_of=NOW,
            )
        },
    )

    result = RiskEngine(risk_config).check(
        order(notional=None, qty="1"),
        snapshot,
    )

    assert "insufficient buying power" in result.reasons


def test_pending_quantity_market_buy_reserves_higher_ask(
    make_service, risk_config
):
    service = make_service()
    service.broker._buying_power = Decimal("200.05")
    _pending(
        service.session_factory,
        key="pending-market-qty-buy",
        side=OrderSide.BUY,
        status=OrderStatus.SUBMITTED,
        qty="1",
    )

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")
    result = RiskEngine(risk_config).check(order(notional="100"), snapshot)

    assert snapshot.pending_buy_notional_by_ticker["AAPL"] == Decimal(
        "100.100000"
    )
    assert "insufficient buying power" in result.reasons


def test_concurrent_quantity_limit_approvals_reserve_limit_price(
    make_service,
):
    service = make_service()
    service.broker._buying_power = Decimal("620")
    first = service.propose_order(
        "AAPL",
        "buy",
        "limit",
        qty="3",
        limit_price="105",
        actor="operator:test",
        reason="execution snapshot first limit proposal",
        request_id="execution-snapshot-first-limit",
    )
    second = service.propose_order(
        "AAPL",
        "buy",
        "limit",
        qty="3",
        limit_price="105",
        actor="operator:test",
        reason="execution snapshot second limit proposal",
        request_id="execution-snapshot-second-limit",
    )
    assert first["status"] == OrderStatus.PROPOSED.value
    assert second["status"] == OrderStatus.PROPOSED.value
    for order_id in (first["order_id"], second["order_id"]):
        service.order_application.approve(
            ApprovalCommand(
                order_id,
                "operator:limit-reservation",
                "independently reviewed",
                utcnow(),
                f"execution-limit-approval-{order_id}",
            )
        )

    first_submission = _submit(service.order_submission, first["order_id"])
    second_submission = _submit(service.order_submission, second["order_id"])

    assert first_submission.status is OrderStatus.REJECTED
    assert first_submission.risk_reasons == ("insufficient buying power",)
    assert second_submission.status is OrderStatus.SUBMITTED
    assert service.broker.submit_calls == 1


def test_pending_buys_across_tickers_reserve_buying_power(
    risk_config, make_snapshot
):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")},
        buying_power=Decimal("150"),
        pending_buy_notional_by_ticker={
            "AAPL": Decimal("30"),
            "MSFT": Decimal("60"),
        },
    )

    result = RiskEngine(risk_config).check(order(notional="70"), snapshot)

    assert "insufficient buying power" in result.reasons


def test_concurrent_approvals_cannot_overcommit_buying_power(make_service):
    service = make_service()
    service.broker._buying_power = Decimal("100")
    first_id = service.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="60",
        actor="operator:test",
        reason="execution buying power first proposal",
        request_id="execution-buying-power-first",
    )["order_id"]
    second_id = service.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="60",
        actor="operator:test",
        reason="execution buying power second proposal",
        request_id="execution-buying-power-second",
    )["order_id"]
    for order_id in (first_id, second_id):
        service.order_application.approve(
            ApprovalCommand(
                order_id,
                "operator:concurrent-test",
                "independently reviewed",
                utcnow(),
                f"execution-concurrent-approval-{order_id}",
            )
        )

    first = _submit(service.order_submission, first_id)
    second = _submit(service.order_submission, second_id)

    assert first.status is OrderStatus.REJECTED
    assert first.risk_reasons == ("insufficient buying power",)
    assert second.status is OrderStatus.SUBMITTED
    assert service.broker.submit_calls == 1


def test_execution_rejects_active_breakers_in_sorted_order(
    risk_config, make_snapshot
):
    snapshot = make_snapshot(prices={"AAPL": Decimal("100")})
    snapshot = replace(
        snapshot,
        active_breakers=frozenset({"liquidity:AAPL", "data:equity"}),
    )

    result = RiskEngine(risk_config).check(order(), snapshot)

    assert "active circuit breaker: data:equity,liquidity:AAPL" in result.reasons


def test_execution_rejects_unreconciled_broker(risk_config, make_snapshot):
    snapshot = replace(
        make_snapshot(prices={"AAPL": Decimal("100")}),
        broker_reconciled=False,
    )

    result = RiskEngine(risk_config).check(order(), snapshot)

    assert "broker reconciliation is not current" in result.reasons


def test_execution_rejects_incomplete_daily_pnl(risk_config, make_snapshot):
    snapshot = replace(
        make_snapshot(prices={"AAPL": Decimal("100")}),
        daily_pnl_complete=False,
    )

    result = RiskEngine(risk_config).check(order(), snapshot)

    assert "daily P&L snapshot is incomplete" in result.reasons


@pytest.mark.parametrize(
    ("realized", "unrealized"),
    [
        (Decimal("NaN"), Decimal("0")),
        (Decimal("0"), Decimal("Infinity")),
        (Decimal("-Infinity"), Decimal("0")),
    ],
)
def test_execution_rejects_non_finite_daily_pnl_without_raising(
    risk_config, make_snapshot, realized, unrealized
):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")},
        realized_pnl_today=realized,
        unrealized_pnl_today=unrealized,
        daily_pnl_complete=True,
    )

    result = RiskEngine(risk_config).check(order(), snapshot)

    assert "daily P&L snapshot is incomplete" in result.reasons


def test_execution_rejects_daily_total_loss(risk_config, make_snapshot):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")},
        realized_pnl_today=Decimal("-300"),
        unrealized_pnl_today=Decimal("-200"),
    )

    result = RiskEngine(risk_config).check(order(), snapshot)

    assert "daily total-loss limit reached" in result.reasons


def test_execution_rejects_account_drawdown(risk_config, make_snapshot):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")},
        account_high_water_mark=Decimal("1000"),
        account_equity=Decimal("900"),
    )

    result = RiskEngine(risk_config).check(order(), snapshot)

    assert "account drawdown limit reached" in result.reasons


def test_execution_rejects_wide_spread(risk_config, make_snapshot):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")},
        spread_pct_by_ticker={"AAPL": Decimal("1.01")},
    )

    result = RiskEngine(risk_config).check(order(), snapshot)

    assert "spread exceeds configured maximum" in result.reasons


def test_execution_rejects_sell_above_unreserved_position(
    risk_config, make_snapshot
):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")},
        positions=[
            Position(
                "AAPL",
                Decimal("5"),
                Decimal("90"),
                Decimal("100"),
                unrealized_intraday_pnl=Decimal("0"),
            )
        ],
        reserved_sell_qty_by_ticker={"AAPL": Decimal("4")},
    )

    result = RiskEngine(risk_config).check(
        order(notional=None, side=OrderSide.SELL, qty="2"),
        snapshot,
    )

    assert "sell quantity exceeds unreserved position" in result.reasons


def test_snapshot_fetches_provider_data_before_database_work(make_service, engine):
    sequence: list[str] = []

    class OrderedBroker(MockBroker):
        def get_account(self):
            sequence.append("account")
            return super().get_account()

        def get_positions(self):
            sequence.append("positions")
            return super().get_positions()

        def get_quote(self, ticker):
            sequence.append(f"quote:{ticker}")
            return super().get_quote(ticker)

    broker = OrderedBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)

    def record_sql(*_args):
        sequence.append("sql")

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        service.snapshot_service.assemble_for_execution("AAPL")
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    first_sql = sequence.index("sql")
    assert sequence.index("account") < first_sql
    assert sequence.index("positions") < first_sql
    assert sequence.index("quote:AAPL") < first_sql


def test_snapshot_marks_stale_quote(make_service):
    service = make_service()
    stale = Quote(
        "AAPL",
        Decimal("99.90"),
        Decimal("100.10"),
        Decimal("100"),
        as_of=datetime.now(timezone.utc) - timedelta(seconds=61),
        book_as_of=datetime.now(timezone.utc) - timedelta(seconds=61),
        trade_as_of=datetime.now(timezone.utc) - timedelta(seconds=61),
    )
    service.broker.get_quote = lambda _ticker: stale

    with pytest.raises(RequiredQuoteUnavailable):
        service.snapshot_service.assemble_for_execution("AAPL")


@pytest.mark.parametrize("missing_component", ["book", "trade"])
def test_snapshot_rejects_missing_component_timestamp(
    make_service,
    missing_component,
):
    service = make_service()
    service.snapshot_service.now = lambda: NOW
    quote = _quote_with_component_times(
        book_as_of=None if missing_component == "book" else NOW,
        trade_as_of=None if missing_component == "trade" else NOW,
    )
    service.broker.get_quote = lambda _ticker: quote

    with pytest.raises(RequiredQuoteUnavailable):
        service.snapshot_service.assemble_for_execution("AAPL")


@pytest.mark.parametrize("stale_component", ["book", "trade"])
def test_snapshot_rejects_stale_component_when_other_component_is_fresh(
    make_service,
    stale_component,
):
    service = make_service()
    service.snapshot_service.now = lambda: NOW
    stale = NOW - timedelta(seconds=61)
    quote = _quote_with_component_times(
        book_as_of=stale if stale_component == "book" else NOW,
        trade_as_of=stale if stale_component == "trade" else NOW,
    )
    service.broker.get_quote = lambda _ticker: quote

    with pytest.raises(RequiredQuoteUnavailable):
        service.snapshot_service.assemble_for_execution("AAPL")


@pytest.mark.parametrize("future_component", ["book", "trade"])
def test_snapshot_rejects_unreasonably_future_component_timestamp(
    make_service,
    future_component,
):
    service = make_service()
    service.snapshot_service.now = lambda: NOW
    future = NOW + timedelta(seconds=30)
    quote = _quote_with_component_times(
        book_as_of=future if future_component == "book" else NOW,
        trade_as_of=future if future_component == "trade" else NOW,
    )
    service.broker.get_quote = lambda _ticker: quote

    with pytest.raises(RequiredQuoteUnavailable):
        service.snapshot_service.assemble_for_execution("AAPL")


@pytest.mark.parametrize(
    ("bid", "ask", "last"),
    [
        (Decimal("0"), Decimal("100.10"), Decimal("100")),
        (Decimal("99.90"), Decimal("0"), Decimal("100")),
        (Decimal("99.90"), Decimal("100.10"), Decimal("0")),
    ],
)
def test_snapshot_rejects_zero_price_component(
    make_service,
    bid,
    ask,
    last,
):
    service = make_service()
    service.snapshot_service.now = lambda: NOW
    quote = _quote_with_component_times(bid=bid, ask=ask, last=last)
    service.broker.get_quote = lambda _ticker: quote

    with pytest.raises(RequiredQuoteUnavailable):
        service.snapshot_service.assemble_for_execution("AAPL")


def test_unrelated_stale_quote_override_does_not_stale_target_snapshot(
    make_service,
):
    service = make_service()
    fresh = service.broker.get_quote("AAPL")
    stale_unrelated = Quote(
        "MSFT",
        Decimal("99.90"),
        Decimal("100.10"),
        Decimal("100"),
        as_of=datetime.now(timezone.utc) - timedelta(seconds=61),
        book_as_of=datetime.now(timezone.utc) - timedelta(seconds=61),
        trade_as_of=datetime.now(timezone.utc) - timedelta(seconds=61),
    )

    with service.session_factory() as session:
        snapshot = service.assemble_snapshot(
            session,
            ["AAPL"],
            quote_overrides={
                "AAPL": fresh,
                "MSFT": stale_unrelated,
            },
        )

    assert snapshot.quote_fresh is True


def test_unknown_buy_consumes_exposure_without_making_ledger_unknown(
    make_service, risk_config
):
    service = make_service()
    _pending(
        service.session_factory,
        key="unknown-buy",
        side=OrderSide.BUY,
        status=OrderStatus.ACCEPTANCE_UNKNOWN,
        notional="1800",
    )

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")
    result = RiskEngine(risk_config).check(order(notional="400"), snapshot)

    assert snapshot.pending_exposure_complete is True
    assert snapshot.pending_buy_notional_by_ticker == {
        "AAPL": Decimal("1800.000000")
    }
    assert any("per ticker" in reason for reason in result.reasons)


def test_unknown_sell_reserves_quantity_and_prevents_second_exit(
    make_service, risk_config
):
    broker = MockBroker(
        positions=[
            Position(
                "AAPL",
                Decimal("5"),
                Decimal("90"),
                Decimal("100"),
                unrealized_intraday_pnl=Decimal("0"),
            )
        ]
    )
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    _pending(
        service.session_factory,
        key="unknown-sell",
        side=OrderSide.SELL,
        status=OrderStatus.ACCEPTANCE_UNKNOWN,
        qty="4",
    )

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")
    result = RiskEngine(risk_config).check(
        order(notional=None, side=OrderSide.SELL, qty="2"),
        snapshot,
    )

    assert snapshot.pending_exposure_complete is True
    assert snapshot.reserved_sell_qty_by_ticker == {
        "AAPL": Decimal("4.000000")
    }
    assert "sell quantity exceeds unreserved position" in result.reasons


def test_pending_buys_and_sells_do_not_net_reservations(make_service):
    service = make_service()
    _pending(
        service.session_factory,
        key="pending-buy",
        side=OrderSide.BUY,
        status=OrderStatus.SUBMITTING,
        notional="500",
    )
    _pending(
        service.session_factory,
        key="pending-sell",
        side=OrderSide.SELL,
        status=OrderStatus.SUBMITTED,
        qty="2",
    )

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.pending_buy_notional_by_ticker["AAPL"] == Decimal("500.000000")
    assert snapshot.reserved_sell_qty_by_ticker["AAPL"] == Decimal("2.000000")
    assert snapshot.pending_signed_notional["AAPL"] == Decimal("500.000000")


def test_null_identity_fill_never_reduces_pending_exposure_reservation(
    make_service,
):
    service = make_service()
    order_id = _pending(
        service.session_factory,
        key="pending-buy-with-legacy-fill",
        side=OrderSide.BUY,
        status=OrderStatus.SUBMITTED,
        qty="2",
    )
    with service.session_factory() as session:
        session.add(
            Fill(
                order_id=order_id,
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id=None,
                filled_at=utcnow(),
            )
        )
        session.commit()

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.pending_buy_notional_by_ticker == {
        "AAPL": Decimal("200.20000000")
    }
    assert snapshot.pending_exposure_complete is False
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False


def test_snapshot_marks_daily_pnl_incomplete_when_position_value_is_missing(
    make_service
):
    class IncompletePnlBroker(MockBroker):
        def get_positions(self):
            return [
                Position(
                    "AAPL",
                    Decimal("2"),
                    Decimal("90"),
                    Decimal("100"),
                    unrealized_intraday_pnl=None,
                )
            ]

    broker = IncompletePnlBroker()
    broker.set_price("AAPL", Decimal("100"))
    snapshot = make_service(broker=broker).snapshot_service.assemble_for_execution(
        "AAPL"
    )

    assert snapshot.daily_pnl_complete is False


@pytest.mark.parametrize(
    "position",
    [
        Position(
            "AAPL",
            Decimal("NaN"),
            Decimal("90"),
            Decimal("100"),
        ),
        Position(
            "AAPL",
            Decimal("2"),
            Decimal("Infinity"),
            Decimal("100"),
        ),
        Position(
            "AAPL",
            Decimal("2"),
            Decimal("90"),
            Decimal("0"),
        ),
        Position(
            " ",
            Decimal("2"),
            Decimal("90"),
            Decimal("100"),
        ),
    ],
)
def test_snapshot_rejects_malformed_position_values(
    make_service,
    position,
):
    class MalformedPositionBroker(MockBroker):
        def get_positions(self):
            return [position]

    broker = MalformedPositionBroker()
    broker.set_price("AAPL", Decimal("100"))

    with pytest.raises(RequiredDependencyUnavailable):
        make_service(broker=broker).snapshot_service.assemble_for_execution(
            "AAPL"
        )


@pytest.mark.parametrize(
    ("target", "first_symbol", "second_symbol", "second_qty"),
    [
        ("BTC/USD", "BTCUSD", "BTC/USD", "1"),
        ("BTC/USD", "BTCUSD", "BTC/USD", "2"),
        ("AAPL", "AAPL", "aapl", "1"),
        ("AAPL", "AAPL", "AAPL", "2"),
    ],
    ids=[
        "crypto-alias-identical",
        "crypto-alias-conflicting",
        "equity-case-identical",
        "equity-exact-conflicting",
    ],
)
def test_snapshot_blocks_canonical_duplicate_positions_without_overwrite(
    make_service,
    app_config,
    target,
    first_symbol,
    second_symbol,
    second_qty,
):
    positions = [
        Position(
            first_symbol,
            Decimal("1"),
            Decimal("90"),
            Decimal("100"),
            unrealized_intraday_pnl=Decimal("0"),
        ),
        Position(
            second_symbol,
            Decimal(second_qty),
            Decimal("90"),
            Decimal("100"),
            unrealized_intraday_pnl=Decimal("0"),
        ),
    ]

    class DuplicatePositionBroker(MockBroker):
        def get_positions(self):
            return positions

    broker = DuplicatePositionBroker()
    broker.set_price(target, Decimal("100"))
    service = make_service(broker=broker)

    snapshot = service.snapshot_service.assemble_for_execution(target)
    config = (
        app_config.crypto_risk
        if AssetClass.for_symbol(target) is AssetClass.CRYPTO
        else app_config.risk
    )
    assert config is not None
    result = RiskEngine(config).check(
        order(ticker=target, notional="100"),
        snapshot,
    )

    assert set(snapshot.positions) == {target}
    assert snapshot.positions[target].qty == Decimal("1")
    assert snapshot.pending_exposure_complete is False
    assert snapshot.broker_reconciled is False
    assert snapshot.daily_pnl_complete is False
    assert (
        "outstanding order exposure is unknown; new orders are blocked"
        in result.reasons
    )


def test_snapshot_marks_non_finite_unrealized_pnl_incomplete(make_service):
    class NonFinitePnlBroker(MockBroker):
        def get_positions(self):
            return [
                Position(
                    "AAPL",
                    Decimal("2"),
                    Decimal("90"),
                    Decimal("100"),
                    unrealized_intraday_pnl=Decimal("NaN"),
                )
            ]

    broker = NonFinitePnlBroker()
    broker.set_price("AAPL", Decimal("100"))

    snapshot = make_service(
        broker=broker
    ).snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.daily_pnl_complete is False
    assert snapshot.unrealized_pnl_today == Decimal(0)


def test_snapshot_marks_non_finite_realized_pnl_incomplete(
    make_service, monkeypatch
):
    monkeypatch.setattr(
        snapshot_module,
        "realized_pnl_today",
        lambda *_args, **_kwargs: Decimal("Infinity"),
    )

    snapshot = make_service().snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.daily_pnl_complete is False
    assert snapshot.realized_pnl_today == Decimal(0)


def test_snapshot_assembles_cash_intraday_pnl_spread_and_market_state(make_service):
    broker = MockBroker(
        positions=[
            Position(
                "AAPL",
                Decimal("2"),
                Decimal("90"),
                Decimal("110"),
            )
        ],
        buying_power=Decimal("5000"),
    )
    broker.set_session_open_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    broker.set_price("AAPL", Decimal("110"))

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.cash == Decimal("5000")
    assert snapshot.unrealized_pnl_today == Decimal("20")
    assert snapshot.daily_pnl_complete is True
    assert snapshot.account_equity == Decimal("5220")
    assert snapshot.market_open is True
    assert snapshot.broker_reconciled is True
    assert snapshot.spread_pct_by_ticker["AAPL"] == Decimal("0.2")


def test_account_high_water_mark_survives_service_restart(make_service):
    first = make_service()
    first_snapshot = first.snapshot_service.assemble_for_execution("AAPL")
    first.broker._buying_power = Decimal("90000")

    second = make_service(broker=first.broker)
    second_snapshot = second.snapshot_service.assemble_for_execution("AAPL")

    assert first_snapshot.account_high_water_mark == Decimal("100000")
    assert second_snapshot.account_high_water_mark == Decimal("100000")
    assert second_snapshot.account_equity == Decimal("90000")


def test_invalid_initial_account_snapshot_does_not_initialize_high_water_mark(
    make_service,
):
    service = make_service()
    service.broker.get_account = lambda: Account(
        buying_power=Decimal("100000"),
        equity=Decimal("0"),
        cash=Decimal("100000"),
    )

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.account_complete is False
    assert snapshot.account_high_water_mark == Decimal("0")
    result = RiskEngine(service.config.risk).check(order(), snapshot)
    assert "account snapshot is incomplete" in result.reasons
    with service.session_factory() as session:
        assert session.get(AccountRiskState, AssetClass.EQUITY.value) is None


def test_invalid_account_snapshot_does_not_update_existing_high_water_mark(
    make_service,
):
    service = make_service()
    service.snapshot_service.assemble_for_execution("AAPL")
    with service.session_factory() as session:
        state = session.get(AccountRiskState, AssetClass.EQUITY.value)
        before = (
            state.high_water_mark,
            state.last_equity,
            state.updated_at,
        )
    service.broker.get_account = lambda: Account(
        buying_power=Decimal("100000"),
        equity=Decimal("NaN"),
        cash=Decimal("100000"),
    )

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.account_complete is False
    assert snapshot.account_high_water_mark == before[0]
    with service.session_factory() as session:
        state = session.get(AccountRiskState, AssetClass.EQUITY.value)
        assert (
            state.high_water_mark,
            state.last_equity,
            state.updated_at,
        ) == before


def test_snapshot_contains_only_symbol_relevant_active_breakers(make_service):
    service = make_service()
    service.breakers.trip(
        BreakerScope.data(AssetClass.EQUITY),
        "feed disagreement",
        "daemon",
        request_id="execution-feed-disagreement",
    )
    service.breakers.trip(
        BreakerScope.liquidity("MSFT"),
        "wide spread",
        "daemon",
        request_id="execution-wide-spread",
    )

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.active_breakers == frozenset({"data:equity"})
