"""Hermetic acceptance test of the paper order lifecycle, one path per test.

proposal -> human approval -> risk re-check -> submission (client_order_id =
idempotency key) -> broker outcome -> reconciliation -> recorded result, all
against a scripted Alpaca-shaped double. Nothing here reaches the network.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from trading_assistant.broker.base import BrokerSubmissionRejected
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import BrokerFill, OrderStatus, Position
from trading_assistant.db.models import AuditEvent, Fill, Order, Proposal
from trading_assistant.ops.paper_report import build_report

ACTOR = "operator:demo"


class PaperBrokerDouble(MockBroker):
    """MockBroker plus scripted rejection, lost responses and authoritative fills."""

    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0
        self.activities: list[BrokerFill] = []
        self.reject_next: str | None = None
        self.lose_next_response = False

    def submit_order(self, order):
        self.submit_calls += 1
        if self.reject_next is not None:
            reason, self.reject_next = self.reject_next, None
            raise BrokerSubmissionRejected(reason)
        accepted = super().submit_order(order)
        if self.lose_next_response:
            self.lose_next_response = False
            raise ConnectionError("response lost after acceptance")
        return accepted

    def get_fill_activities(self, after=None):
        return list(self.activities)

    def fill(self, broker_order_id: str, qty: Decimal, price: Decimal) -> None:
        current = self._orders_by_id[broker_order_id]
        filled = replace(
            current,
            status=OrderStatus.FILLED,
            filled_qty=qty,
            avg_fill_price=price,
        )
        self._orders_by_id[broker_order_id] = filled
        self._orders_by_key[current.idempotency_key] = filled
        self.activities.append(
            BrokerFill(
                broker_fill_id=f"fill-{broker_order_id}",
                broker_order_id=broker_order_id,
                ticker=current.ticker,
                side=current.side.value if hasattr(current.side, "value") else str(current.side),
                qty=qty,
                price=price,
                filled_at=datetime.now(timezone.utc),
            )
        )
        self._positions[current.ticker] = Position(
            ticker=current.ticker,
            qty=qty,
            avg_entry_price=price,
            current_price=price,
            unrealized_intraday_pnl=Decimal("0"),
        )


def _service(make_service):
    broker = PaperBrokerDouble()
    broker.set_price("AAPL", Decimal("100"))
    return make_service(broker=broker), broker


def _propose(service, key: str, **overrides):
    request = {
        "ticker": "AAPL",
        "side": "buy",
        "order_type": "limit",
        "qty": "1",
        "limit_price": "100.50",
        "idempotency_key": key,
        "actor": ACTOR,
        "reason": f"demo {key}: 1 share AAPL, limit 100.50",
        "request_id": f"propose-{key}",
    }
    request.update(overrides)
    proposal = service.propose_order(**request)
    assert proposal["status"] == "proposed", proposal
    return proposal["order_id"]


def _approve(service, order_id: int, request_id: str):
    return service.approve_order(
        order_id, actor=ACTOR, reason="limits and rationale reviewed", request_id=request_id
    )


def _reconcile(service, request_id: str):
    return service.reconciliation.reconcile(
        actor=ACTOR, reason="demo reconciliation", request_id=request_id
    )


def _order(service, order_id: int) -> Order:
    with service.session_factory() as session:
        return session.get(Order, order_id)


def test_proposal_is_never_sent_until_a_human_approves(make_service):
    service, broker = _service(make_service)

    order_id = _propose(service, "demo-wait")

    assert broker.submit_calls == 0
    assert [p["order_id"] for p in service.get_pending()] == [order_id]
    assert service.get_pending()[0]["rationale"].startswith("demo demo-wait")


def test_approved_order_fills_reconciles_and_is_recorded_end_to_end(make_service, tmp_path):
    service, broker = _service(make_service)
    order_id = _propose(service, "demo-fill")

    approved = _approve(service, order_id, "approve-fill")
    submitted = _order(service, order_id)

    assert approved["executed"] is True and broker.submit_calls == 1
    assert submitted.status == OrderStatus.SUBMITTED.value
    assert broker.get_order_by_client_id("demo-fill") is not None  # client_order_id == key

    broker.fill(submitted.broker_order_id, Decimal("1"), Decimal("100.25"))
    report = _reconcile(service, "reconcile-fill")

    assert report.inserted_fills == 1 and report.broker_drift == ()
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        fill = session.scalars(select(Fill).where(Fill.order_id == order_id)).one()
        proposal = session.scalars(select(Proposal).where(Proposal.order_id == order_id)).one()
        actions = {
            event.action
            for event in session.scalars(
                select(AuditEvent).where(AuditEvent.target_id == str(order_id))
            )
        }
        assert order.status == OrderStatus.FILLED.value
        assert (fill.qty, fill.price) == (Decimal("1"), Decimal("100.25"))
        assert proposal.order_id == order_id and order.approval_actor == ACTOR
        assert {"order.submission_claim", "order.submission_result"} <= actions
        evaluation = build_report(session)
    assert evaluation["funnel"]["outcomes"] == {"filled": 1}
    assert evaluation["open_positions"][0]["qty"] == "1"


def test_definitive_broker_rejection_is_final_and_nothing_fills(make_service):
    service, broker = _service(make_service)
    order_id = _propose(service, "demo-reject")
    broker.reject_next = "insufficient_buying_power"

    _approve(service, order_id, "approve-reject")

    order = _order(service, order_id)
    assert order.status == OrderStatus.REJECTED.value
    assert order.last_error_code == "insufficient_buying_power"
    assert _reconcile(service, "reconcile-reject").inserted_fills == 0


def test_a_submitted_order_can_be_canceled_and_stays_canceled(make_service):
    service, broker = _service(make_service)
    order_id = _propose(service, "demo-cancel")
    _approve(service, order_id, "approve-cancel")

    canceled = service.cancel_live_order(
        order_id, actor=ACTOR, reason="demo cancel", request_id="cancel-1"
    )

    assert _order(service, order_id).status == OrderStatus.CANCELED.value, canceled
    assert _reconcile(service, "reconcile-cancel").inserted_fills == 0


def test_a_lost_broker_response_is_never_resubmitted_and_reconciles_by_key(make_service):
    service, broker = _service(make_service)
    order_id = _propose(service, "demo-lost")
    broker.lose_next_response = True

    first = _approve(service, order_id, "approve-lost")
    again = _approve(service, order_id, "approve-lost-again")

    assert first["status"] == OrderStatus.ACCEPTANCE_UNKNOWN.value
    assert again.get("executed") is not True
    assert broker.submit_calls == 1 and len(broker._orders_by_key) == 1
    report = _reconcile(service, "reconcile-lost")
    assert order_id not in report.unresolved_unknown
    assert _order(service, order_id).status == OrderStatus.SUBMITTED.value


def test_an_unapproved_proposal_expires_and_cannot_be_approved_late(make_service):
    service, broker = _service(make_service)
    order_id = _propose(service, "demo-expire")
    with service.session_factory() as session:
        proposal = session.scalars(select(Proposal).where(Proposal.order_id == order_id)).one()
        proposal.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.commit()

    late = _approve(service, order_id, "approve-late")

    assert late.get("executed") is not True
    assert broker.submit_calls == 0
    assert _order(service, order_id).status == OrderStatus.EXPIRED.value


def test_a_broker_fill_the_ledger_cannot_explain_is_reported_as_drift(make_service):
    service, broker = _service(make_service)
    broker.activities.append(
        BrokerFill(
            broker_fill_id="unexplained-fill",
            broker_order_id="not-ours",
            ticker="AAPL",
            side="buy",
            qty=Decimal("3"),
            price=Decimal("99"),
            filled_at=datetime.now(timezone.utc),
        )
    )

    report = _reconcile(service, "reconcile-mismatch")

    assert report.broker_drift, "an unexplained fill must be surfaced, not absorbed"
    with service.session_factory() as session:
        assert session.scalars(select(Fill)).all() == []
