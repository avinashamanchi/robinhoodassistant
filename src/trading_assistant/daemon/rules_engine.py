"""Pure evaluation of conditional-rule triggers.

A rule condition is a small dict, e.g. {"price_below": 175} or {"price_above": 50}.
Evaluation is deterministic and I/O-free (given a quote), so it is trivially
unit-testable and cannot itself place orders.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..broker.models import Quote

SUPPORTED = {"price_below", "price_above"}


def evaluate(condition: dict[str, Any], quote: Quote) -> bool:
    """True if the quote satisfies the condition. Unknown keys never fire."""
    last = quote.last
    if "price_below" in condition and last < Decimal(str(condition["price_below"])):
        return True
    if "price_above" in condition and last > Decimal(str(condition["price_above"])):
        return True
    return False


def describe(condition: dict[str, Any]) -> str:
    parts = []
    if "price_below" in condition:
        parts.append(f"price below {condition['price_below']}")
    if "price_above" in condition:
        parts.append(f"price above {condition['price_above']}")
    return " and ".join(parts) or "unknown condition"
