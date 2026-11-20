"""Failure handling not covered elsewhere: Alpaca auth and rate-limit responses,
and a restart that finds an open order and an open position."""

from __future__ import annotations

from decimal import Decimal

import pytest
from alpaca.common.exceptions import APIError

from tests.test_alpaca_broker import FakeData, FakeTrading, _api_error, _order
from tests.test_paper_lifecycle_e2e import PaperBrokerDouble
from trading_assistant.broker.alpaca import AlpacaBroker
from trading_assistant.broker.base import BrokerAcceptanceUnknown
from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import Order

ACTOR = "operator:demo"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_auth_rate_limit_and_server_errors_on_submit_are_never_read_as_rejection(status):
    class Failing(FakeTrading):
        def submit_order(self, order_data):
            self.submit_calls += 1
            raise _api_error(status)

    trading = Failing()
    broker = AlpacaBroker(trading, FakeData({}))

    with pytest.raises(BrokerAcceptanceUnknown):
        broker.submit_order(_order())
    assert trading.submit_calls == 1  # surfaced, not retried


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_or_rate_limited_lookup_never_means_not_submitted(status):
    trading = FakeTrading(lookup_error=_api_error(status))
    broker = AlpacaBroker(trading, FakeData({}))

    with pytest.raises(APIError):
        broker.submit_order(_order())
    assert trading.submit_calls == 0  # an unknown lookup must not lead to a submit


def test_a_restarted_service_recognises_its_open_order_and_position_without_resubmitting(make_service):
    broker = PaperBrokerDouble()
    broker.set_price("AAPL", Decimal("100"))
    first = make_service(broker=broker)
    filled = first.propose_order("AAPL", "buy", "market", qty="1", idempotency_key="restart-filled",
                                 actor=ACTOR, reason="filled before restart", request_id="p-filled")
    working = first.propose_order("AAPL", "buy", "limit", qty="1", limit_price="99.50",
                                  idempotency_key="restart-working", actor=ACTOR,
                                  reason="still open at restart", request_id="p-working")
    for proposal, request in ((filled, "a-filled"), (working, "a-working")):
        first.approve_order(proposal["order_id"], actor=ACTOR, reason="ok", request_id=request)
    with first.session_factory() as session:
        broker.fill(session.get(Order, filled["order_id"]).broker_order_id, Decimal("1"), Decimal("100.10"))
    submitted_before_restart = broker.submit_calls

    restarted = make_service(broker=broker)  # same database, same broker, new process state
    report = restarted.reconciliation.reconcile(actor=ACTOR, reason="startup", request_id="startup")

    assert report.inserted_fills == 1 and report.broker_drift == ()
    with restarted.session_factory() as session:
        assert session.get(Order, filled["order_id"]).status == OrderStatus.FILLED.value
        open_order = session.get(Order, working["order_id"])
        assert open_order.status == OrderStatus.SUBMITTED.value
        broker.fill(open_order.broker_order_id, Decimal("1"), Decimal("99.50"))
    later = restarted.reconciliation.reconcile(actor=ACTOR, reason="after restart", request_id="later")

    assert later.inserted_fills == 1
    assert broker.submit_calls == submitted_before_restart  # nothing was resubmitted
    assert [(p["ticker"], Decimal(p["qty"])) for p in restarted.get_positions()] == [("AAPL", Decimal("2"))]
