"""Phase 8 rule types: trailing stop (persisted HWM), time stop, OCO groups."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from trading_assistant.daemon import rules_engine
from trading_assistant.daemon.monitor import Monitor
from trading_assistant.db.models import Order, Rule
from trading_assistant.notifications.base import NullNotifier


def _add_rule(svc, **kw):
    ticker = kw.pop("ticker", "AAPL")
    kind = kw.pop("kind", "price")
    plan_id = kw.pop("plan_id", None)
    condition = json.loads(kw.pop("condition_json", "{}"))
    action = json.loads(
        kw.pop("action_json", json.dumps({"side": "sell", "qty": "5"}))
    )
    deadline = kw.pop("deadline", None)
    assert not kw
    if kind == "time":
        condition = {"type": "time", "deadline": deadline.isoformat()}
    result = svc.create_conditional_rule(
        ticker,
        condition,
        action,
        kind=kind,
        group_key=f"plan-{plan_id}" if plan_id is not None else None,
        plan_id=plan_id,
    )
    return result["rule_id"]


# ── pure logic ──────────────────────────────────────────────────
def test_update_trailing_stop():
    fires, hwm = rules_engine.update_trailing_stop(None, Decimal("100"), 10)
    assert hwm == Decimal("100") and fires is False
    fires, hwm = rules_engine.update_trailing_stop(Decimal("100"), Decimal("120"), 10)
    assert hwm == Decimal("120") and fires is False       # new high, no fire
    fires, hwm = rules_engine.update_trailing_stop(Decimal("120"), Decimal("107"), 10)
    assert hwm == Decimal("120") and fires is True        # 107 <= 108 threshold


def test_time_stop_fires():
    now = datetime(2022, 6, 1, tzinfo=timezone.utc)
    assert rules_engine.time_stop_fires(now - timedelta(hours=1), now) is True
    assert rules_engine.time_stop_fires(now + timedelta(hours=1), now) is False


# ── trailing HWM persists across a daemon restart ───────────────
def test_trailing_hwm_persists_across_restart(make_service):
    svc = make_service()  # AAPL @ 100
    _add_rule(svc, kind="trailing", condition_json=json.dumps({"trailing_stop_pct": 10}))
    mon = Monitor(svc, NullNotifier())

    assert mon.tick() == []                                # hwm=100, no fire
    svc.broker.set_price("AAPL", Decimal("120"))
    assert mon.tick() == []                                # hwm advances to 120
    with svc.session_factory() as s:
        assert s.execute(select(Rule)).scalar_one().hwm == Decimal("120")

    # Restart: fresh service+monitor on the SAME db. Price 105 must fire off the
    # PERSISTED hwm=120 (threshold 108), not a reset-to-105 hwm.
    svc2 = make_service()
    svc2.broker.set_price("AAPL", Decimal("105"))
    acted = Monitor(svc2, NullNotifier()).tick()
    assert len(acted) == 1                                 # fired -> HWM survived restart


# ── OCO: a stop firing cancels the plan's siblings ──────────────
def test_oco_cancels_siblings_atomically(make_service):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import Position

    broker = MockBroker(
        positions=[Position("AAPL", Decimal("5"), Decimal("100"), Decimal("100"))]
    )
    svc = make_service(broker=broker)
    _add_rule(svc, kind="entry", plan_id=1, condition_json=json.dumps({"price_below": 80}))
    _add_rule(svc, kind="target", plan_id=1, condition_json=json.dumps({"price_above": 200}))
    _add_rule(
        svc,
        kind="stop",
        plan_id=1,
        condition_json=json.dumps({"price_below": 90}),
    )

    svc.broker.set_price("AAPL", Decimal("85"))            # only the stop's condition is true
    acted = Monitor(svc, NullNotifier(), auto_execute=True).tick()
    assert len(acted) == 1 and acted[0]["oco_canceled"] == 2
    assert acted[0]["executed"] is None
    assert broker._orders_by_key == {}

    with svc.session_factory() as s:
        states = {r.kind: r.state for r in s.execute(select(Rule)).scalars()}
    assert states["stop"] == "triggered"
    assert states["entry"] == "canceled" and states["target"] == "canceled"


def test_full_exit_is_capped_to_unreserved_live_position(make_service):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import Position

    class RecordingBroker(MockBroker):
        submitted = []

        def submit_order(self, order):
            self.submitted.append(order)
            return super().submit_order(order)

    broker = RecordingBroker(
        positions=[Position("AAPL", Decimal("5"), Decimal("100"), Decimal("100"))]
    )
    svc = make_service(broker=broker)
    _add_rule(
        svc,
        kind="stop",
        plan_id=2,
        action_json=json.dumps({"side": "sell", "qty": "10"}),
        condition_json=json.dumps({"price_below": 95}),
    )
    broker.set_price("AAPL", Decimal("90"))

    acted = Monitor(svc, NullNotifier(), auto_execute=True).tick()

    assert acted[0]["executed"] is None
    assert broker.submitted == []
    with svc.session_factory() as session:
        proposal = session.execute(select(Order)).scalar_one()
    assert proposal.qty == Decimal("5")
