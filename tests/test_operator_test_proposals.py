"""Operator test proposals: refusals, labelling, and the three demonstrations.

A: submit and cancel a non-marketable limit order.
B: an approved entry that fills, reconciles and shows a position.
C: an approved, position-reducing exit that reconciles back to flat.
All against Alpaca-shaped doubles; nothing reaches the network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.test_paper_lifecycle_e2e import PaperBrokerDouble
from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import Order
from trading_assistant.ops.paper_report import build_report
from trading_assistant.orders.operator_test import (
    OperatorTestRefused,
    OperatorTestRequest,
    propose_operator_test,
)

ACTOR = "operator:demo"


def _service(make_service, *, enabled=True, market_open=True, cap="50"):
    broker = PaperBrokerDouble()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker, market_open=market_open)
    service.config = service.config.model_copy(
        update={
            "operator_test": service.config.operator_test.model_copy(
                update={"enabled": enabled, "symbols": ["AAPL"], "max_notional_per_order": float(cap)}
            )
        }
    )
    return service, broker


def _propose(service, key, **request):
    return propose_operator_test(
        service,
        OperatorTestRequest(**request),
        actor=ACTOR,
        reason="lifecycle demonstration",
        request_id=f"request-{key}",
        idempotency_key=f"optest-{key}",
    )


def _refusal(service, **request) -> str:
    with pytest.raises(OperatorTestRefused) as refused:
        _propose(service, "refused", **request)
    return refused.value.code


NON_MARKETABLE = dict(scenario="non_marketable", symbol="AAPL", order_type="limit",
                      qty=Decimal("0.2"), limit_price=Decimal("99.50"))
ENTRY = dict(scenario="entry", symbol="AAPL", order_type="market", notional=Decimal("40"))


def test_test_proposals_are_off_unless_enabled(make_service):
    service, broker = _service(make_service, enabled=False)

    assert _refusal(service, **ENTRY) == "operator_test_disabled"
    with service.session_factory() as session:
        assert session.query(Order).count() == 0


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"symbol": "MSFT"}, "symbol_not_approved_for_tests"),
        ({"notional": Decimal("60")}, "test_notional_exceeds_cap"),
        ({"order_type": "limit", "qty": Decimal("0.3"), "notional": None,
          "limit_price": Decimal("99.00")}, "entry_limit_below_ask"),
        ({"qty": Decimal("1")}, "market_order_invalid"),
        ({"scenario": "nope"}, "scenario_invalid"),
    ],
    ids=["unapproved-symbol", "over-cap", "entry-limit-below-ask", "qty-and-notional", "bad-scenario"],
)
def test_invalid_entries_are_refused_before_any_order_exists(make_service, overrides, code):
    service, _broker = _service(make_service)

    assert _refusal(service, **{**ENTRY, **overrides}) == code
    with service.session_factory() as session:
        assert session.query(Order).count() == 0


def test_a_non_marketable_limit_must_be_below_the_bid(make_service):
    service, _broker = _service(make_service)

    assert _refusal(service, **{**NON_MARKETABLE, "limit_price": Decimal("99.95")}) == (
        "limit_would_be_marketable"
    )
    assert _refusal(service, **{**NON_MARKETABLE, "limit_price": Decimal("99.505")}) == (
        "limit_price_precision"
    )


def test_market_closed_is_reported_not_queued(make_service):
    service, _broker = _service(make_service, market_open=False)

    assert _refusal(service, **ENTRY) == "market_closed"


def test_a_test_proposal_is_labelled_and_waits_for_a_human(make_service):
    service, broker = _service(make_service)

    proposal = _propose(service, "label", **ENTRY)

    assert proposal["status"] == "proposed" and proposal["operator_test"] is True
    assert broker.submit_calls == 0
    (pending,) = service.get_pending()
    assert pending["rationale"].startswith("OPERATOR TEST (not a strategy signal), scenario entry")
    assert "Operator note: lifecycle demonstration" in pending["rationale"]


def test_demo_a_submit_then_cancel_a_non_marketable_order(make_service):
    service, broker = _service(make_service)
    order_id = _propose(service, "demo-a", **NON_MARKETABLE)["order_id"]

    service.approve_order(order_id, actor=ACTOR, reason="demo A", request_id="approve-a")
    assert broker.submit_calls == 1
    service.cancel_live_order(order_id, actor=ACTOR, reason="demo A cancel", request_id="cancel-a")

    with service.session_factory() as session:
        assert session.get(Order, order_id).status == OrderStatus.CANCELED.value
    assert service.reconciliation.reconcile(
        actor=ACTOR, reason="demo A", request_id="reconcile-a"
    ).inserted_fills == 0


def test_demo_b_and_c_entry_fills_then_an_approved_exit_returns_to_flat(make_service):
    service, broker = _service(make_service)

    entry = _propose(service, "demo-b", scenario="entry", symbol="AAPL", order_type="market",
                     qty=Decimal("0.3"))["order_id"]
    service.approve_order(entry, actor=ACTOR, reason="demo B", request_id="approve-b")
    with service.session_factory() as session:
        broker.fill(session.get(Order, entry).broker_order_id, Decimal("0.3"), Decimal("100.10"))
    assert service.reconciliation.reconcile(
        actor=ACTOR, reason="demo B", request_id="reconcile-b"
    ).inserted_fills == 1
    assert [(p["ticker"], Decimal(p["qty"])) for p in service.get_positions()] == [("AAPL", Decimal("0.3"))]

    assert _refusal(service, scenario="exit", symbol="AAPL", order_type="market",
                    qty=Decimal("0.5")) == "exit_exceeds_position"
    exit_id = _propose(service, "demo-c", scenario="exit", symbol="AAPL", order_type="market",
                       qty=Decimal("0.3"))["order_id"]
    service.approve_order(exit_id, actor=ACTOR, reason="demo C", request_id="approve-c")
    with service.session_factory() as session:
        broker.fill(session.get(Order, exit_id).broker_order_id, Decimal("0.3"), Decimal("100.40"))
    report = service.reconciliation.reconcile(actor=ACTOR, reason="demo C", request_id="reconcile-c")

    assert report.inserted_fills == 1 and report.broker_drift == ()
    assert service.get_positions() == []
    with service.session_factory() as session:
        evaluation = build_report(session)
    assert evaluation["funnel"]["by_source"] == {"operator_test": 2}
    assert evaluation["pnl"]["realized"] == "0.09"


def test_an_exit_without_a_position_is_refused(make_service):
    service, _broker = _service(make_service)

    assert _refusal(service, scenario="exit", symbol="AAPL", order_type="market",
                    qty=Decimal("1")) == "no_position_to_reduce"


def test_the_api_route_creates_a_test_proposal_and_reports_refusals(authenticated_client):
    client, csrf = authenticated_client
    service = client.trading_service
    headers = {"X-CSRF-Token": csrf}
    body = {"scenario": "entry", "symbol": "AAPL", "order_type": "market",
            "notional": "40", "reason": "api demonstration"}

    refused = client.post("/proposals/test", json=body, headers={**headers, "Idempotency-Key": "optest-api-1"})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "operator_test_disabled"

    service.config = service.config.model_copy(
        update={"operator_test": service.config.operator_test.model_copy(
            update={"enabled": True, "symbols": ["AAPL"]})}
    )
    created = client.post("/proposals/test", json=body, headers={**headers, "Idempotency-Key": "optest-api-2"})
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "proposed" and service.broker.submit_calls == 0
    assert client.get("/pending").json()["pending"][0]["rationale"].startswith("OPERATOR TEST")
