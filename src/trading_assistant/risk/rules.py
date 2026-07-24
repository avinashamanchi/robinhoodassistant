"""Individual risk rules — one deterministic function per limit.

Each returns ``None`` when the order passes, or a human-readable reason string
when it fails. All inputs come from the immutable ``PortfolioSnapshot`` and
``RiskConfig`` (no I/O), so every rule is trivially unit-testable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..broker.models import (
    OrderRequest,
    OrderSide,
    OrderType,
    PortfolioSnapshot,
    Quote,
)
from ..config import RiskConfig


def _reference_quote(
    order: OrderRequest, snapshot: PortfolioSnapshot
) -> Optional[Quote]:
    return snapshot.quotes.get(order.ticker.upper())


def _ticker_values(
    order: OrderRequest,
    snapshot: PortfolioSnapshot,
    quote: Quote,
) -> tuple[Decimal, Decimal]:
    current = snapshot.positions.get(order.ticker.upper())
    current_qty = current.qty if current else Decimal(0)
    pending = snapshot.pending_signed_notional.get(
        order.ticker.upper(), Decimal(0)
    )
    current_value = current_qty * quote.last + pending
    order_value = order.risk_notional(quote)
    signed_order_value = (
        order_value if order.side is OrderSide.BUY else -order_value
    )
    return abs(current_value), abs(current_value + signed_order_value)


def check_allowlist(order: OrderRequest, config: RiskConfig) -> Optional[str]:
    if order.ticker.upper() not in config.ticker_allowlist:
        return f"{order.ticker.upper()} is not on the ticker allowlist"
    return None


def check_pending_exposure_known(snapshot: PortfolioSnapshot) -> Optional[str]:
    if not snapshot.pending_exposure_complete:
        return "outstanding order exposure is unknown; new orders are blocked"
    return None


def check_max_notional(
    order: OrderRequest, snapshot: PortfolioSnapshot, config: RiskConfig
) -> Optional[str]:
    quote = _reference_quote(order, snapshot)
    if quote is None:
        return f"no quote available for {order.ticker.upper()}; cannot size order"
    notional = order.risk_notional(quote)
    limit = Decimal(str(config.max_notional_per_order))
    if notional > limit:
        return f"order notional ${notional:.2f} exceeds max ${limit:.2f} per order"
    return None


def check_max_position(
    order: OrderRequest, snapshot: PortfolioSnapshot, config: RiskConfig
) -> Optional[str]:
    quote = _reference_quote(order, snapshot)
    if quote is None:
        return f"no quote available for {order.ticker.upper()}; cannot size position"
    _current_value, projected_value = _ticker_values(
        order, snapshot, quote
    )
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
    quote = _reference_quote(order, snapshot)
    if quote is None:
        return f"no quote available for {order.ticker.upper()}; cannot size exposure"
    current_ticker_value, projected_ticker_value = _ticker_values(
        order, snapshot, quote
    )
    projected_gross = (
        snapshot.gross_exposure_with_pending()
        - current_ticker_value
        + projected_ticker_value
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
    quote = _reference_quote(order, snapshot)
    if quote is None:
        return f"no quote available for {order.ticker.upper()}; cannot sanity-check price"
    price = quote.last
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


def check_cross_broker_concentration(
    order: OrderRequest, snapshot: PortfolioSnapshot, config: RiskConfig
) -> Optional[str]:
    """WARNING (never a rejection): combined Alpaca + external exposure in this
    ticker would exceed max_position_per_ticker. External holdings aren't ours to
    manage, so this only informs — the human may intend the concentration."""
    quote = _reference_quote(order, snapshot)
    if quote is None:
        return None
    _current_value, projected_alpaca = _ticker_values(
        order, snapshot, quote
    )
    external = snapshot.external_position_value(order.ticker.upper())
    combined = projected_alpaca + external
    limit = Decimal(str(config.max_position_per_ticker))
    if external > 0 and combined > limit:
        return (
            f"cross-broker concentration: combined {order.ticker.upper()} exposure "
            f"${combined:.2f} (Alpaca ${projected_alpaca:.2f} + external ${external:.2f}) "
            f"exceeds ${limit:.2f} — not blocked, external holdings are read-only"
        )
    return None
