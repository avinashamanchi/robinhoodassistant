"""Equity snapshots, equity drawdown, benchmark, fill quality and per-source books."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tests.test_paper_lifecycle_e2e import PaperBrokerDouble
from trading_assistant.ops.equity import (
    EquitySnapshotRecorder,
    equity_statistics,
    read_snapshots,
)
from trading_assistant.ops.paper_report import build_report, fill_quality
from trading_assistant.db.models import Order

T0 = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)


def _service(make_service):
    broker = PaperBrokerDouble()
    broker.set_price("AAPL", Decimal("100"))
    broker.set_price("SPY", Decimal("500"))
    return make_service(broker=broker), broker


def test_recorder_writes_at_most_one_snapshot_per_interval_and_never_raises(make_service):
    service, broker = _service(make_service)
    clock = {"now": T0}
    recorder = EquitySnapshotRecorder(service, interval_seconds=900, now=lambda: clock["now"])

    assert recorder.maybe_record() is True
    clock["now"] = T0 + timedelta(minutes=5)
    assert recorder.maybe_record() is False
    clock["now"] = T0 + timedelta(minutes=16)
    broker.get_account = lambda: (_ for _ in ()).throw(ConnectionError("broker down"))
    assert recorder.maybe_record() is False  # logged, not raised

    (snapshot,) = read_snapshots(service.session_factory)
    assert snapshot["equity"] == "100000" and snapshot["benchmark"] == "SPY"
    assert snapshot["benchmark_last"] == "500" and snapshot["source"] == "mock"


def test_equity_statistics_measure_drawdown_benchmark_and_gaps():
    snapshots = [
        {"observed_at": (T0 + timedelta(hours=h)).isoformat(), "equity": e, "benchmark": "SPY", "benchmark_last": b}
        for h, e, b in ((0, "1000", "500"), (1, "1100", "505"), (2, "990", "510"), (8, "1050", "490"))
    ]

    stats = equity_statistics(snapshots)

    assert stats["max_equity_drawdown"] == "110.00" and stats["max_equity_drawdown_pct"] == "10.000"
    assert stats["equity_return_pct"] == "5.000" and stats["benchmark_return_pct"] == "-2.000"
    assert stats["largest_gap_hours"] == 6.0
    assert equity_statistics(snapshots[:1])["snapshots"] == 1


def test_fill_quality_compares_fills_with_the_decision_quote(make_service):
    service, broker = _service(make_service)
    proposal = service.propose_order(
        "AAPL", "buy", "market", qty="1", idempotency_key="optest-quality",
        actor="operator:demo", reason="fill quality", request_id="propose-quality",
    )
    service.approve_order(proposal["order_id"], actor="operator:demo", reason="ok", request_id="approve-quality")
    with service.session_factory() as session:
        broker.fill(session.get(Order, proposal["order_id"]).broker_order_id, Decimal("1"), Decimal("100.20"))
    service.reconciliation.reconcile(actor="operator:demo", reason="fill quality", request_id="reconcile-quality")

    (row,) = fill_quality(service.session_factory)["orders"]

    assert row["source"] == "operator_test" and row["decision_mid"] == "100.0000"
    assert row["average_fill"] == "100.2000" and row["slippage_bps"] == "20.0"


def test_strategy_and_operator_test_trades_are_kept_in_separate_books(make_service):
    service, broker = _service(make_service)
    for key, side, price in (("ap-strategy-buy", "buy", "100"), ("optest-buy", "buy", "100"),
                             ("ap-strategy-sell", "sell", "110"), ("optest-sell", "sell", "95")):
        proposal = service.propose_order(
            "AAPL", side, "market", qty="1", idempotency_key=key,
            actor="operator:demo", reason=key, request_id=f"p-{key}",
        )
        service.approve_order(proposal["order_id"], actor="operator:demo", reason="ok", request_id=f"a-{key}")
        with service.session_factory() as session:
            broker.fill(session.get(Order, proposal["order_id"]).broker_order_id, Decimal("1"), Decimal(price))
        service.reconciliation.reconcile(actor="operator:demo", reason=key, request_id=f"r-{key}")

    with service.session_factory() as session:
        report = build_report(session)

    assert report["pnl_by_source"]["autopilot"]["realized"] == "10.00"
    assert report["pnl_by_source"]["operator_test"]["realized"] == "-5.00"
    assert report["funnel"]["by_source"] == {"autopilot": 2, "operator_test": 2}


def test_a_hung_snapshot_never_blocks_or_overlaps_the_safety_loop(make_service, caplog):
    import asyncio
    import logging
    import threading
    import time

    from trading_assistant.daemon.monitor import Monitor

    release = threading.Event()
    calls = []

    class HungRecorder:
        def maybe_record(self):
            calls.append(1)
            release.wait(5)
            return True

    monitor = Monitor(make_service(), equity_recorder=HungRecorder())
    monitor.equity_timeout = 0.1

    async def scenario():
        started = time.monotonic()
        monitor._schedule_equity_snapshot()
        monitor._schedule_equity_snapshot()          # still running: no second snapshot
        scheduling = time.monotonic() - started
        await monitor._equity_task                   # ends at the timeout, not when the call returns
        return scheduling, time.monotonic() - started

    with caplog.at_level(logging.WARNING):
        scheduling, waited = asyncio.run(scenario())
    release.set()

    assert scheduling < 0.05 and waited < 1.0
    assert calls == [1]
    assert "equity_snapshot_timed_out" in caplog.text
