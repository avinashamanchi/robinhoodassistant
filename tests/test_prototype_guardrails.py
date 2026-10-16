"""Prototype guardrails: a cap on open positions and on daily order submissions.

Both apply to every order through the risk engine (human, rule and autopilot
orders alike), are off unless configured, fail closed when today's order count
is unknown, and never block a strictly reduce-only exit.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading_assistant.assets import AssetClass
from trading_assistant.broker.models import OrderRequest, OrderSide, OrderType, Position
from trading_assistant.db.models import Order
from trading_assistant.risk.clock import MarketClockObservation
from trading_assistant.risk.engine import RiskEngine
from trading_assistant.security.sensitive_fields import persist_sensitive

NOW = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)


def _order(ticker="NVDA", side=OrderSide.BUY, qty="1") -> OrderRequest:
    return OrderRequest(
        ticker=ticker,
        side=side,
        order_type=OrderType.MARKET,
        idempotency_key=f"guardrail-{ticker}-{side.value}-{qty}",
        qty=Decimal(qty),
    )


def _position(ticker: str, qty: str = "2") -> Position:
    return Position(
        ticker=ticker,
        qty=Decimal(qty),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("100"),
        unrealized_intraday_pnl=Decimal("0"),
    )


def _guarded(risk_config, **limits):
    return RiskEngine(risk_config.model_copy(update=limits))


_PRICES = {"AAPL": Decimal("100"), "MSFT": Decimal("100"), "NVDA": Decimal("100")}


def test_opening_a_new_name_beyond_the_open_position_cap_is_rejected(risk_config, make_snapshot):
    engine = _guarded(risk_config, max_open_positions=2)
    snapshot = replace(
        make_snapshot(prices=_PRICES, positions=[_position("AAPL"), _position("MSFT")]),
        orders_submitted_today=0,
    )

    rejected = engine.check(_order("NVDA"), snapshot)
    adding = engine.check(_order("AAPL"), snapshot)

    assert rejected.approved is False
    assert any("max 2 open positions" in reason for reason in rejected.reasons)
    assert adding.approved is True, adding.reasons


def test_a_pending_buy_counts_as_an_open_position(risk_config, make_snapshot):
    engine = _guarded(risk_config, max_open_positions=2)
    snapshot = replace(
        make_snapshot(
            prices=_PRICES,
            positions=[_position("AAPL")],
            pending_buy_notional_by_ticker={"MSFT": Decimal("100")},
        ),
        orders_submitted_today=0,
    )

    result = engine.check(_order("NVDA"), snapshot)

    assert result.approved is False
    assert any("2 open or pending" in reason for reason in result.reasons)


def test_the_daily_order_count_blocks_at_the_limit_and_when_unknown(risk_config, make_snapshot):
    engine = _guarded(risk_config, max_orders_per_day=2)
    base = make_snapshot(prices=_PRICES)

    under = engine.check(_order(), replace(base, orders_submitted_today=1))
    at_limit = engine.check(_order(), replace(base, orders_submitted_today=2))
    unknown = engine.check(_order(), replace(base, orders_submitted_today=None))

    assert under.approved is True, under.reasons
    assert at_limit.approved is False
    assert any("max 2 per day" in reason for reason in at_limit.reasons)
    assert unknown.approved is False
    assert any("order count is unknown" in reason for reason in unknown.reasons)


def test_guardrails_are_off_unless_configured(risk_config, make_snapshot):
    engine = RiskEngine(risk_config)
    snapshot = replace(
        make_snapshot(prices=_PRICES, positions=[_position("AAPL"), _position("MSFT")]),
        orders_submitted_today=None,
    )

    assert engine.check(_order("NVDA"), snapshot).approved is True


def test_a_strictly_reduce_only_exit_is_never_blocked_by_the_guardrails(risk_config, make_snapshot):
    engine = _guarded(risk_config, max_open_positions=1, max_orders_per_day=1)
    snapshot = replace(
        make_snapshot(prices=_PRICES, positions=[_position("AAPL", "5"), _position("MSFT")]),
        orders_submitted_today=99,
    )

    result = engine.check(_order("AAPL", side=OrderSide.SELL, qty="2"), snapshot)

    assert result.approved is True, result.reasons


class _SessionClock:
    def __init__(self, boundary: datetime) -> None:
        self.boundary = boundary

    def is_open(self, at=None):
        return True

    def most_recent_open(self, at=None):
        return self.boundary

    def observe(self, at):
        return MarketClockObservation(is_open=True, most_recent_open=self.boundary)


def _seed(service, rows) -> dict[str, int]:
    ids = {}
    with service.session_factory() as session:
        for key, ticker, started in rows:
            order = Order(
                idempotency_key=key,
                ticker=ticker,
                side="buy",
                order_type="market",
                status="filled",
                qty=Decimal("1"),
                submission_started_at=started,
                created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
            )
            persist_sensitive(session, order, {"approval_reason": "guardrail fixture"})
            ids[key] = order.id
        session.commit()
    return ids


def test_snapshot_counts_this_sessions_submissions_for_the_asset_class(make_service):
    service = make_service(quote_now=lambda: NOW)
    service.snapshot_service.now = lambda: NOW
    boundary = NOW - timedelta(hours=3)
    service._clocks[AssetClass.EQUITY] = _SessionClock(boundary)
    ids = _seed(
        service,
        [
            ("before-session", "AAPL", boundary - timedelta(minutes=1)),
            ("this-session-1", "AAPL", boundary + timedelta(minutes=5)),
            ("this-session-2", "MSFT", boundary + timedelta(hours=1)),
            ("never-submitted", "AAPL", None),
            ("crypto", "BTC/USD", boundary + timedelta(minutes=10)),
        ],
    )

    with service.session_factory() as session:
        snapshot = service.assemble_snapshot(session, ["AAPL"])
        excluding = service.assemble_snapshot(
            session, ["AAPL"], exclude_order_id=ids["this-session-2"]
        )

    assert snapshot.orders_submitted_today == 2
    assert excluding.orders_submitted_today == 1
