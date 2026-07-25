"""Monitoring daemon: triggers proposals, one-shot, auto-exec, crash-safe."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from threading import Event

import pytest
from sqlalchemy import select

from trading_assistant.daemon.monitor import Monitor
from trading_assistant.db.models import AuditEvent
from trading_assistant.notifications.base import NullNotifier, RecordingNotifier


def _rule(svc, cond, action=None):
    action = action or {"side": "buy", "notional": "100"}
    return svc.create_conditional_rule(
        "AAPL",
        cond,
        action,
        actor="operator:test",
        reason="monitor test rule setup",
        request_id="monitor-test-rule",
    )


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
    with svc.session_factory() as session:
        audit = session.query(AuditEvent).filter_by(
            action="order.propose",
            target_id=str(acted[0]["proposal"]["order_id"]),
        ).one()
    assert audit.actor == "daemon:rules"
    assert audit.reason == "daemon conditional rule evaluation"
    assert audit.request_id


def test_rule_is_one_shot(make_service):
    svc = make_service()
    _rule(svc, {"price_below": 175})
    mon = Monitor(svc, NullNotifier())
    assert len(mon.tick()) == 1
    assert mon.tick() == []                       # already triggered; no re-fire


def test_monitor_tick_delegates_only_to_rule_worker(make_service):
    svc = make_service()
    sentinel = [{"delegated": True}]

    class StubWorker:
        calls = 0

        def tick(self, **context):
            self.calls += 1
            return sentinel

    worker = StubWorker()
    svc.propose_order = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("monitor must not propose directly")
    )
    svc.approve_order = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("monitor must not approve or submit")
    )

    monitor = Monitor(
        svc, NullNotifier(), auto_execute=True, rule_worker=worker
    )

    assert monitor.tick() is sentinel
    assert worker.calls == 1
    assert svc.broker.submit_calls == 0


def test_rule_lease_survives_when_application_crashes_before_transaction(
    make_service,
    caplog,
):
    from trading_assistant.db.models import Rule

    svc = make_service()
    rule_id = _rule(svc, {"price_below": 175})["rule_id"]

    def fail_before(phase):
        if phase == "before_transaction":
            raise ConnectionError("database temporarily unavailable")

    svc.rule_application.crash_hook = fail_before
    result = Monitor(svc, NullNotifier()).tick()

    assert result[0]["error"] == "rule_evaluation_failed"
    assert "database temporarily unavailable" not in caplog.text
    assert "ConnectionError" not in caplog.text
    with svc.session_factory() as session:
        assert session.get(Rule, rule_id).state == "active"


def test_monitor_never_submits_even_when_auto_execute_argument_is_true(make_service):
    svc = make_service()
    _rule(svc, {"price_below": 175})

    acted = Monitor(svc, NullNotifier(), auto_execute=True).tick()

    assert acted[0]["proposal"]["status"] == "proposed"
    assert acted[0]["executed"] is None
    assert svc.broker.submit_calls == 0


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
    _rule(svc, {"price_below": 175})
    _rule(svc, {"price_above": 150})
    broker.quote_calls = 0

    acted = Monitor(svc, NullNotifier()).tick()

    assert len(acted) == 1
    assert broker.quote_calls == 1


def test_tick_quote_cache_covers_two_ticker_risk_snapshot(make_service):
    from collections import Counter

    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import Position

    class CountingBroker(MockBroker):
        def __init__(self):
            super().__init__(
                positions=[
                    Position(
                        "MSFT",
                        Decimal("1"),
                        Decimal("100"),
                        Decimal("100"),
                    )
                ]
            )
            self.quote_calls = Counter()

        def get_quote(self, ticker):
            self.quote_calls[ticker.upper()] += 1
            return super().get_quote(ticker)

    broker = CountingBroker()
    broker.set_price("MSFT", Decimal("100"))
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    svc.create_conditional_rule(
        "MSFT",
        {"price_above": 150},
        {"side": "buy", "notional": "100"},
        actor="operator:test",
        reason="monitor multi-symbol rule setup",
        request_id="monitor-msft-rule",
    )
    _rule(svc, {"price_below": 175})

    acted = Monitor(svc, NullNotifier()).tick()

    assert len(acted) == 1
    assert acted[0]["proposal"]["status"] == "proposed"
    assert broker.quote_calls == {"MSFT": 1, "AAPL": 1}


def test_snapshot_expansion_flows_back_to_tick_quote_cache(make_service):
    from collections import Counter

    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import Position

    class CountingBroker(MockBroker):
        def __init__(self):
            super().__init__(
                positions=[
                    Position(
                        "MSFT",
                        Decimal("1"),
                        Decimal("100"),
                        Decimal("100"),
                    )
                ]
            )
            self.quote_calls = Counter()

        def get_quote(self, ticker):
            self.quote_calls[ticker.upper()] += 1
            return super().get_quote(ticker)

    broker = CountingBroker()
    broker.set_price("AAPL", Decimal("100"))
    broker.set_price("MSFT", Decimal("100"))
    svc = make_service(broker=broker)
    _rule(svc, {"price_below": 175})
    svc.create_conditional_rule(
        "MSFT",
        {"price_above": 150},
        {"side": "buy", "notional": "100"},
        actor="operator:test",
        reason="monitor grouped rule setup",
        request_id="monitor-grouped-rule",
    )

    acted = Monitor(svc, NullNotifier()).tick()

    assert len(acted) == 1
    assert acted[0]["proposal"]["status"] == "proposed"
    assert broker.quote_calls == {"AAPL": 1, "MSFT": 1}


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
        actor="operator:test",
        reason="monitor crypto rule setup",
        request_id="monitor-crypto-rule",
    )
    broker.quote_calls = 0

    acted = Monitor(svc, NullNotifier()).tick()

    assert len(acted) == 1
    assert acted[0]["proposal"]["status"] == "proposed"
    assert broker.quote_calls > 0


def test_preapproved_database_row_is_rejected_instead_of_autoexecuted(make_service):
    from trading_assistant.db.models import Rule

    svc = make_service()
    rid = _rule(svc, {"price_below": 175})["rule_id"]
    with svc.session_factory() as s:
        s.get(Rule, rid).pre_approved = True
        s.commit()

    acted = Monitor(svc, NullNotifier(), auto_execute=True).tick()

    assert acted[0]["proposal"] is None
    assert acted[0]["error"] == "rule_load_failed"
    assert svc.broker.submit_calls == 0


def test_crash_safe_rules_persist(make_service):
    svc = make_service()
    _rule(svc, {"price_below": 175})
    # Simulate a restart: a fresh service/monitor on the SAME database.
    svc2 = make_service()
    mon2 = Monitor(svc2, NullNotifier())
    assert mon2.reconcile()["active"] == 1        # rule survived the "restart"
    assert len(mon2.tick()) == 1


def test_startup_no_longer_recovers_legacy_per_rule_processing_claims(make_service):
    svc = make_service()

    summary = Monitor(svc, NullNotifier()).reconcile()

    assert summary["claims_recovered"] == 0
    assert not hasattr(Monitor, "_claim_rule")


def test_startup_reconcile_syncs_terminal_broker_order_before_positions(make_service):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.broker.models import (
        BrokerFill,
        OrderResult,
        OrderStatus,
        Position,
    )
    from trading_assistant.db.models import Order, utcnow

    class ActivityBroker(MockBroker):
        activities = []

        def get_fill_activities(self, after=None):
            return list(self.activities)

    broker = ActivityBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    oid = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        qty="4",
        actor="operator:test",
        reason="monitor reconciliation proposal",
        request_id="monitor-reconciliation-proposal",
    )["order_id"]
    svc.approve_order(
        oid,
        actor="operator:test",
        reason="monitor reconciliation test",
        request_id="monitor-reconciliation-approval",
    )

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
    broker.activities = [
        BrokerFill(
            broker_fill_id="startup-fill-1",
            broker_order_id=broker_id,
            ticker="AAPL",
            side="buy",
            qty=Decimal("4"),
            price=Decimal("100"),
            filled_at=utcnow(),
        )
    ]
    broker._positions["AAPL"] = Position(
        "AAPL", Decimal("4"), Decimal("100"), Decimal("100")
    )

    summary = Monitor(svc, NullNotifier()).reconcile()

    assert summary["order_sync"]["newly_filled"] == 1
    assert summary["position_reconciliation"]["reconciled"] is True
    with svc.session_factory() as s:
        assert s.get(Order, oid).status == "filled"


def test_startup_monitor_delegates_to_reconciliation_service(make_service):
    from trading_assistant.orders.reconciliation import ReconciliationReport

    svc = make_service()
    report = ReconciliationReport(1, (), 2, 3, ())
    observed_context = {}

    def sync_with_context(**context):
        observed_context.update(context)
        return svc.serialize_reconciliation_report(report)

    svc.sync_open_orders = sync_with_context

    summary = Monitor(svc, NullNotifier()).reconcile()

    assert summary["order_sync"]["resolved_unknown"] == 1
    assert summary["order_sync"]["synced_orders"] == 2
    assert summary["order_sync"]["inserted_fills"] == 3
    assert observed_context["actor"] == "daemon:startup"
    assert (
        observed_context["reason"]
        == "daemon startup broker order reconciliation"
    )
    assert observed_context["request_id"]


def test_daemon_loop_body_runs_clean(make_service):
    # One full loop body: fill sync + daily-loss enforcement + rule tick + daily tasks.
    svc = make_service()
    mon = Monitor(svc, NullNotifier())
    svc.sync_open_orders(
        actor="operator:test",
        reason="monitor loop body setup",
        request_id="monitor-loop-body-sync",
    )
    svc.enforce_daily_loss_limits(
        actor="daemon:test",
        reason="monitor loop daily loss check",
        request_id="monitor-loop-daily-loss",
    )
    mon.tick()
    mon.run_daily_tasks()
    svc.write_heartbeat("daemon")
    assert svc.health()["db_ok"] is True and svc.health()["daemon_alive"] is True


def test_slow_daily_analysis_does_not_block_heartbeat_cycles(make_service):
    daily_started = Event()
    daily_release = Event()
    daily_finished = Event()

    class SlowShadow:
        def grade_due(self):
            daily_started.set()
            try:
                daily_release.wait()
                return 0
            finally:
                daily_finished.set()

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
        cycle_timeout_seconds=1.0,
        daily_task_timeout_seconds=0.02,
        shadow=SlowShadow(),
    )

    async def scenario():
        async def wait_until(condition, description):
            async def poll():
                while not condition():
                    await asyncio.sleep(0.01)

            try:
                await asyncio.wait_for(poll(), timeout=5)
            except TimeoutError:
                pytest.fail(f"timed out waiting for {description}")

        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(stop))
        try:
            await wait_until(daily_started.is_set, "daily analysis to start")
            blocked_heartbeat_count = heartbeat_count
            await wait_until(
                lambda: heartbeat_count >= blocked_heartbeat_count + 3,
                "three heartbeats during daily analysis",
            )
            assert daily_release.is_set() is False
            assert daily_finished.is_set() is False
        finally:
            daily_release.set()
            try:
                if daily_started.is_set():
                    await wait_until(
                        daily_finished.is_set,
                        "daily analysis to finish",
                    )
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=5)
                if monitor._daily_task is not None:
                    await asyncio.wait_for(monitor._daily_task, timeout=5)

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
    from trading_assistant.risk.breakers import BreakerScope

    svc = make_service()
    monitor = Monitor(svc, NullNotifier())
    svc.sync_open_orders = lambda **context: {
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
    state = svc.breakers.get(BreakerScope.operator_global())
    assert state is not None and state.tripped is True
    assert state.actor == "daemon:monitor"


def test_startup_reconciliation_failure_trips_switches_and_stops(make_service):
    from trading_assistant.risk.breakers import BreakerScope

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

    state = svc.breakers.get(BreakerScope.operator_global())
    assert state is not None and state.tripped is True
    assert state.actor == "daemon:startup"
