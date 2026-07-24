from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event

import trading_assistant.orders.snapshot as snapshot_module
from trading_assistant.assets import AssetClass
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
)
from trading_assistant.db.models import Order, utcnow
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.risk.engine import RiskEngine


NOW = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)


def order(
    ticker: str = "AAPL",
    notional: str | None = "100",
    *,
    side: OrderSide = OrderSide.BUY,
    qty: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        ticker=ticker,
        side=side,
        order_type=OrderType.MARKET,
        idempotency_key=f"risk-{ticker}-{side.value}-{notional}-{qty}",
        notional=Decimal(notional) if notional is not None else None,
        qty=Decimal(qty) if qty is not None else None,
    )


def _pending(
    session_factory,
    *,
    key: str,
    side: OrderSide,
    status: OrderStatus,
    notional: str | None = None,
    qty: str | None = None,
) -> int:
    with session_factory() as session:
        row = Order(
            idempotency_key=key,
            ticker="AAPL",
            side=side.value,
            order_type=OrderType.MARKET.value,
            status=status.value,
            notional=Decimal(notional) if notional is not None else None,
            qty=Decimal(qty) if qty is not None else None,
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
        "AAPL", "buy", "market", notional="60"
    )["order_id"]
    second_id = service.propose_order(
        "AAPL", "buy", "market", notional="60"
    )["order_id"]
    for order_id in (first_id, second_id):
        service.order_application.approve(
            ApprovalCommand(
                order_id,
                "operator:concurrent-test",
                "independently reviewed",
                utcnow(),
            )
        )

    first = service.order_submission.submit(first_id)
    second = service.order_submission.submit(second_id)

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
    )
    service.broker.get_quote = lambda _ticker: stale

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.quote_fresh is False


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


def test_snapshot_contains_only_symbol_relevant_active_breakers(make_service):
    service = make_service()
    service.breakers.trip(
        BreakerScope.data(AssetClass.EQUITY),
        "feed disagreement",
        "daemon",
    )
    service.breakers.trip(
        BreakerScope.liquidity("MSFT"),
        "wide spread",
        "daemon",
    )

    snapshot = service.snapshot_service.assemble_for_execution("AAPL")

    assert snapshot.active_breakers == frozenset({"data:equity"})
