"""A2 + A3: kill switch trips on real FIFO loss and survives a restart."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.risk.killswitch import KillSwitch
from trading_assistant.risk.pnl import FillLike, realized_pnl_today


def test_trip_persists_across_restart(db_url, engine):
    factory = make_session_factory(engine)
    with factory() as s:
        assert KillSwitch.is_tripped(s) is False
        KillSwitch.trip(s, reason="test trip")
        s.commit()

    # Simulate a process restart: brand-new engine + session on the same DB file.
    engine2 = create_db_engine(db_url)
    factory2 = make_session_factory(engine2)
    with factory2() as s:
        assert KillSwitch.is_tripped(s) is True


def test_reset_unblocks(engine):
    factory = make_session_factory(engine)
    with factory() as s:
        KillSwitch.trip(s, reason="test")
        s.commit()
    with factory() as s:
        KillSwitch.reset(s)
        s.commit()
    with factory() as s:
        assert KillSwitch.is_tripped(s) is False


def test_daily_loss_from_fills_trips_switch(engine):
    """Kill switch trips off a REAL FIFO computation, not a stubbed number (A2)."""
    now = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    fills = [
        FillLike("AAPL", "buy", Decimal("10"), Decimal("100"),
                 datetime(2026, 7, 8, 14, tzinfo=timezone.utc)),
        FillLike("AAPL", "sell", Decimal("10"), Decimal("40"),
                 datetime(2026, 7, 8, 15, tzinfo=timezone.utc)),
    ]
    loss = realized_pnl_today(fills, now=now)
    assert loss == Decimal("-600")  # (40-100)*10

    factory = make_session_factory(engine)
    with factory() as s:
        tripped = KillSwitch.evaluate_daily_loss(
            s, realized_pnl_today=loss, loss_limit=Decimal("500")
        )
        s.commit()
        assert tripped is True
        assert KillSwitch.is_tripped(s) is True


def test_small_loss_does_not_trip(engine):
    factory = make_session_factory(engine)
    with factory() as s:
        tripped = KillSwitch.evaluate_daily_loss(
            s, realized_pnl_today=Decimal("-100"), loss_limit=Decimal("500")
        )
        s.commit()
        assert tripped is False
