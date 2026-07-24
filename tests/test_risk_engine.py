"""The risk engine: every limit, pass and fail. Deterministic, pure (A1)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import inspect

import pytest

from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderType,
    Position,
    Quote,
)
from trading_assistant.risk.engine import RiskEngine


def _order(
    ticker="AAPL",
    side=OrderSide.BUY,
    order_type=OrderType.MARKET,
    notional=None,
    qty=None,
    limit_price=None,
) -> OrderRequest:
    return OrderRequest(
        ticker=ticker,
        side=side,
        order_type=order_type,
        idempotency_key=f"k-{ticker}-{notional}-{qty}-{limit_price}",
        notional=Decimal(notional) if notional is not None else None,
        qty=Decimal(qty) if qty is not None else None,
        limit_price=Decimal(limit_price) if limit_price is not None else None,
    )


def _check(engine, order, snapshot):
    return engine.check(order, snapshot)


def test_check_has_exact_pure_two_argument_interface():
    assert list(inspect.signature(RiskEngine.check).parameters) == [
        "self",
        "order",
        "snapshot",
    ]


# ── happy path ──────────────────────────────────────────────────
def test_valid_order_approved(risk_config, make_snapshot):
    engine = RiskEngine(risk_config)
    snap = make_snapshot(prices={"AAPL": Decimal("100")})
    result = _check(engine, _order(notional="400"), snap)
    assert result.approved is True
    assert result.reasons == []


# ── each limit in isolation ─────────────────────────────────────
def test_allowlist_rejects_unknown_ticker(risk_config, make_snapshot):
    engine = RiskEngine(risk_config)
    snap = make_snapshot(prices={"TSLA": Decimal("100")})  # quote present, so only allowlist fails
    result = _check(engine, _order(ticker="TSLA", notional="100"), snap)
    assert result.rejected
    assert any("allowlist" in r for r in result.reasons)


def test_max_notional_per_order(risk_config, make_snapshot):
    engine = RiskEngine(risk_config)
    snap = make_snapshot(prices={"AAPL": Decimal("100")})
    result = _check(engine, _order(notional="600"), snap)  # > 500
    assert result.rejected
    assert any("per order" in r for r in result.reasons)


def test_max_position_per_ticker(risk_config, make_snapshot):
    engine = RiskEngine(risk_config)
    snap = make_snapshot(
        prices={"AAPL": Decimal("100")},
        positions=[Position("AAPL", Decimal("16"), Decimal("100"), Decimal("100"))],
    )
    # $500 order (passes notional) but pushes position to $2100 > $2000.
    result = _check(engine, _order(notional="500"), snap)
    assert result.rejected
    assert any("per ticker" in r for r in result.reasons)


def test_max_position_includes_outstanding_order_exposure(
    risk_config, make_snapshot
):
    engine = RiskEngine(risk_config)
    snap = make_snapshot(
        prices={"AAPL": Decimal("100")},
        pending_signed_notional={"AAPL": Decimal("1800")},
    )
    result = _check(engine, _order(notional="400"), snap)
    assert result.rejected
    assert any("per ticker" in reason for reason in result.reasons)


def test_unknown_outstanding_order_exposure_fails_closed(
    risk_config, make_snapshot
):
    engine = RiskEngine(risk_config)
    snapshot = make_snapshot(prices={"AAPL": Decimal("100")})
    object.__setattr__(snapshot, "pending_exposure_complete", False)

    result = _check(engine, _order(notional="100"), snapshot)

    assert result.rejected
    assert any("outstanding order exposure" in reason for reason in result.reasons)


def test_max_portfolio_exposure(risk_config, make_snapshot):
    cfg = risk_config.model_copy(
        update={"max_notional_per_order": 100000, "max_position_per_ticker": 100000,
                "max_portfolio_exposure": 5000}
    )
    engine = RiskEngine(cfg)
    snap = make_snapshot(
        prices={"AAPL": Decimal("100")},
        positions=[
            Position("AAPL", Decimal("30"), Decimal("100"), Decimal("100")),  # 3000
            Position("MSFT", Decimal("30"), Decimal("100"), Decimal("100")),  # 3000
        ],
    )
    result = _check(engine, _order(notional="1000"), snap)  # gross -> 7000 > 5000
    assert result.rejected
    assert any("exposure" in r for r in result.reasons)


def test_price_sanity_on_limit_orders(risk_config, make_snapshot):
    engine = RiskEngine(risk_config)
    snap = make_snapshot(prices={"AAPL": Decimal("100")})
    order = _order(order_type=OrderType.LIMIT, qty="1", limit_price="120")  # 20% off
    result = _check(engine, order, snap)
    assert result.rejected
    assert any("deviates" in r for r in result.reasons)


def test_market_closed_rejected(risk_config, make_snapshot):
    engine = RiskEngine(risk_config)
    snap = make_snapshot(prices={"AAPL": Decimal("100")})
    snap = snap.__class__(**{**snap.__dict__, "market_open": False})
    result = _check(engine, _order(notional="100"), snap)
    assert result.rejected
    assert any("market is closed" in r for r in result.reasons)


def test_killswitch_blocks_everything(risk_config, make_snapshot):
    engine = RiskEngine(risk_config)
    snap = make_snapshot(prices={"AAPL": Decimal("100")})
    snap = snap.__class__(
        **{**snap.__dict__, "active_breakers": frozenset({"loss:equity"})}
    )
    result = _check(engine, _order(notional="100"), snap)
    assert result.rejected
    assert any("circuit breaker" in r for r in result.reasons)


def test_missing_quote_fails_closed(risk_config, make_snapshot):
    """No market data for the ticker -> reject (fail closed), never size blindly."""
    engine = RiskEngine(risk_config)
    snap = make_snapshot(prices={})  # no quote for AAPL at all
    result = _check(engine, _order(notional="100"), snap)
    assert result.rejected
    assert any("no quote available" in r for r in result.reasons)


@pytest.mark.parametrize(
    "last",
    [
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_quantity_buy_rejects_non_positive_or_non_finite_last_before_arithmetic(
    risk_config, make_snapshot, last
):
    snapshot = make_snapshot(prices={"AAPL": Decimal("100")})
    snapshot = replace(
        snapshot,
        quotes={
            "AAPL": Quote(
                ticker="AAPL",
                bid=Decimal("99"),
                ask=Decimal("101"),
                last=last,
            )
        },
    )

    result = RiskEngine(risk_config).check(_order(qty="10"), snapshot)

    assert result.rejected
    assert "quote for AAPL is invalid" in result.reasons


def test_notional_sell_rejects_zero_last_instead_of_dividing_by_zero(
    risk_config, make_snapshot
):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")},
        positions=[
            Position("AAPL", Decimal("10"), Decimal("100"), Decimal("100"))
        ],
    )
    snapshot = replace(
        snapshot,
        quotes={
            "AAPL": Quote(
                ticker="AAPL",
                bid=Decimal("0"),
                ask=Decimal("0"),
                last=Decimal("0"),
            )
        },
    )

    result = RiskEngine(risk_config).check(
        _order(side=OrderSide.SELL, notional="100"),
        snapshot,
    )

    assert result.rejected
    assert "quote for AAPL is invalid" in result.reasons


@pytest.mark.parametrize(
    ("bid", "ask"),
    [
        (Decimal("-1"), Decimal("101")),
        (Decimal("99"), Decimal("-1")),
        (Decimal("NaN"), Decimal("101")),
        (Decimal("99"), Decimal("NaN")),
        (Decimal("Infinity"), Decimal("101")),
        (Decimal("99"), Decimal("Infinity")),
        (Decimal("101"), Decimal("99")),
        (Decimal("0"), Decimal("0")),
    ],
)
def test_rejects_invalid_bid_ask_or_impossible_quote_shape_before_arithmetic(
    risk_config, make_snapshot, bid, ask
):
    snapshot = make_snapshot(prices={"AAPL": Decimal("100")})
    snapshot = replace(
        snapshot,
        quotes={
            "AAPL": Quote(
                ticker="AAPL",
                bid=bid,
                ask=ask,
                last=Decimal("100"),
            )
        },
    )

    result = RiskEngine(risk_config).check(_order(notional="100"), snapshot)

    assert result.rejected
    assert "quote for AAPL is invalid" in result.reasons


def test_reasons_accumulate(risk_config, make_snapshot):
    engine = RiskEngine(risk_config)
    snap = make_snapshot(prices={"TSLA": Decimal("100")})
    # Not on allowlist AND market closed -> at least two independent reasons.
    snap = snap.__class__(**{**snap.__dict__, "market_open": False})
    result = _check(engine, _order(ticker="TSLA", notional="100"), snap)
    assert result.rejected
    assert len(result.reasons) >= 2
