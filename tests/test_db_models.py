"""Persistence + relationships for core tables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import Fill, Order, Proposal, utcnow


def test_order_proposal_fill_roundtrip(session_factory):
    with session_factory() as s:
        order = Order(
            idempotency_key="idem-1",
            ticker="AAPL",
            side="buy",
            order_type="market",
            notional=Decimal("100"),
            status=OrderStatus.PROPOSED.value,
        )
        s.add(order)
        s.flush()
        s.add(
            Proposal(
                order_id=order.id,
                reasoning="LLM says buy",
                ttl_minutes=15,
                expires_at=utcnow() + timedelta(minutes=15),
            )
        )
        s.add(
            Fill(
                order_id=order.id,
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
            )
        )
        s.commit()
        oid = order.id

    with session_factory() as s:
        order = s.get(Order, oid)
        assert order.idempotency_key == "idem-1"
        assert order.proposal.reasoning == "LLM says buy"
        assert len(order.fills) == 1
        # Timestamps are timezone-aware (UTC).
        assert order.created_at.tzinfo is not None


def test_idempotency_key_unique(session_factory):
    with session_factory() as s:
        s.add(Order(idempotency_key="dup", ticker="AAPL", side="buy", order_type="market"))
        s.commit()
    with session_factory() as s:
        s.add(Order(idempotency_key="dup", ticker="MSFT", side="buy", order_type="market"))
        try:
            s.commit()
            raised = False
        except Exception:
            raised = True
        assert raised, "duplicate idempotency_key must violate the unique constraint"
