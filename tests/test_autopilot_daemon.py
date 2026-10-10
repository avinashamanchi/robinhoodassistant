"""The daemon as single owner of autopilot scheduling and execution.

Mock broker, fake clocks and disposable databases only. These tests exercise
the runner and the monitor in-process; they are not evidence about the
operator's installed daemon or any broker account.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trading_assistant.assets import AssetClass
from trading_assistant.autopilot import Autopilot
from trading_assistant.autopilot.evidence import EvidenceStore
from trading_assistant.autopilot.readiness import REPORT_RELATIVE, ReadinessReport
from trading_assistant.autopilot.runner import AutopilotRunner
from trading_assistant.ops.tenure import TenureLost
from trading_assistant.risk.clock import FakeClock
from trading_assistant.signals.models import MarketFeatures

NY = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 6, 9, 30, tzinfo=NY)


def _features(sma20=105):
    return MarketFeatures(
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        as_of=datetime.now(timezone.utc) - timedelta(hours=1),
        sma_20=sma20, sma_50=100, sma_200=90, last_close=100,
    )


class Guard:
    def __init__(self, owned_checks=10**6):
        self.owned_checks = owned_checks
        self.checks = 0
        self.lost = False
        self.closed = False

    def ensure_owned(self):
        self.checks += 1
        if self.checks > self.owned_checks:
            self.lost = True
            raise TenureLost()


def _config(app_config, mode):
    return app_config.model_copy(
        update={"autopilot": app_config.autopilot.model_copy(
            update={"mode": mode, "universe": ["AAPL"], "notional_per_trade": Decimal("100")}
        )}
    )


def _runner(app_config, make_service, tmp_path, *, mode="observe", now=None,
            provider=None, guard=None, opened=OPEN):
    service = make_service()
    service._clocks[AssetClass.EQUITY] = FakeClock(is_open=True, most_recent_open=opened.astimezone(timezone.utc))
    config = _config(app_config, mode)
    service.config = config
    clock = {"now": now or (opened + timedelta(minutes=45)).astimezone(timezone.utc)}
    autopilot = Autopilot(
        service,
        provider or (lambda _symbol: _features()),
        universe=["AAPL"],
        notional_per_trade=Decimal("100"),
        max_orders_per_day=8,
        dry_run=True,
        tenure_guard=guard,
    )
    runner = AutopilotRunner(
        config=config,
        service=service,
        autopilot=autopilot,
        store=EvidenceStore(service.session_factory, actor="autopilot:sma_trend"),
        root=tmp_path,
        tenure_guard=guard,
        now=lambda: clock["now"],
        installation_check=lambda _root: None,
    )
    return runner, service, clock


# ── scheduling ────────────────────────────────────────────────────────────────
def test_cycle_is_not_due_before_the_open_offset_or_when_closed(app_config, make_service, tmp_path):
    runner, service, clock = _runner(app_config, make_service, tmp_path,
                                     now=(OPEN + timedelta(minutes=10)).astimezone(timezone.utc))
    assert runner.run_if_due() is None
    service._clocks[AssetClass.EQUITY].set_open(False)
    clock["now"] = (OPEN + timedelta(hours=8)).astimezone(timezone.utc)
    assert runner.run_if_due() is None
    assert runner.store.cycles() == []


def test_observe_runs_once_per_session_and_records_evidence(app_config, make_service, tmp_path):
    runner, service, clock = _runner(app_config, make_service, tmp_path)

    assert runner.run_if_due() == {"session": "2026-10-06", "result": "observed"}
    assert runner.run_if_due() is None              # already recorded
    assert service.broker.submit_calls == 0

    (record,) = runner.store.cycles()
    detail = record.detail
    assert detail["mode"] == "observe" and detail["execution"] == "simulated"
    assert detail["decisions"][0]["reason"] == "enter_long"
    assert detail["decisions"][0]["feature_age_hours"] is not None
    assert detail["intended_actions"] == [
        {"symbol": "AAPL", "side": "buy", "qty": None, "notional": "100"}
    ]
    assert detail["orders"] == []
    report = tmp_path / REPORT_RELATIVE
    assert report.stat().st_mode & 0o777 == 0o600

    next_open = datetime(2026, 10, 7, 9, 30, tzinfo=NY)
    service._clocks[AssetClass.EQUITY] = FakeClock(is_open=True, most_recent_open=next_open.astimezone(timezone.utc))
    clock["now"] = (next_open + timedelta(minutes=45)).astimezone(timezone.utc)
    assert runner.run_if_due() == {"session": "2026-10-07", "result": "observed"}


def test_paper_mode_without_readiness_is_blocked_and_places_nothing(app_config, make_service, tmp_path):
    runner, service, _clock = _runner(app_config, make_service, tmp_path, mode="paper")

    assert runner.run_if_due()["result"] == "blocked"
    assert service.broker.submit_calls == 0
    (record,) = runner.store.cycles()
    assert record.detail["execution"] == "simulated"
    assert "operator_approval" in record.detail["readiness"]["unmet"]


def test_paper_mode_with_readiness_executes_through_the_risk_engine(app_config, make_service, tmp_path, monkeypatch):
    runner, service, _clock = _runner(app_config, make_service, tmp_path, mode="paper")
    monkeypatch.setattr(runner, "_readiness", lambda now: _ready_report(runner, now))

    assert runner.run_if_due()["result"] == "executed"
    assert service.broker.submit_calls == 1
    (record,) = runner.store.cycles()
    assert record.detail["execution"] == "paper"
    assert record.detail["orders"][0]["symbol"] == "AAPL"


def _ready_report(runner, now):
    from trading_assistant.autopilot.readiness import evaluate_readiness

    report = evaluate_readiness(
        config=runner.config, store=runner.store, root=runner.root, now=now,
        tenure_guard=runner.tenure_guard, breakers=runner.service.breakers,
        installation_check=lambda _r: None,
    )
    return ReadinessReport(**{**report.__dict__, "ready": True})


# ── ownership, failure, cancellation ──────────────────────────────────────────
def test_tenure_lost_before_the_cycle_stops_without_evidence(app_config, make_service, tmp_path):
    runner, _service, _clock = _runner(app_config, make_service, tmp_path, guard=Guard(owned_checks=0))
    with pytest.raises(TenureLost):
        runner.run_if_due()
    assert runner.store.cycles() == []


def test_tenure_lost_during_the_cycle_stops_without_completion(app_config, make_service, tmp_path, monkeypatch):
    guard = Guard(owned_checks=2)  # runner check + engine cycle start, then lost
    runner, service, _clock = _runner(app_config, make_service, tmp_path, mode="paper", guard=guard)
    monkeypatch.setattr(runner, "_readiness", lambda now: _ready_report(runner, now))
    with pytest.raises(TenureLost):
        runner.run_if_due()
    assert service.broker.submit_calls == 0
    assert runner.store.cycles() == []


def test_failed_cycles_are_recorded_and_retried_a_bounded_number_of_times(app_config, make_service, tmp_path, monkeypatch):
    runner, _service, _clock = _runner(app_config, make_service, tmp_path)

    def broken(**_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(runner.autopilot, "run_once", broken)
    results = [runner.run_if_due() for _ in range(5)]
    assert [r["result"] if r else None for r in results] == ["failed", "failed", "failed", None, None]
    assert [r.result_code for r in runner.store.cycles()] == ["failed"] * 3
    assert runner.store.cycles()[0].detail["error"] == "RuntimeError"


def test_cancelled_cycle_is_not_recorded_and_reruns_without_duplicates(app_config, make_service, tmp_path, monkeypatch):
    calls = []

    def provider(_symbol):
        calls.append(1)
        if len(calls) == 1:
            runner.request_cancel()  # shutdown arrives mid-cycle
        return _features()

    runner, service, _clock = _runner(app_config, make_service, tmp_path, mode="paper", provider=provider)
    runner.autopilot.universe = ["AAPL", "MSFT"]
    service.broker.set_price("MSFT", Decimal("100"))
    monkeypatch.setattr(runner, "_readiness", lambda now: _ready_report(runner, now))

    assert runner.run_if_due()["result"] == "cancelled"
    assert runner.store.cycles() == []
    first_submissions = service.broker.submit_calls

    assert runner.run_if_due()["result"] == "executed"
    with service.session_factory() as s:
        from sqlalchemy import select

        from trading_assistant.db.models import Order

        tickers = [o.ticker for o in s.execute(select(Order)).scalars()]
    assert sorted(tickers) == ["AAPL", "MSFT"]
    # AAPL was submitted before the cancel took effect; the rerun found it in
    # flight and placed only MSFT: no duplicate.
    assert first_submissions == 1
    assert service.broker.submit_calls == 2


def test_unavailable_data_is_recorded_as_degraded(app_config, make_service, tmp_path):
    def unavailable(_symbol):
        raise RuntimeError("bars unavailable")

    runner, _service, _clock = _runner(app_config, make_service, tmp_path, provider=unavailable)
    assert runner.run_if_due()["result"] == "degraded"
    assert runner.store.cycles()[0].detail["counts"] == {"features_unavailable": 1}


def test_concurrent_duplicate_schedulers_place_one_order(app_config, make_service, tmp_path, monkeypatch):
    runner, service, _clock = _runner(app_config, make_service, tmp_path, mode="paper")
    monkeypatch.setattr(runner, "_readiness", lambda now: _ready_report(runner, now))
    twin = AutopilotRunner(
        config=runner.config, service=service,
        autopilot=Autopilot(service, lambda _s: _features(), universe=["AAPL"],
                            notional_per_trade=Decimal("100"), max_orders_per_day=8, dry_run=True),
        store=runner.store, root=tmp_path, now=runner.now, installation_check=lambda _r: None,
    )
    monkeypatch.setattr(twin, "_readiness", lambda now: _ready_report(twin, now))

    threads = [threading.Thread(target=r.run_if_due) for r in (runner, twin)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert service.broker.submit_calls == 1


# ── the monitor hosts the runner ──────────────────────────────────────────────
class StubRunner:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0
        self.cancels = 0

    def run_if_due(self):
        self.calls += 1
        return self.behaviour(self)

    def request_cancel(self):
        self.cancels += 1


def _monitor(make_service, runner, **kwargs):
    from trading_assistant.daemon.monitor import Monitor

    service = make_service()
    return Monitor(service, poll_interval_seconds=0.01, cycle_timeout_seconds=5,
                   autopilot=runner, **kwargs), service


def test_monitor_stops_on_autopilot_tenure_loss_without_tripping_kill_switches(make_service):
    def lose(_runner):
        raise TenureLost()

    monitor, service = _monitor(make_service, StubRunner(lose))
    with pytest.raises(TenureLost):
        asyncio.run(asyncio.wait_for(monitor.run(), timeout=10))
    from trading_assistant.risk.breakers import BreakerScope

    state = service.breakers.get(BreakerScope.operator_global())
    assert state is None or not state.tripped


def test_monitor_isolates_autopilot_failures_and_cancels_on_shutdown(make_service):
    def fail(_runner):
        raise RuntimeError("boom")

    runner = StubRunner(fail)
    monitor, service = _monitor(make_service, runner)
    stop = asyncio.Event()

    async def scenario():
        task = asyncio.create_task(monitor.run(stop))
        while runner.calls < 3:
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))
    assert runner.calls >= 3
    assert runner.cancels >= 1                     # shutdown cancelled the cycle
    from trading_assistant.risk.breakers import BreakerScope

    state = service.breakers.get(BreakerScope.operator_global())
    assert state is None or not state.tripped


def test_monitor_requests_cancellation_when_a_cycle_times_out(make_service):
    release = threading.Event()

    def slow(_runner):
        release.wait(5)
        return None

    runner = StubRunner(slow)
    monitor, _service = _monitor(make_service, runner, autopilot_timeout_seconds=0.05)

    async def scenario():
        task = asyncio.create_task(monitor._bounded_autopilot())
        await asyncio.sleep(0.2)
        release.set()
        await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))
    assert runner.cancels == 1


def test_monitor_never_overlaps_autopilot_cycles(make_service):
    release = threading.Event()

    def slow(_runner):
        release.wait(5)

    runner = StubRunner(slow)
    monitor, _service = _monitor(make_service, runner)

    async def scenario():
        monitor._schedule_autopilot()
        await asyncio.sleep(0.05)
        monitor._schedule_autopilot()
        monitor._schedule_autopilot()
        release.set()
        await monitor._autopilot_task

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))
    assert runner.calls == 1


# ── the daemon builds the single owner ───────────────────────────────────────
def _container(service):
    return SimpleNamespace(service=service, rate_limiter=None, session_factory=service.session_factory)


def test_daemon_builds_no_autopilot_when_off(app_config, make_service):
    from trading_assistant.daemon.main import _build_autopilot_runner

    runner = _build_autopilot_runner(
        app_config, SimpleNamespace(), container=_container(make_service()),
        runtime_tenure_guard=None, historical_alpaca_client_factory=None,
        historical_cache_dir=".cache/bars",
    )
    assert runner is None


def test_daemon_builds_a_dry_running_runner_bound_to_its_tenure(app_config, make_service):
    from trading_assistant.daemon.main import _build_autopilot_runner

    guard = Guard()
    runner = _build_autopilot_runner(
        _config(app_config, "observe"), SimpleNamespace(),
        container=_container(make_service()), runtime_tenure_guard=guard,
        historical_alpaca_client_factory=None, historical_cache_dir=".cache/bars",
    )
    assert isinstance(runner, AutopilotRunner)
    assert runner.autopilot.dry_run is True
    assert runner.tenure_guard is guard and runner.autopilot.tenure_guard is guard


def test_daemon_refuses_an_untradeable_autopilot_configuration(app_config, make_service):
    from trading_assistant.autopilot import AutopilotDisabled
    from trading_assistant.daemon.main import _build_autopilot_runner

    config = _config(app_config, "observe")
    config = config.model_copy(update={"autopilot": config.autopilot.model_copy(update={"universe": ["BTC/USD"]})})
    with pytest.raises(AutopilotDisabled):
        _build_autopilot_runner(
            config, SimpleNamespace(), container=_container(make_service()),
            runtime_tenure_guard=None, historical_alpaca_client_factory=None,
            historical_cache_dir=".cache/bars",
        )
