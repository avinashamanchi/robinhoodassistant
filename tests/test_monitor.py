"""Monitoring daemon: triggers proposals, one-shot, auto-exec, crash-safe."""

from __future__ import annotations

from decimal import Decimal

from trading_assistant.daemon.monitor import Monitor
from trading_assistant.notifications.base import NullNotifier, RecordingNotifier


def _rule(svc, cond, action=None):
    action = action or {"side": "buy", "notional": "100"}
    return svc.create_conditional_rule("AAPL", cond, action)


def test_trigger_creates_proposal_and_notifies(make_service):
    svc = make_service()  # AAPL @ 100
    _rule(svc, {"price_below": 175})
    notifier = RecordingNotifier()
    mon = Monitor(svc, notifier)

    acted = mon.tick()
    assert len(acted) == 1
    assert acted[0]["proposal"]["status"] == "proposed"
    assert acted[0]["executed"] is None
    assert svc.broker.submit_calls == 0          # proposed, NOT executed
    assert len(notifier.sent) == 1
    assert len(svc.get_pending()) == 1


def test_rule_is_one_shot(make_service):
    svc = make_service()
    _rule(svc, {"price_below": 175})
    mon = Monitor(svc, NullNotifier())
    assert len(mon.tick()) == 1
    assert mon.tick() == []                       # already triggered; no re-fire


def test_no_trigger_when_condition_unmet(make_service):
    svc = make_service()
    _rule(svc, {"price_below": 50})               # 100 is not below 50
    assert Monitor(svc, NullNotifier()).tick() == []


def test_auto_execute_requires_preapproved(make_service):
    from trading_assistant.db.models import Rule

    svc = make_service()
    created = _rule(svc, {"price_below": 175})
    # Ad-hoc rule (not pre-approved): flag on, but it must NOT auto-execute.
    assert Monitor(svc, NullNotifier(), auto_execute=True).tick()[0]["executed"] is None
    assert svc.broker.submit_calls == 0

    # Mark it pre-approved (as plan approval would) -> now it auto-executes.
    svc2 = make_service()
    rid = _rule(svc2, {"price_below": 175})["rule_id"]
    with svc2.session_factory() as s:
        s.get(Rule, rid).pre_approved = True
        s.commit()
    acted = Monitor(svc2, NullNotifier(), auto_execute=True).tick()
    assert acted[0]["executed"]["executed"] is True
    assert svc2.broker.submit_calls == 1


def test_crash_safe_rules_persist(make_service):
    svc = make_service()
    _rule(svc, {"price_below": 175})
    # Simulate a restart: a fresh service/monitor on the SAME database.
    svc2 = make_service()
    mon2 = Monitor(svc2, NullNotifier())
    assert mon2.reconcile()["active"] == 1        # rule survived the "restart"
    assert len(mon2.tick()) == 1


def test_daemon_loop_body_runs_clean(make_service):
    # One full loop body: fill sync + daily-loss enforcement + rule tick + daily tasks.
    svc = make_service()
    mon = Monitor(svc, NullNotifier())
    svc.sync_open_orders()
    svc.enforce_daily_loss_limits()
    mon.tick()
    mon.run_daily_tasks()
    svc.write_heartbeat("daemon")
    assert svc.health()["db_ok"] is True and svc.health()["daemon_alive"] is True
