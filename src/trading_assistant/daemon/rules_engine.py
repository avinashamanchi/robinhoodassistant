"""Pure evaluation of conditional-rule triggers.

A rule condition is a small dict, e.g. {"price_below": 175} or {"price_above": 50}.
Evaluation is deterministic and I/O-free (given a quote), so it is trivially
unit-testable and cannot itself place orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from ..broker.models import Quote

SUPPORTED = {"price_below", "price_above", "trailing_stop_pct", "time_stop"}


def evaluate(condition: dict[str, Any], quote: Quote) -> bool:
    """True if the quote satisfies a simple price condition. Unknown keys never fire."""
    last = quote.last
    if "price_below" in condition and last < Decimal(str(condition["price_below"])):
        return True
    if "price_above" in condition and last > Decimal(str(condition["price_above"])):
        return True
    return False


def update_trailing_stop(
    hwm: Optional[Decimal], price: Decimal, pct: float
) -> tuple[bool, Decimal]:
    """Advance the high-water mark and test the trailing stop.

    Returns (fires, new_hwm). The new HWM must be PERSISTED by the caller so a
    daemon restart doesn't reset it and silently widen the stop.
    """
    new_hwm = price if hwm is None else max(hwm, price)
    threshold = new_hwm * (Decimal(1) - Decimal(str(pct)) / Decimal(100))
    return price <= threshold, new_hwm


def time_stop_fires(deadline: Optional[datetime], now: Optional[datetime] = None) -> bool:
    if deadline is None:
        return False
    now = now or datetime.now(timezone.utc)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return now >= deadline


def describe(condition: dict[str, Any]) -> str:
    parts = []
    if "price_below" in condition:
        parts.append(f"price below {condition['price_below']}")
    if "price_above" in condition:
        parts.append(f"price above {condition['price_above']}")
    return " and ".join(parts) or "unknown condition"
