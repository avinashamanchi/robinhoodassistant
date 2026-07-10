"""ExternalAccountSource protocol + read-only value types.

READ-ONLY by construction: the protocol declares only getters. See the package
docstring for the hard non-goals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, runtime_checkable


class ExternalAuthError(Exception):
    """Raised on external-broker auth failure with a human-readable hint."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ExternalPosition:
    ticker: str
    quantity: Decimal
    avg_cost: Decimal
    current_price: Decimal
    source: str

    @property
    def current_value(self) -> Decimal:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.current_price - self.avg_cost) * self.quantity


@dataclass(frozen=True)
class ExternalAccountSummary:
    total_equity: Decimal
    cash: Decimal
    buying_power: Decimal
    source: str
    as_of: datetime = field(default_factory=_utcnow)
    stale: bool = False  # True when served from cache after a fetch failure


@runtime_checkable
class ExternalAccountSource(Protocol):
    """Read-only data source. Intentionally has NO write/order/transfer methods."""

    source_name: str

    def get_positions(self) -> list[ExternalPosition]: ...
    def get_account_summary(self) -> ExternalAccountSummary: ...
    def get_order_history(self, days: int = 30) -> list[dict]: ...
    def get_dividends(self, days: int = 90) -> list[dict]: ...
