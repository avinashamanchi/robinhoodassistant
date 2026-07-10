"""Realized P&L via FIFO lot tracking (A2).

Realized P&L is recognized at the *closing* trade. We replay all fills in
chronological order, matching each closing fill against the oldest open lots
(FIFO), and attribute each realized chunk to the timestamp of the closing fill.
"Daily" realized P&L = the sum of chunks whose closing fill happened since the
most recent regular-session open, evaluated in America/New_York.

All fills are stored in UTC; we convert only for the market-day boundary.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Iterable
from zoneinfo import ZoneInfo

from ..assets import AssetClass

NY = ZoneInfo("America/New_York")
_REGULAR_OPEN = time(9, 30)


@dataclass(frozen=True)
class FillLike:
    """Minimal shape the P&L engine needs (ORM Fill rows satisfy this)."""

    ticker: str
    side: str          # "buy" | "sell"
    qty: Decimal
    price: Decimal
    filled_at: datetime  # UTC-aware


@dataclass
class _Lot:
    qty: Decimal   # signed: >0 long, <0 short
    price: Decimal


def most_recent_regular_open(now: datetime) -> datetime:
    """Most recent weekday 09:30 America/New_York at or before ``now``, as UTC.

    Weekends roll back to Friday. This intentionally ignores exchange holidays —
    the daily-loss reset boundary does not need holiday precision, and keeping it
    calendar-free keeps it deterministic and dependency-light. Precise
    open/close checks go through the MarketClock (A7), not this helper.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(NY)
    candidate = local.replace(
        hour=_REGULAR_OPEN.hour, minute=_REGULAR_OPEN.minute, second=0, microsecond=0
    )
    if candidate > local:
        candidate -= timedelta(days=1)
    # Roll back over weekends (Sat=5, Sun=6).
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _realized_events(fills: Iterable[FillLike]) -> list[tuple[datetime, Decimal]]:
    """Replay fills FIFO, returning (closing_time, realized_pnl) chunks."""
    ordered = sorted(fills, key=lambda f: f.filled_at)
    books: dict[str, deque[_Lot]] = defaultdict(deque)
    events: list[tuple[datetime, Decimal]] = []

    for fill in ordered:
        book = books[fill.ticker.upper()]
        delta = fill.qty if fill.side == "buy" else -fill.qty  # signed inventory change

        # Close against opposite-signed open lots first (FIFO).
        while delta != 0 and book and (book[0].qty > 0) != (delta > 0):
            lot = book[0]
            close_qty = min(abs(lot.qty), abs(delta))
            if lot.qty > 0:  # closing a long: profit if sold above cost
                pnl = (fill.price - lot.price) * close_qty
            else:            # closing a short: profit if bought below entry
                pnl = (lot.price - fill.price) * close_qty
            events.append((fill.filled_at, pnl))

            lot.qty += close_qty if lot.qty < 0 else -close_qty
            delta += close_qty if delta < 0 else -close_qty
            if lot.qty == 0:
                book.popleft()

        # Whatever remains opens a new lot in the fill's direction.
        if delta != 0:
            book.append(_Lot(qty=delta, price=fill.price))

    return events


def realized_events(fills: Iterable[FillLike]) -> list[tuple[datetime, Decimal]]:
    """Public view of the FIFO realized-P&L chunks (closing time, pnl) — used by
    backtest metrics for per-trade win/loss statistics."""
    return _realized_events(fills)


def realized_pnl(fills: Iterable[FillLike], since: datetime | None = None) -> Decimal:
    """Total realized P&L. If ``since`` is given, only chunks closed at/after it."""
    events = _realized_events(fills)
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    total = Decimal(0)
    for closed_at, pnl in events:
        if since is None or closed_at >= since:
            total += pnl
    return total


def most_recent_utc_midnight(now: datetime) -> datetime:
    """Most recent 00:00 UTC at or before ``now`` — the crypto daily boundary (24/7)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def most_recent_daily_boundary(
    now: datetime, asset_class: AssetClass = AssetClass.EQUITY
) -> datetime:
    """Daily P&L reset boundary. Equity = NY regular open; crypto = UTC midnight."""
    if asset_class is AssetClass.CRYPTO:
        return most_recent_utc_midnight(now)
    return most_recent_regular_open(now)


def realized_pnl_today(
    fills: Iterable[FillLike],
    now: datetime | None = None,
    asset_class: AssetClass = AssetClass.EQUITY,
) -> Decimal:
    """Realized P&L since the most recent daily boundary for the asset class."""
    now = now or datetime.now(timezone.utc)
    return realized_pnl(fills, since=most_recent_daily_boundary(now, asset_class))
