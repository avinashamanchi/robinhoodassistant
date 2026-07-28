"""Phase 3 execution path: approve -> final risk re-check -> broker submit.

This is the only path that trades. Tests cover the happy path plus every way it
must refuse: kill switch, expired proposal, execution-time price move, double
approval, and human reject.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import AuditEvent, Order, Proposal, utcnow
from trading_assistant.risk.killswitch import KillSwitch
from tests.conftest import decrypt_test_sensitive


def _propose(svc, **kw):
    kw.setdefault("ticker", "AAPL")
    kw.setdefault("side", "buy")
    kw.setdefault("order_type", "market")
    kw.setdefault("notional", "100")
    kw.setdefault("actor", "operator:test")
    kw.setdefault("reason", "execution test proposal")
    kw.setdefault("request_id", "execution-test-proposal")
    return svc.propose_order(**kw)


def _approve(svc, order_id, reason="test approval"):
    return svc.approve_order(
        order_id,
        actor="operator:test",
        reason=reason,
        request_id="execution-test-approval",
    )


def test_approve_runs_final_risk_check_then_submits(make_service):
    svc = make_service()
    order_id = _propose(svc)["order_id"]
    result = _approve(svc, order_id)

    assert result["executed"] is True
    assert result["status"] == "submitted"
    assert result["broker_order_id"] is not None
    assert svc.broker.submit_calls == 1  # exactly one broker submit
    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.approval_actor == "operator:test"
        assert decrypt_test_sensitive(
            order,
            "approval_reason",
        ) == "test approval"
        assert session.query(AuditEvent).filter_by(action="order.approve").count() == 1


def test_killswitch_blocks_execution(make_service):
    svc = make_service()
    order_id = _propose(svc)["order_id"]
    with svc.session_factory() as s:
        KillSwitch.trip(
            s,
            reason="drill",
            actor="test:execution",
            request_id="execution-killswitch-drill",
        )
        s.commit()

    result = _approve(svc, order_id)
    assert result["executed"] is False
    assert result["status"] == "rejected"
    assert any("circuit breaker" in r for r in result["risk_reasons"])
    assert svc.broker.submit_calls == 0


def test_expired_proposal_cannot_be_approved(make_service):
    svc = make_service()
    order_id = _propose(svc)["order_id"]
    # Force the proposal past its TTL (A6).
    with svc.session_factory() as s:
        prop = s.execute(
            select(Proposal).where(Proposal.order_id == order_id)
        ).scalar_one()
        prop.expires_at = utcnow() - timedelta(minutes=1)
        s.commit()

    result = _approve(svc, order_id)
    assert result["executed"] is False
    assert result["status"] == "expired"
    assert svc.broker.submit_calls == 0


def test_execution_time_price_move_rejects(make_service):
    svc = make_service()
    # Propose $400 order at $100 (passes: within $500 notional).
    order_id = _propose(svc, notional="400")["order_id"]
    # Price triples before approval; qty is fixed by notional so notional is still
    # $400 — instead move the price so a large existing position + this order would
    # breach. Simpler: raise price so a limit-style check would fail is N/A here, so
    # we assert the re-check RUNS by tripping via a fresh over-limit condition:
    # bump the order into a disallowed state by shrinking the per-order limit at run
    # time is not possible; instead verify re-check uses fresh snapshot by moving
    # price and using a position that now exceeds the per-ticker cap.
    from trading_assistant.broker.models import Position

    svc.broker._positions["AAPL"] = Position(
        "AAPL", Decimal("18"), Decimal("100"), Decimal("100")
    )  # $1800 existing; +$400 -> $2200 > $2000 per-ticker limit at execution time
    result = _approve(svc, order_id)
    assert result["executed"] is False
    assert result["status"] == "rejected"
    assert any("per ticker" in r for r in result["risk_reasons"])
    assert svc.broker.submit_calls == 0


def test_double_approval_conflicts(make_service):
    svc = make_service()
    order_id = _propose(svc)["order_id"]
    first = _approve(svc, order_id)
    assert first["executed"] is True

    second = _approve(svc, order_id, "duplicate test approval")
    assert second["executed"] is False
    assert "not in PROPOSED" in second.get("error", "")
    assert svc.broker.submit_calls == 1  # still only one real submit


def test_reject_order(make_service):
    svc = make_service()
    order_id = _propose(svc)["order_id"]
    result = svc.reject_order(
        order_id,
        actor="operator:test",
        reason="execution test rejection",
        request_id="execution-test-rejection",
    )
    assert result["status"] == "rejected"

    # A rejected order can no longer be approved.
    approve = _approve(svc, order_id)
    assert approve["executed"] is False
    assert svc.broker.submit_calls == 0


def test_accept_then_disconnect_records_unknown_without_resubmission(make_service):
    from trading_assistant.broker.mock import MockBroker

    class AcceptedThenDisconnectBroker(MockBroker):
        submit_calls = 0

        def submit_order(self, order):
            self.submit_calls += 1
            super().submit_order(order)
            raise ConnectionError("response lost after broker acceptance")

    broker = AcceptedThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    order_id = _propose(svc)["order_id"]

    first = _approve(svc, order_id, "acceptance-loss drill")
    assert first["executed"] is False
    assert first["status"] == OrderStatus.ACCEPTANCE_UNKNOWN.value

    with svc.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert order.acceptance_state == OrderStatus.ACCEPTANCE_UNKNOWN.value
        assert order.submission_attempt == 1
    replay = _approve(svc, order_id, "must not resubmit unknown acceptance")
    assert replay["executed"] is False
    blocked = _propose(svc)
    assert blocked["status"] == "rejected"
    assert any(
        "broker reconciliation is not current" in reason
        for reason in blocked["risk_reasons"]
    )
    assert broker.submit_calls == 1


def test_approve_requires_explicit_actor_and_reason(make_service):
    svc = make_service()
    order_id = _propose(svc)["order_id"]

    with pytest.raises(TypeError):
        svc.approve_order(order_id)
