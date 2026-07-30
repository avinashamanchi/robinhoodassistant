"""Phase 7 §1: equity/crypto are independent in the paper runtime.

Existing equity behavior is covered unchanged by the Phase 1 suite; these tests
prove the two asset classes' kill switches, clocks, and P&L boundaries do not
bleed into each other.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_assistant.assets import AssetClass, broker_symbol_matches_local
from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.risk.clock import CryptoClock, FakeClock
from trading_assistant.risk.killswitch import KillSwitch
from trading_assistant.risk.pnl import (
    FillLike,
    most_recent_daily_boundary,
    realized_pnl_today,
)
from tests.conftest import bind_test_sensitive_factory

EQ = AssetClass.EQUITY
CR = AssetClass.CRYPTO


def test_for_symbol_classifies():
    assert AssetClass.for_symbol("BTC/USD") is CR
    assert AssetClass.for_symbol("ETH/USD") is CR
    assert AssetClass.for_symbol("AAPL") is EQ


def test_broker_symbol_equivalence_is_directional_from_local_canonical_form():
    assert broker_symbol_matches_local("BTCUSD", "BTC/USD") is True
    assert broker_symbol_matches_local("BTC/USD", "BTC/USD") is True
    assert broker_symbol_matches_local("BTC/USD", "BTCUSD") is False
    assert broker_symbol_matches_local("ACME/USD", "ACMEUSD") is False


# ── kill switch independence ────────────────────────────────────
def test_kill_switches_trip_independently(engine):
    f = bind_test_sensitive_factory(make_session_factory(engine))
    with f() as s:
        KillSwitch.trip(
            s,
            reason="equity loss",
            asset_class=EQ,
            actor="test:asset-class",
            request_id="asset-equity-loss",
        )
        s.commit()
    with f() as s:
        assert KillSwitch.is_tripped(s, EQ) is True
        assert KillSwitch.is_tripped(s, CR) is False  # crypto untouched


def test_crypto_trip_does_not_touch_equity(engine):
    f = bind_test_sensitive_factory(make_session_factory(engine))
    with f() as s:
        KillSwitch.trip(
            s,
            reason="crypto dump",
            asset_class=CR,
            actor="test:asset-class",
            request_id="asset-crypto-loss",
        )
        s.commit()
    with f() as s:
        assert KillSwitch.is_tripped(s, CR) is True
        assert KillSwitch.is_tripped(s, EQ) is False


def test_compatibility_reset_cannot_clear_either_asset_class(engine):
    f = bind_test_sensitive_factory(make_session_factory(engine))
    with f() as s:
        KillSwitch.trip(
            s,
            reason="e",
            asset_class=EQ,
            actor="test:asset-class",
            request_id="asset-equity-independent",
        )
        KillSwitch.trip(
            s,
            reason="c",
            asset_class=CR,
            actor="test:asset-class",
            request_id="asset-crypto-independent",
        )
        s.commit()
    with f() as s:
        with pytest.raises(
            RuntimeError,
            match="compatibility reset is unavailable",
        ):
            KillSwitch.reset(
                s,
                asset_class=EQ,
                actor="test:asset-class",
                request_id="asset-equity-reset",
            )
    with f() as s:
        assert KillSwitch.is_tripped(s, EQ) is True
        assert KillSwitch.is_tripped(s, CR) is True


def test_default_asset_class_is_equity(engine):
    """Pre-Phase-7 call style (no asset_class) still targets equity."""
    f = bind_test_sensitive_factory(make_session_factory(engine))
    with f() as s:
        KillSwitch.trip(
            s,
            reason="legacy call",
            actor="test:asset-class",
            request_id="asset-legacy-equity",
        )  # defaults to equity
        s.commit()
    with f() as s:
        assert KillSwitch.is_tripped(s) is True         # equity
        assert KillSwitch.is_tripped(s, CR) is False


def test_crypto_trip_persists_across_restart(db_url, engine):
    f = bind_test_sensitive_factory(make_session_factory(engine))
    with f() as s:
        KillSwitch.trip(
            s,
            reason="dump",
            asset_class=CR,
            actor="test:asset-class",
            request_id="asset-persistent-crypto",
        )
        s.commit()
    engine2 = create_db_engine(db_url)
    restarted = bind_test_sensitive_factory(
        make_session_factory(engine2)
    )
    with restarted() as s:
        assert KillSwitch.is_tripped(s, CR) is True
        assert KillSwitch.is_tripped(s, EQ) is False


# ── clock ───────────────────────────────────────────────────────
def test_crypto_clock_always_open():
    assert CryptoClock().is_open() is True
    # Equity clock is independent and controllable.
    assert FakeClock(is_open=False).is_open() is False


# ── P&L boundary ────────────────────────────────────────────────
def test_daily_boundary_differs_by_class():
    now = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    eq_boundary = most_recent_daily_boundary(now, EQ)        # ~13:30 UTC (NY open)
    cr_boundary = most_recent_daily_boundary(now, CR)        # 00:00 UTC (midnight)
    assert cr_boundary < eq_boundary
    assert cr_boundary.hour == 0 and cr_boundary.minute == 0


def test_pnl_today_uses_class_boundary():
    now = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)
    # Trade closes at 05:00 UTC: after UTC midnight, before NY open (13:30 UTC).
    fills = [
        FillLike("BTC/USD", "buy", Decimal("1"), Decimal("100"),
                 datetime(2026, 7, 7, 20, tzinfo=timezone.utc)),
        FillLike("BTC/USD", "sell", Decimal("1"), Decimal("110"),
                 datetime(2026, 7, 8, 5, tzinfo=timezone.utc)),
    ]
    # Crypto counts it (>= midnight); equity boundary excludes it (< NY open).
    assert realized_pnl_today(fills, now=now, asset_class=CR) == Decimal("10")
    assert realized_pnl_today(fills, now=now, asset_class=EQ) == Decimal("0")


# ── service routing ─────────────────────────────────────────────
def test_service_routes_crypto_around_equity_killswitch(make_service):
    svc = make_service(market_open=False)  # equity market CLOSED
    svc.broker.set_price("BTC/USD", Decimal("100"))
    with svc.session_factory() as s:
        KillSwitch.trip(
            s,
            reason="equity drill",
            asset_class=EQ,
            actor="test:asset-class",
            request_id="asset-equity-drill",
        )
        s.commit()

    # Equity order: blocked by both the equity kill switch and closed market.
    eq = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        actor="operator:test",
        reason="asset class equity proposal",
        request_id="asset-class-equity",
    )
    assert eq["status"] == "rejected"

    # Crypto order: crypto switch clean + crypto clock always open -> proposed.
    cr = svc.propose_order(
        "BTC/USD",
        "buy",
        "market",
        notional="100",
        actor="operator:test",
        reason="asset class crypto proposal",
        request_id="asset-class-crypto",
    )
    assert cr["status"] == "proposed"
    assert cr["approved_by_risk"] is True
