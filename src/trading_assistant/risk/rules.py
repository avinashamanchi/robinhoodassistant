"""Individual risk rules — one deterministic function per limit.

Each returns ``None`` when the order passes, or a human-readable reason string
when it fails. All inputs come from the immutable ``PortfolioSnapshot`` and
``RiskConfig`` (no I/O), so every rule is trivially unit-testable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..broker.models import OrderRequest, OrderSide, OrderType, PortfolioSnapshot
from ..config import RiskConfig


def _reference_price(order: OrderRequest, snapshot: PortfolioSnapshot) -> Optional[Decimal]:
    quote = snapshot.quotes.get(order.ticker.upper())
    return quote.last if quote else None


def _order_qty_shares(order: OrderRequest, price: Decimal) -> Decimal:
    if order.qty is not None:
        return order.qty
    assert order.notional is not None
    return order.notional / price


def check_allowlist(order: OrderRequest, config: RiskConfig) -> Optional[str]:
    if order.ticker.upper() not in config.ticker_allowlist:
        return f"{order.ticker.upper()} is not on the ticker allowlist"
    return None


def check_max_notional(
    order: OrderRequest, snapshot: PortfolioSnapshot, config: RiskConfig
) -> Optional[str]:
    price = _reference_price(order, snapshot)
    if price is None:
        return f"no quote available for {order.ticker.upper()}; cannot size order"
    notional = order.estimated_notional(price)
    limit = Decimal(str(config.max_notional_per_order))
    if notional > limit:
        return f"order notional ${notional:.2f} exceeds max ${limit:.2f} per order"
    return None


def check_max_position(
    order: OrderRequest, snapshot: PortfolioSnapshot, config: RiskConfig
) -> Optional[str]:
    price = _reference_price(order, snapshot)
    if price is None:
        return f"no quote available for {order.ticker.upper()}; cannot size position"
    qty = _order_qty_shares(order, price)
    signed = qty if order.side is OrderSide.BUY else -qty
    current = snapshot.positions.get(order.ticker.upper())
    current_qty = current.qty if current else Decimal(0)
    projected_value = abs(current_qty + signed) * price
    limit = Decimal(str(config.max_position_per_ticker))
    if projected_value > limit:
        return (
            f"projected {order.ticker.upper()} position ${projected_value:.2f} "
            f"exceeds max ${limit:.2f} per ticker"
        )
    return None


def check_portfolio_exposure(
    order: OrderRequest, snapshot: PortfolioSnapshot, config: RiskConfig
) -> Optional[str]:
    price = _reference_price(order, snapshot)
    if price is None:
        return f"no quote available for {order.ticker.upper()}; cannot size exposure"
    qty = _order_qty_shares(order, price)
    signed = qty if order.side is OrderSide.BUY else -qty
    current = snapshot.positions.get(order.ticker.upper())
    current_qty = current.qty if current else Decimal(0)
    current_ticker_value = abs(current_qty) * price
    projected_ticker_value = abs(current_qty + signed) * price
    projected_gross = (
        snapshot.gross_exposure() - current_ticker_value + projected_ticker_value
    )
    limit = Decimal(str(config.max_portfolio_exposure))
    if projected_gross > limit:
        return (
            f"projected portfolio exposure ${projected_gross:.2f} "
            f"exceeds max ${limit:.2f}"
        )
    return None


def check_price_sanity(
    order: OrderRequest, snapshot: PortfolioSnapshot, config: RiskConfig
) -> Optional[str]:
    if order.order_type is not OrderType.LIMIT or order.limit_price is None:
        return None
    price = _reference_price(order, snapshot)
    if price is None:
        return f"no quote available for {order.ticker.upper()}; cannot sanity-check price"
    if price == 0:
        return f"reference price for {order.ticker.upper()} is zero"
    deviation_pct = abs(order.limit_price - price) / price * Decimal(100)
    limit = Decimal(str(config.price_sanity_pct))
    if deviation_pct > limit:
        return (
            f"limit price ${order.limit_price:.2f} deviates {deviation_pct:.2f}% "
            f"from last ${price:.2f} (max {limit:.2f}%)"
        )
    return None


def check_market_hours(
    order: OrderRequest, config: RiskConfig, market_open: bool
) -> Optional[str]:
    if not market_open and config.reject_when_market_closed:
        return "market is closed; order rejected (queueing not requested)"
    return None
