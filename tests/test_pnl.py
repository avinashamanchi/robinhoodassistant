"""A2: FIFO realized P&L + America/New_York daily boundary (UTC storage)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_assistant.risk.pnl import (
    NY,
    FillLike,
    most_recent_regular_open,
    realized_pnl,
    realized_pnl_today,
)


def _f(ticker, side, qty, price, dt) -> FillLike:
    return FillLike(ticker, side, Decimal(qty), Decimal(price), dt)


def test_fifo_long_partial_close():
    fills = [
        _f("AAPL", "buy", "10", "100", datetime(2026, 1, 2, 15, tzinfo=timezone.utc)),
        _f("AAPL", "buy", "10", "110", datetime(2026, 1, 2, 16, tzinfo=timezone.utc)),
        _f("AAPL", "sell", "15", "120", datetime(2026, 1, 2, 17, tzinfo=timezone.utc)),
    ]
    # 10 @ (120-100) + 5 @ (120-110) = 200 + 50
    assert realized_pnl(fills) == Decimal("250")


def test_fifo_short_then_cover():
    fills = [
        _f("AAPL", "sell", "10", "100", datetime(2026, 1, 2, 15, tzinfo=timezone.utc)),
        _f("AAPL", "buy", "10", "90", datetime(2026, 1, 2, 16, tzinfo=timezone.utc)),
    ]
    # short at 100, cover at 90 -> +100
    assert realized_pnl(fills) == Decimal("100")


def test_open_position_has_no_realized_pnl():
    fills = [_f("AAPL", "buy", "10", "100", datetime(2026, 1, 2, 15, tzinfo=timezone.utc))]
    assert realized_pnl(fills) == Decimal("0")


def test_daily_boundary_excludes_prior_day():
    now = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)  # Wed ~14:00 ET
    fills = [
        # Closed YESTERDAY (loss) -> excluded from "today".
        _f("MSFT", "buy", "5", "100", datetime(2026, 7, 6, 15, tzinfo=timezone.utc)),
        _f("MSFT", "sell", "5", "90", datetime(2026, 7, 7, 16, tzinfo=timezone.utc)),
        # Opened yesterday, CLOSED TODAY (gain) -> counted.
        _f("AAPL", "buy", "10", "100", datetime(2026, 7, 7, 14, tzinfo=timezone.utc)),
        _f("AAPL", "sell", "10", "110", datetime(2026, 7, 8, 15, tzinfo=timezone.utc)),
    ]
    assert realized_pnl(fills) == Decimal("50")            # 100 - 50 overall
    assert realized_pnl_today(fills, now=now) == Decimal("100")  # only today's close


def test_most_recent_open_is_weekday_0930_et_before_now():
    for now in [
        datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc),   # midday
        datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc),   # before open ET
        datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc),  # weekend
    ]:
        open_utc = most_recent_regular_open(now)
        local = open_utc.astimezone(NY)
        assert open_utc <= now
        assert (local.hour, local.minute) == (9, 30)
        assert local.weekday() < 5  # Mon-Fri only


def test_before_open_rolls_back_a_day():
    before_open = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)  # 08:00 ET Wed
    open_utc = most_recent_regular_open(before_open)
    assert open_utc.astimezone(NY).date() < before_open.astimezone(NY).date()
