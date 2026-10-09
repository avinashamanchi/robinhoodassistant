"""Autopilot order safety: intent keys, replays, uncertain submissions.

Every case uses the mock/spy broker and a disposable database; no paper or
live order is placed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import threading
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from trading_assistant.assets import AssetClass
from trading_assistant.autopilot import Autopilot
from trading_assistant.autopilot.identity import action_key
from trading_assistant.broker.mock import MockBroker
from trading_assistant.db.models import Order
from trading_assistant.signals.models import MarketFeatures

SESSION = date(2026, 10, 6)


def _features(symbol, sma20, sma50, **extra):
    return MarketFeatures(
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        as_of=datetime.now(timezone.utc) - timedelta(hours=1),
        sma_20=sma20,
        sma_50=sma50,
        sma_200=extra.get("sma200", 90),
        last_close=extra.get("last_close", 100),
    )


LONG = {"AAPL": _features("AAPL", 105, 100)}


def _autopilot(service, features=LONG, **overrides):
    options = dict(universe=["AAPL"], notional_per_trade=Decimal("100"), max_orders_per_day=8)
    options.update(overrides)
    return Autopilot(service, lambda symbol: features[symbol.upper()], **options)


def _orders(service, symbol="AAPL"):
    with service.session_factory() as s:
        return s.execute(select(Order).where(Order.ticker == symbol)).scalars().all()


def _reasons(ap):
    return [d.reason for d in ap.last_decisions]


# ── intent keys ───────────────────────────────────────────────────────────────
def test_intent_key_is_stable_and_specific():
    key = action_key("sma_trend", "aapl", "buy", SESSION)
    assert key == action_key("sma_trend", "AAPL", "buy", SESSION)
    assert len(key) <= 64
    others = {
        action_key("sma_crossover", "AAPL", "buy", SESSION),
        action_key("sma_trend", "MSFT", "buy", SESSION),
        action_key("sma_trend", "AAPL", "sell", SESSION),
        action_key("sma_trend", "AAPL", "buy", SESSION + timedelta(days=1)),
    }
    assert key not in others and len(others) == 4


def test_order_carries_the_intent_key_as_its_idempotency_key(make_service):
    service = make_service()
    _autopilot(service).run_once(session=SESSION)
    (order,) = _orders(service)
    assert order.idempotency_key == action_key("sma_trend", "AAPL", "buy", SESSION)


# ── repeated, resumed and duplicate requests ──────────────────────────────────
def test_repeated_cycle_in_a_session_never_creates_a_second_order(make_service):
    service = make_service()
    first = _autopilot(service)
    first.run_once(session=SESSION)
    second = _autopilot(service)
    second.run_once(session=SESSION)

    assert len(_orders(service)) == 1
    assert service.broker.submit_calls == 1
    assert _reasons(second) == ["order_in_flight"]


def test_restart_between_recording_intent_and_approval_resumes_that_order(make_service):
    """A crash after propose_order (intent recorded) but before approval."""
    service = make_service()
    key = action_key("sma_trend", "AAPL", "buy", SESSION)
    proposal = service.propose_order(
        "AAPL", "buy", "market", notional="100", idempotency_key=key,
        actor="autopilot:sma_trend", reason="interrupted cycle", request_id=uuid4().hex,
    )
    assert proposal["status"] == "proposed"

    ap = _autopilot(service)
    results = ap.run_once(session=SESSION)

    assert len(_orders(service)) == 1
    assert service.broker.submit_calls == 1
    assert results[0]["order_id"] == proposal["order_id"]
    assert results[0]["replayed"] is True
    assert _reasons(ap) == ["resumed_intent"]


def test_concurrent_duplicate_execution_requests_place_one_order(make_service):
    service = make_service()
    errors = []

    def run():
        try:
            _autopilot(service).run_once(session=SESSION)
        except Exception as error:  # a losing racer may surface a conflict
            errors.append(type(error).__name__)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(_orders(service)) == 1
    assert service.broker.submit_calls == 1


# ── uncertain, rejected, cancelled and partial outcomes ───────────────────────
class LostResponseBroker(MockBroker):
    """Accepts the order, then the response is lost on the way back."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.submit_calls = 0

    def submit_order(self, order):
        self.submit_calls += 1
        super().submit_order(order)
        raise TimeoutError("response lost after the broker accepted the order")


def test_lost_broker_response_is_reconciled_never_resubmitted(make_service):
    broker = LostResponseBroker()
    broker.set_price("AAPL", Decimal("100"))
    service = make_service(broker=broker)

    first = _autopilot(service)
    first.run_once(session=SESSION)
    assert _reasons(first) == ["submission_uncertain"]
    (order,) = _orders(service)
    assert order.status == "acceptance_unknown"

    # The same session again: the uncertain order blocks the symbol.
    second = _autopilot(service)
    second.run_once(session=SESSION)
    assert _reasons(second) == ["order_in_flight"]
    assert broker.submit_calls == 1

    # Reconciliation finds the order by client_order_id; still one order.
    service.sync_open_orders(actor="test", reason="reconcile", request_id=uuid4().hex)
    (order,) = _orders(service)
    assert order.status == "submitted"
    assert order.broker_order_id is not None
    assert broker.submit_calls == 1


def test_rejected_intent_is_not_retried_in_the_same_session(make_service):
    service = make_service()
    oversized = _autopilot(service, notional_per_trade=Decimal("100000"))
    oversized.run_once(session=SESSION)
    assert _reasons(oversized) == ["risk_rejected"]

    again = _autopilot(service, notional_per_trade=Decimal("100000"))
    again.run_once(session=SESSION)
    assert _reasons(again) == ["intent_already_rejected"]
    assert len(_orders(service)) == 1
    assert service.broker.submit_calls == 0


def test_cancelled_order_is_not_resubmitted_until_the_next_session(make_service):
    service = make_service()
    _autopilot(service).run_once(session=SESSION)
    (order,) = _orders(service)
    service.broker.cancel_order(order.broker_order_id)
    service.sync_open_orders(actor="test", reason="reconcile", request_id=uuid4().hex)
    (order,) = _orders(service)
    assert order.status == "canceled"

    same_session = _autopilot(service)
    same_session.run_once(session=SESSION)
    assert _reasons(same_session) == ["intent_already_canceled"]

    next_session = _autopilot(service)
    next_session.run_once(session=SESSION + timedelta(days=1))
    assert _reasons(next_session) == ["enter_long"]
    assert len(_orders(service)) == 2


def test_partially_filled_order_blocks_and_counts_only_filled_quantity(make_service):
    from trading_assistant.db.models import Fill
    from trading_assistant.security.sensitive_fields import persist_sensitive

    service = make_service()
    with service.session_factory() as s:
        order = Order(
            idempotency_key=uuid4().hex, ticker="AAPL", side="buy",
            order_type="market", qty=Decimal("5"), status="partially_filled",
            approval_actor="autopilot:sma_trend",
            approved_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        persist_sensitive(s, order, {"approval_reason": "partial fixture"})
        s.flush()
        s.add(Fill(order_id=order.id, ticker="AAPL", side="buy", qty=Decimal("2"),
                   price=Decimal("100"), broker_fill_id=f"fill-{uuid4().hex}"))
        s.commit()

    ap = _autopilot(service)
    ap.run_once(session=SESSION)
    (decision,) = ap.last_decisions
    assert decision.reason == "order_in_flight"
    assert decision.owned == Decimal("2")
    assert service.broker.submit_calls == 0


def test_dry_run_reports_the_intent_key_and_touches_nothing(make_service):
    service = make_service()
    ap = _autopilot(service, dry_run=True)
    (intended,) = ap.run_once(session=SESSION)
    assert intended["intent_key"] == action_key("sma_trend", "AAPL", "buy", SESSION)
    with service.session_factory() as s:
        assert s.execute(select(func.count()).select_from(Order)).scalar_one() == 0
