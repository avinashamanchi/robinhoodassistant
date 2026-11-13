"""A fresh database baselined against a flat paper account whose history it never had."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest
from sqlalchemy import select

from tests.conftest import decrypt_test_sensitive
from tests.test_paper_history_adoption import (
    BUY_TIME,
    _AdoptionGuard,
    _HistoryBroker,
    _trip_broker_drift,
)
from trading_assistant.broker.models import BrokerFill, Position
from trading_assistant.db.models import AuditEvent, CircuitBreakerState, Fill, ReconciliationCursor
from trading_assistant.ops.paper_baseline import PaperBaselineError, establish_flat_baseline
from trading_assistant.orders.reconciliation import ReconciliationService
from trading_assistant.orders.repository import OrderRepository
from trading_assistant.risk.breakers import BreakerScope

NOW = BUY_TIME + timedelta(days=90)


class _Broker(_HistoryBroker):
    """Honours ``after`` like Alpaca (inclusive boundary, to be safe)."""

    def get_fill_activities(self, after=None):
        self.activity_reads += 1
        return [a for a in self.activities if after is None or a.filled_at >= after]


def _baseline(session_factory, broker, **overrides):
    options = dict(actor="operator:test", reason="baseline a flat account",
                   request_id="baseline-1", maintenance_guard=_AdoptionGuard(), now=lambda: NOW)
    options.update(overrides)
    return establish_flat_baseline(session_factory, broker, **options)


def _reconcile(session_factory, broker, request_id):
    return ReconciliationService(session_factory, broker, OrderRepository(session_factory)).reconcile(
        actor="runtime:test", reason="after the baseline", request_id=request_id
    )


def test_without_a_baseline_unknown_history_is_drift(session_factory):
    report = _reconcile(session_factory, _Broker(), "no-baseline")
    assert report.broker_drift


def test_the_baseline_sets_history_aside_and_keeps_the_latch(session_factory):
    broker = _Broker()
    _trip_broker_drift(session_factory)

    receipt = _baseline(session_factory, broker)

    assert receipt.fills_set_aside == 2 and receipt.baseline_at.startswith(NOW.date().isoformat())
    with session_factory() as session:
        cursor = session.get(ReconciliationCursor, ("alpaca", "fills"))
        assert cursor.last_activity_at == NOW and cursor.version == 1
        assert session.scalar(select(Fill)) is None                      # nothing imported
        latch = session.get(CircuitBreakerState, BreakerScope.broker_drift().key)
        assert latch.tripped is True                                      # never cleared here
        audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "paper_baseline.establish"))
        detail = json.loads(decrypt_test_sensitive(audit, "detail_json"))
    assert detail["fills_set_aside"] == 2 and detail["activity_digest"] == receipt.activity_digest

    report = _reconcile(session_factory, broker, "after-baseline")
    assert report.broker_drift == ()


def test_fills_after_the_baseline_are_still_reconciled(session_factory):
    broker = _Broker()
    _trip_broker_drift(session_factory)
    _baseline(session_factory, broker)
    broker.activities.append(BrokerFill(
        broker_fill_id="fill-new", broker_order_id="broker-unknown", ticker="AAPL", side="buy",
        qty=Decimal("1"), price=Decimal("100"), filled_at=NOW + timedelta(hours=1),
    ))

    report = _reconcile(session_factory, broker, "new-unknown-fill")
    assert report.broker_drift                                            # an unexplained new fill is drift


def test_an_account_with_a_position_is_refused(session_factory):
    broker = _Broker(positions=[Position("AAPL", Decimal("1"), Decimal("100"), Decimal("100"))])
    _trip_broker_drift(session_factory)

    with pytest.raises(PaperBaselineError) as refused:
        _baseline(session_factory, broker)
    assert refused.value.stable_code == "paper_baseline_account_not_flat"


def test_the_latch_must_already_be_tripped(session_factory):
    with pytest.raises(PaperBaselineError) as refused:
        _baseline(session_factory, _Broker())
    assert refused.value.stable_code == "paper_baseline_broker_drift_breaker_required"


def test_a_second_baseline_is_refused(session_factory):
    broker = _Broker()
    _trip_broker_drift(session_factory)
    _baseline(session_factory, broker)

    with pytest.raises(PaperBaselineError) as refused:
        _baseline(session_factory, broker, request_id="baseline-2")
    assert refused.value.stable_code == "paper_baseline_target_has_history"


def test_history_changing_between_reads_is_refused(session_factory):
    class Changing(_Broker):
        def get_fill_activities(self, after=None):
            rows = super().get_fill_activities(after)
            return rows[:1] if self.activity_reads > 1 else rows

    _trip_broker_drift(session_factory)
    with pytest.raises(PaperBaselineError) as refused:
        _baseline(session_factory, Changing())
    assert refused.value.stable_code == "paper_baseline_broker_changed"


def test_a_clock_behind_the_broker_is_refused(session_factory):
    _trip_broker_drift(session_factory)
    with pytest.raises(PaperBaselineError) as refused:
        _baseline(session_factory, _Broker(), now=lambda: BUY_TIME - timedelta(seconds=1))
    assert refused.value.stable_code == "paper_baseline_clock_behind_broker"
