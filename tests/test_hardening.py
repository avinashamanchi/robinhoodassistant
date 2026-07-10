"""Phase 5 hardening: partial fills, fill idempotency, cancel/replace,
startup reconciliation, and an end-to-end kill-switch drill."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_assistant.assets import AssetClass
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import Position
from trading_assistant.db.models import Fill


def _submitted(svc, notional="400") -> int:
    order_id = svc.propose_order("AAPL", "buy", "market", notional=notional)["order_id"]
    svc.approve_order(order_id)  # -> SUBMITTED
    return order_id


# ── partial fills ───────────────────────────────────────────────
def test_partial_then_full_fill(make_service):
    svc = make_service()                       # AAPL @ 100 -> target 4 shares
    oid = _submitted(svc)
    r1 = svc.record_fill(oid, qty="1.5", price="100")
    assert r1["status"] == "partially_filled"
    r2 = svc.record_fill(oid, qty="2.5", price="100")
    assert r2["status"] == "filled"


def test_duplicate_fill_is_idempotent(make_service):
    svc = make_service()
    oid = _submitted(svc)
    first = svc.record_fill(oid, qty="2", price="100", broker_fill_id="fill-1")
    dup = svc.record_fill(oid, qty="2", price="100", broker_fill_id="fill-1")
    assert first["duplicate"] is False
    assert dup["duplicate"] is True
    assert dup["filled_qty"] == first["filled_qty"]   # not double-counted


# ── cancel / replace ────────────────────────────────────────────
def test_cancel_live_order(make_service):
    svc = make_service()
    oid = _submitted(svc)
    result = svc.cancel_live_order(oid)
    assert result["status"] == "canceled"
    assert "error" in svc.cancel_live_order(oid)       # cannot cancel twice


def test_replace_order(make_service):
    svc = make_service()
    oid = _submitted(svc)
    result = svc.replace_order(
        oid, ticker="AAPL", side="buy", order_type="market", notional="200"
    )
    assert result["canceled"]["status"] == "canceled"
    assert result["replacement"]["status"] == "proposed"


# ── startup reconciliation ──────────────────────────────────────
def test_reconcile_detects_drift(make_service):
    # Broker reports a position that local fills don't account for.
    broker = MockBroker(positions=[Position("AAPL", Decimal("10"), Decimal("100"), Decimal("100"))])
    svc = make_service(broker=broker)
    result = svc.reconcile_positions()
    assert result["reconciled"] is False
    assert "AAPL" in result["drift"]


def test_reconcile_clean_when_matching(make_service):
    svc = make_service()  # no positions, no fills
    assert svc.reconcile_positions()["reconciled"] is True


# ── kill-switch drill (end-to-end) ──────────────────────────────
def test_killswitch_drill(make_service):
    svc = make_service()
    now = datetime.now(timezone.utc)
    # Insert a realized -$5,000 round trip for today, directly as fills.
    with svc.session_factory() as s:
        s.add(Fill(ticker="AAPL", side="buy", qty=Decimal("100"), price=Decimal("100"), filled_at=now))
        s.add(Fill(ticker="AAPL", side="sell", qty=Decimal("100"), price=Decimal("50"), filled_at=now))
        s.commit()

    tripped = svc.enforce_daily_loss_limits()
    assert tripped["equity"] is True
    assert tripped["crypto"] is False          # crypto independent

    # New equity orders are now blocked...
    blocked = svc.propose_order("AAPL", "buy", "market", notional="100")
    assert blocked["status"] == "rejected"
    assert any("kill switch" in r for r in blocked["risk_reasons"])

    # ...until a human resets the equity switch.
    svc.reset_killswitch(AssetClass.EQUITY)
    ok = svc.propose_order("AAPL", "buy", "market", notional="100")
    assert ok["status"] == "proposed"
