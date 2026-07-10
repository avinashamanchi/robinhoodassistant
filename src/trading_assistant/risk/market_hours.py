"""Market-hours helper. Consumes the MarketClock protocol only (A7)."""

from __future__ import annotations

from datetime import datetime

from .clock import MarketClock


def is_market_open(clock: MarketClock, at: datetime | None = None) -> bool:
    """Thin wrapper so callers assemble the ``market_open`` bool the pure engine needs."""
    return clock.is_open(at)
