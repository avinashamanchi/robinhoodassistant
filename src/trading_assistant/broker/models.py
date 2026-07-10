"""Broker-layer value types shared across the whole system.

These are plain, serializable dataclasses with no I/O. The risk engine (A1)
operates purely on an ``OrderRequest`` + a ``PortfolioSnapshot`` assembled by
the caller, so every type here must be constructible in a test without a broker.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, enum.Enum):
    """Lifecycle statuses. Transition rules live in db.models.OrderStateMachine (A4)."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Money is handled as Decimal end-to-end to avoid float drift on notionals.
Money = Decimal


@dataclass(frozen=True)
class OrderRequest:
    """An intent to trade. Carries EITHER qty OR notional, never both.

    ``idempotency_key`` is client-generated and unique per logical order; the
    broker layer must never resubmit the same key without first checking status.
    """

    ticker: str
    side: OrderSide
    order_type: OrderType
    idempotency_key: str
    qty: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if (self.qty is None) == (self.notional is None):
            raise ValueError("OrderRequest requires exactly one of qty or notional")
        if self.qty is not None and self.qty <= 0:
            raise ValueError("qty must be positive")
        if self.notional is not None and self.notional <= 0:
            raise ValueError("notional must be positive")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit order requires limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market order must not carry a limit_price")

    def estimated_notional(self, reference_price: Decimal) -> Decimal:
        """USD notional this order represents, using a reference price for qty orders.

        For notional orders the amount is exact. For qty orders it is qty *
        reference_price (the caller supplies the current/last price).
        """
        if self.notional is not None:
            return self.notional
        assert self.qty is not None  # guaranteed by __post_init__
        return self.qty * reference_price


@dataclass(frozen=True)
class Quote:
    ticker: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    prev_close: Optional[Decimal] = None
    as_of: datetime = field(default_factory=_utcnow)

    @property
    def day_change_pct(self) -> Optional[Decimal]:
        if self.prev_close is None or self.prev_close == 0:
            return None
        return (self.last - self.prev_close) / self.prev_close * Decimal(100)


@dataclass(frozen=True)
class Position:
    ticker: str
    qty: Decimal          # signed; negative = short
    avg_entry_price: Decimal
    current_price: Decimal

    @property
    def market_value(self) -> Decimal:
        return self.qty * self.current_price


@dataclass(frozen=True)
class Account:
    buying_power: Decimal
    equity: Decimal
    cash: Decimal


@dataclass(frozen=True)
class OrderResult:
    """What the broker returns after a submit / status query."""

    idempotency_key: str
    broker_order_id: Optional[str]
    status: OrderStatus
    filled_qty: Decimal = Decimal(0)
    avg_fill_price: Optional[Decimal] = None
    submitted_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Everything the risk engine needs, assembled by the caller (A1).

    The risk engine performs NO I/O; it reads this immutable snapshot. Quotes
    map ticker -> latest Quote; positions map ticker -> Position.
    ``realized_pnl_today`` is computed by risk.pnl from the fills table (A2).

    ``external_positions`` holds READ-ONLY holdings at other brokers (e.g.
    Robinhood), keyed by ticker. Hard risk limits apply only to our Alpaca
    positions; external holdings only inform a non-blocking cross-broker warning.
    Typed loosely to avoid coupling broker/ to external_accounts/.
    """

    positions: dict[str, Position]
    quotes: dict[str, Quote]
    buying_power: Decimal
    realized_pnl_today: Decimal
    as_of: datetime = field(default_factory=_utcnow)
    external_positions: dict[str, "object"] = field(default_factory=dict)

    def position_value(self, ticker: str) -> Decimal:
        pos = self.positions.get(ticker)
        return abs(pos.market_value) if pos else Decimal(0)

    def external_position_value(self, ticker: str) -> Decimal:
        ext = self.external_positions.get(ticker.upper())
        return abs(ext.current_value) if ext is not None else Decimal(0)

    def gross_exposure(self) -> Decimal:
        """Total absolute market value across all positions (USD)."""
        return sum((abs(p.market_value) for p in self.positions.values()), Decimal(0))
