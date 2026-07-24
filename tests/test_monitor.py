"""Monitoring daemon: triggers proposals, one-shot, auto-exec, crash-safe."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import pytest
from sqlalchemy import select

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


def test_rule_claim_is_compare_and_set(make_service):
    svc = make_service()
    rule_id = _rule(svc, {"price_below": 175})["rule_id"]
    first = Monitor(svc, NullNotifier())
    second = Monitor(svc, NullNotifier())

    assert first._claim_rule(rule_id) is True
    assert second._claim_rule(rule_id) is False


def test_rule_returns_to_active_when_proposal_raises(make_service):
    from trading_assistant.db.models import Rule

    svc = make_service()
    rule_id = _rule(svc, {"price_below": 175})["rule_id"]

    def fail_proposal(*args, **kwargs):
        raise ConnectionError("database temporarily unavailable")

    svc.propose_order = fail_proposal
    result = Monitor(svc, NullNotifier()).tick()

    assert result[0]["error"] == "ConnectionError"
    with svc.session_factory() as session:
        assert session.get(Rule, rule_id).state == "active"


def test_rule_does_not_retry_unknown_broker_acceptance(make_service):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.db.models import Order, Rule

    class ResponseLossBroker(MockBroker):
        lose_first_response = True

        def submit_order(self, order):
            result = super().submit_order(order)
            if self.lose_first_response:
                self.lose_first_response = False
                raise ConnectionError("response lost after acceptance")
            return result

    broker = ResponseLossBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    rule_id = _rule(svc, {"price_below": 175})["rule_id"]
    with svc.session_factory() as session:
        session.get(Rule, rule_id).pre_approved = True
        session.commit()
    monitor = Monitor(svc, NullNotifier(), auto_execute=True)

    first = monitor.tick()
    second = monitor.tick()

    assert first[0]["executed"]["status"] == "acceptance_unknown"
    assert first[0]["executed"]["executed"] is False
    assert second == []
    assert len(broker._orders_by_key) == 1
    with svc.session_factory() as session:
        orders = session.execute(select(Order)).scalars().all()
        assert len(orders) == 1
        assert orders[0].idempotency_key == f"rule-{rule_id}"
        assert orders[0].status == "acceptance_unknown"


def test_no_trigger_when_condition_unmet(make_service):
    svc = make_service()
    _rule(svc, {"price_below": 50})               # 100 is not below 50
    assert Monitor(svc, NullNotifier()).tick() == []


def test_tick_fetches_one_quote_per_ticker_even_with_many_rules(make_service):
    from trading_assistant.broker.mock import MockBroker

    class CountingBroker(MockBroker):
        quote_calls = 0

        def get_quote(self, ticker):
            self.quote_calls += 1
            return super().get_quote(ticker)

    broker = CountingBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    _rule(svc, {"price_below": 50})
    _rule(svc, {"price_above": 150})
    broker.quote_calls = 0

    Monitor(svc, NullNotifier()).tick()

    assert broker.quote_calls == 1


def test_tick_does_not_poll_equity_quotes_while_market_is_closed(make_service):
    from trading_assistant.broker.mock import MockBroker

    class CountingBroker(MockBroker):
        quote_calls = 0

        def get_quote(self, ticker):
            self.quote_calls += 1
            return super().get_quote(ticker)

    broker = CountingBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker, market_open=False)
    _rule(svc, {"price_below": 175})
    broker.quote_calls = 0

    assert Monitor(svc, NullNotifier()).tick() == []
    assert broker.quote_calls == 0


def test_closed_equity_clock_does_not_stop_crypto_monitoring(make_service):
    from trading_assistant.broker.mock import MockBroker

    class CountingBroker(MockBroker):
        quote_calls = 0

        def get_quote(self, ticker):
            self.quote_calls += 1
            return super().get_quote(ticker)

    broker = CountingBroker()
    broker.set_price("BTC/USD", Decimal("50000"))
    svc = make_service(broker=broker, market_open=False)
    svc.create_conditional_rule(
        "BTC/USD",
        {"price_below": 60000},
        {"side": "buy", "notional": "100"},
    )
    broker.quote_calls = 0

    acted = Monitor(svc, NullNotifier()).tick()

    assert len(acted) == 1
    assert acted[0]["proposal"]["status"] == "proposed"
    assert broker.quote_calls > 0


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


def test_startup_releases_rule_claim_left_processing_by_crash(make_service):
    from trading_assistant.db.models import Rule

    svc = make_service()
    rule_id = _rule(svc, {"price_below": 175})["rule_id"]
    assert Monitor(svc, NullNotifier())._claim_rule(rule_id)

    summary = Monitor(svc, NullNotifier()).reconcile()

    assert summary["claims_recovered"] == 1
    with svc.session_factory() as session:
        assert session.get(Rule, rule_id).state == "active"


def test_startup_reconcile_syncs_terminal_broker_order_before_positions(make_service):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import OrderResult, OrderStatus, Position
    from trading_assistant.db.models import Order

    broker = MockBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = svc.propose_order("AAPL", "buy", "market", qty="4")["order_id"]
    svc.approve_order(oid, actor="operator:test", reason="monitor reconciliation test")

    with svc.session_factory() as s:
        local = s.get(Order, oid)
        broker_id = local.broker_order_id
        client_id = local.idempotency_key
        assert local.status == "submitted"

    # Simulate Alpaca having filled while the daemon was offline.
    filled = OrderResult(
        client_id,
        broker_id,
        OrderStatus.FILLED,
        filled_qty=Decimal("4"),
        avg_fill_price=Decimal("100"),
    )
    broker._orders_by_id[broker_id] = filled
    broker._orders_by_key[client_id] = filled
    broker._positions["AAPL"] = Position(
        "AAPL", Decimal("4"), Decimal("100"), Decimal("100")
    )

    summary = Monitor(svc, NullNotifier()).reconcile()

    assert summary["order_sync"]["newly_filled"] == 1
    assert summary["position_reconciliation"]["reconciled"] is True
    with svc.session_factory() as s:
        assert s.get(Order, oid).status == "filled"


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


def test_slow_daily_analysis_does_not_block_heartbeat_cycles(make_service):
    class SlowShadow:
        def grade_due(self):
            time.sleep(0.12)
            return 0

        def run_once(self):
            return []

    svc = make_service()
    heartbeat_count = 0
    original_write = svc.write_heartbeat

    def count_heartbeat(source="daemon"):
        nonlocal heartbeat_count
        heartbeat_count += 1
        return original_write(source)

    svc.write_heartbeat = count_heartbeat
    monitor = Monitor(
        svc,
        NullNotifier(),
        poll_interval_seconds=0.01,
        cycle_timeout_seconds=0.2,
        daily_task_timeout_seconds=0.02,
        shadow=SlowShadow(),
    )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(stop))
        await asyncio.sleep(0.09)
        stop.set()
        await asyncio.wait_for(task, timeout=0.5)

    asyncio.run(scenario())

    assert heartbeat_count >= 3


def test_core_cycle_can_recover_after_a_transient_failure(make_service):
    monitor = Monitor(make_service(), NullNotifier(), cycle_timeout_seconds=0.2)
    calls = 0

    def flaky_cycle():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("temporary broker disconnect")

    monitor._core_cycle = flaky_cycle

    async def scenario():
        with pytest.raises(ConnectionError):
            await monitor._bounded_core_cycle()
        await monitor._bounded_core_cycle()

    asyncio.run(scenario())
    assert calls == 2


def test_runtime_reconciliation_failure_trips_switches_before_rules(make_service):
    from trading_assistant.assets import AssetClass
    from trading_assistant.risk.killswitch import KillSwitch

    svc = make_service()
    monitor = Monitor(svc, NullNotifier())
    svc.sync_open_orders = lambda: {
        "synced": 0,
        "newly_filled": 0,
        "failed": 1,
        "fills_repaired": 0,
    }
    rules_evaluated = False

    def unsafe_tick():
        nonlocal rules_evaluated
        rules_evaluated = True

    monitor.tick = unsafe_tick

    with pytest.raises(RuntimeError, match="order reconciliation"):
        monitor._core_cycle()

    assert rules_evaluated is False
    with svc.session_factory() as session:
        assert KillSwitch.is_tripped(session, AssetClass.EQUITY)
        assert KillSwitch.is_tripped(session, AssetClass.CRYPTO)


def test_startup_reconciliation_failure_trips_switches_and_stops(make_service):
    from trading_assistant.assets import AssetClass
    from trading_assistant.risk.killswitch import KillSwitch

    svc = make_service()
    monitor = Monitor(svc, NullNotifier(), cycle_timeout_seconds=0.2)
    monitor.reconcile = lambda: {
        "active": 1,
        "triggered": 0,
        "order_sync": {"synced": 0, "newly_filled": 0, "failed": 1},
        "position_reconciliation": {"reconciled": True, "drift": {}},
    }

    with pytest.raises(RuntimeError, match="reconciliation"):
        asyncio.run(monitor.run(asyncio.Event()))

    with svc.session_factory() as session:
        assert KillSwitch.is_tripped(session, AssetClass.EQUITY)
        assert KillSwitch.is_tripped(session, AssetClass.CRYPTO)
