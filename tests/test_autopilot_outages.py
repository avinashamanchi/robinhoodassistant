"""Simulated outages and process-lifecycle behaviour of the autopilot.

Everything is simulated in-process: fake brokers, a disposable database, a
temporary working directory for logs. Nothing here verifies the operator's
installed launchd jobs, real network behaviour, or any broker account.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from trading_assistant.assets import AssetClass
from trading_assistant.autopilot import Autopilot
from trading_assistant.autopilot.evidence import EvidenceStore
from trading_assistant.autopilot.readiness import ReadinessReport, evaluate_readiness
from trading_assistant.autopilot.runner import AutopilotRunner
from trading_assistant.broker.base import BrokerSubmissionRejected
from trading_assistant.broker.mock import MockBroker
from trading_assistant.db.models import Order
from trading_assistant.dependencies import RequiredDependencyUnavailable
from trading_assistant.risk.clock import FakeClock
from trading_assistant.signals.models import MarketFeatures

NY = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 6, 9, 30, tzinfo=NY)


def _features():
    return MarketFeatures(
        symbol="AAPL", asset_class=AssetClass.EQUITY,
        as_of=datetime.now(timezone.utc) - timedelta(hours=1),
        sma_20=105, sma_50=100, sma_200=90, last_close=100,
    )


def _autopilot(service, provider=None, **overrides):
    options = dict(universe=["AAPL"], notional_per_trade=Decimal("100"), max_orders_per_day=8)
    options.update(overrides)
    return Autopilot(service, provider or (lambda _s: _features()), **options)


def _reasons(ap):
    return [d.reason for d in ap.last_decisions]


def _orders(service):
    with service.session_factory() as s:
        return s.execute(select(Order)).scalars().all()


# ── broker outages during a cycle ─────────────────────────────────────────────
def test_unreadable_positions_end_the_cycle_without_orders(make_service):
    class NoPositions(MockBroker):
        def get_positions(self):
            raise TimeoutError("positions endpoint timed out")

    broker = NoPositions()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    service.sync_open_orders = lambda **_k: {"failed": 0}
    ap = _autopilot(service)
    assert ap.run_once() == []
    assert _reasons(ap) == ["positions_unavailable"]
    assert _orders(service) == []


def test_unavailable_order_sync_skips_the_whole_cycle(make_service):
    service = make_service()

    def unavailable(**_kwargs):
        raise RequiredDependencyUnavailable

    service.sync_open_orders = unavailable
    ap = _autopilot(service)
    assert ap.run_once() == []
    assert _reasons(ap) == ["order_sync_unavailable"]
    assert service.broker.submit_calls == 0


def test_rate_limited_market_data_is_a_skipped_symbol_not_an_error(make_service):
    def denied(_symbol):
        raise RequiredDependencyUnavailable  # limiter denial surfaces this way

    service = make_service()
    ap = _autopilot(service, provider=denied)
    assert ap.run_once() == []
    assert _reasons(ap) == ["features_unavailable"]


class RejectingBroker(MockBroker):
    """The broker definitively refuses (e.g. an authentication failure)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.submit_calls = 0

    def submit_order(self, order):
        self.submit_calls += 1
        raise BrokerSubmissionRejected("broker_unauthorized")


def test_definitive_broker_rejection_is_final_for_the_session(make_service):
    broker = RejectingBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    first = _autopilot(service)
    first.run_once(session=OPEN.date())
    (order,) = _orders(service)
    assert order.status == "rejected"
    assert _reasons(first) == ["risk_rejected"]

    again = _autopilot(service)
    again.run_once(session=OPEN.date())
    assert _reasons(again) == ["intent_already_rejected"]
    assert broker.submit_calls == 1


class FlakyBroker(MockBroker):
    """Times out before accepting anything (connection refused)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.submit_calls = 0

    def submit_order(self, order):
        self.submit_calls += 1
        raise ConnectionError("connection refused")


def test_ambiguous_submission_failure_is_uncertain_and_never_blindly_retried(make_service):
    broker = FlakyBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)
    first = _autopilot(service)
    first.run_once(session=OPEN.date())
    assert _reasons(first) == ["submission_uncertain"]

    again = _autopilot(service)
    again.run_once(session=OPEN.date())
    assert _reasons(again) == ["order_in_flight"]
    assert broker.submit_calls == 1


# ── runner: storage contention and restart ────────────────────────────────────
def _runner(app_config, service, tmp_path, *, store=None):
    service._clocks[AssetClass.EQUITY] = FakeClock(is_open=True, most_recent_open=OPEN.astimezone(timezone.utc))
    config = app_config.model_copy(update={"autopilot": app_config.autopilot.model_copy(
        update={"mode": "paper", "universe": ["AAPL"], "notional_per_trade": Decimal("100")})})
    service.config = config
    runner = AutopilotRunner(
        config=config, service=service,
        autopilot=_autopilot(service, dry_run=True),
        store=store or EvidenceStore(service.session_factory, actor="autopilot:sma_trend"),
        root=tmp_path, now=lambda: (OPEN + timedelta(minutes=45)).astimezone(timezone.utc),
        installation_check=lambda _r: None,
    )

    def ready(now):
        report = evaluate_readiness(config=config, store=runner.store, root=tmp_path, now=now,
                                    breakers=service.breakers, installation_check=lambda _r: None)
        return ReadinessReport(**{**report.__dict__, "ready": True})

    runner._readiness = ready
    return runner


def test_evidence_write_failure_reruns_without_duplicate_orders(app_config, make_service, tmp_path):
    from sqlalchemy.exc import OperationalError

    service = make_service()
    store = EvidenceStore(service.session_factory, actor="autopilot:sma_trend")
    real_record = store.record_cycle
    attempts = []

    def locked_once(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return real_record(**kwargs)

    store.record_cycle = locked_once
    runner = _runner(app_config, service, tmp_path, store=store)

    with pytest.raises(OperationalError):
        runner.run_if_due()          # orders placed, evidence not recorded
    assert service.broker.submit_calls == 1

    result = runner.run_if_due()     # the monitor's next tick
    assert result["result"] == "executed"
    assert service.broker.submit_calls == 1
    assert len(_orders(service)) == 1
    assert [r.result_code for r in store.cycles()] == ["executed"]


def test_restarted_daemon_resumes_the_session_without_duplicates(app_config, make_service, tmp_path):
    """A process killed mid-cycle leaves orders but no evidence; a new
    runner (new process, same database) completes the session safely."""
    service = make_service()
    crashed = _runner(app_config, service, tmp_path)
    crashed.autopilot.dry_run = False                 # as the runner sets it
    crashed.autopilot.run_once(session=OPEN.date())  # the work before the kill
    assert service.broker.submit_calls == 1

    restarted = _runner(app_config, service, tmp_path)
    assert restarted.run_if_due()["result"] == "executed"
    assert service.broker.submit_calls == 1
    (record,) = restarted.store.cycles()
    assert record.detail["decisions"][0]["reason"] == "order_in_flight"


# ── diagnostics: logging, redaction, rotation ─────────────────────────────────
def test_autopilot_diagnostics_reach_the_private_redacted_role_log(tmp_path, monkeypatch, make_service):
    from trading_assistant.logging import configure_runtime_logging

    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    saved = list(root.handlers)
    # A registered (non-credential-shaped) value: redaction masks any
    # registered secret wherever it appears.
    secret = "autopilot-diagnostic-registered-value-7f3c"
    try:
        path = configure_runtime_logging("daemon", SimpleNamespace(anthropic_api_key=secret))
        service = make_service()
        ap = _autopilot(service, dry_run=True)
        ap.run_once()
        logging.getLogger("trading_assistant.autopilot").warning("probe token=%s", secret)
        for handler in root.handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
    finally:
        for handler in list(root.handlers):
            if handler not in saved:
                root.removeHandler(handler)
                handler.close()

    assert path.name == "daemon.runtime.log"
    assert "autopilot decision symbol=AAPL signal=long action=buy reason=enter_long" in text
    assert secret not in text and "REDACTED" in text
    assert os.stat(tmp_path / "logs" / "daemon.runtime.log").st_mode & 0o777 == 0o600


def test_role_log_rotation_keeps_every_file_private(tmp_path):
    from trading_assistant.logging import _PrivateRotatingFileHandler

    handler = _PrivateRotatingFileHandler(tmp_path / "daemon.runtime.log", maxBytes=200, backupCount=2)
    logger = logging.getLogger("rotation-probe")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        for index in range(50):
            logger.warning("autopilot decision line %03d %s", index, "x" * 40)
    finally:
        logger.removeHandler(handler)
        handler.close()

    files = sorted(tmp_path.glob("daemon.runtime.log*"))
    assert len(files) == 3                      # active + 2 backups, bounded
    assert all(f.stat().st_mode & 0o777 == 0o600 for f in files)
