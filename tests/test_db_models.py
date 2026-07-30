"""Persistence + relationships for core tables."""

from __future__ import annotations

import stat
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import (
    AuthSession,
    Base,
    ConcurrencyLease,
    Fill,
    Order,
    PanicReceipt,
    Proposal,
    ProviderBudgetDay,
    ProviderReservation,
    RateWindow,
    utcnow,
)
from trading_assistant.db.session import create_db_engine
from trading_assistant.security.sensitive_fields import (
    persist_sensitive,
    sensitive_store,
)


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
        persist_sensitive(
            s,
            order,
            {"approval_reason": "test fixture"},
        )
        persist_sensitive(
            s,
            Proposal(
                order_id=order.id,
                ttl_minutes=15,
                expires_at=utcnow() + timedelta(minutes=15),
            ),
            {"reasoning": "LLM says buy"},
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
        assert (
            sensitive_store(s).read(order.proposal, "reasoning")
            == "LLM says buy"
        )
        assert len(order.fills) == 1
        # Timestamps are timezone-aware (UTC).
        assert order.created_at.tzinfo is not None


def test_idempotency_key_unique(session_factory):
    with session_factory() as s:
        persist_sensitive(
            s,
            Order(
                idempotency_key="dup",
                ticker="AAPL",
                side="buy",
                order_type="market",
            ),
            {"approval_reason": "test fixture"},
        )
        s.commit()
    with session_factory() as s:
        with pytest.raises(Exception):
            persist_sensitive(
                s,
                Order(
                    idempotency_key="dup",
                    ticker="MSFT",
                    side="buy",
                    order_type="market",
                ),
                {"approval_reason": "test fixture"},
            )
            s.commit()


def test_auth_session_hash_is_unique(session_factory):
    now = utcnow()
    with session_factory() as session:
        session.add(
            AuthSession(
                token_hash="a" * 64,
                csrf_hash="b" * 64,
                actor="operator:test",
                expires_at=now + timedelta(hours=8),
            )
        )
        session.commit()
    with session_factory() as session:
        session.add(
            AuthSession(
                token_hash="a" * 64,
                csrf_hash="c" * 64,
                actor="operator:test",
                expires_at=now + timedelta(hours=8),
            )
        )
        with pytest.raises(Exception):
            session.commit()


def test_policy_rows_round_trip(session_factory):
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    with session_factory() as session:
        session.add(
            RateWindow(
                bucket_key="a" * 64,
                policy_name="chat",
                window_started_at=now,
                expires_at=now + timedelta(minutes=10),
                hits=1,
            )
        )
        session.add(
            ConcurrencyLease(
                resource_key="backtest:global",
                owner="test:owner",
                expires_at=now + timedelta(minutes=10),
            )
        )
        session.add(
            ProviderBudgetDay(
                provider="gemini",
                budget_day=date(2026, 7, 27),
                calls_used=1,
                input_tokens_used=100,
                output_tokens_used=50,
            )
        )
        session.add(
            ProviderReservation(
                reservation_id="reservation-1",
                provider="gemini",
                category="chat",
                request_id="request-1",
                budget_day=date(2026, 7, 27),
                input_reserved=100,
                output_reserved=50,
                expires_at=now + timedelta(minutes=5),
            )
        )
        persist_sensitive(
            session,
            PanicReceipt(
                account_scope="alpaca-paper",
                request_id="request-1",
                state="started",
                expires_at=now + timedelta(seconds=90),
            ),
            {"response_json": "{}"},
        )
        session.commit()

    with session_factory() as session:
        assert session.get(RateWindow, "a" * 64).hits == 1
        assert session.get(ConcurrencyLease, "backtest:global").owner == "test:owner"
        assert session.get(
            ProviderBudgetDay, ("gemini", date(2026, 7, 27))
        ).calls_used == 1
        assert session.get(ProviderReservation, "reservation-1").state == "reserved"
        assert session.get(PanicReceipt, "alpaca-paper").request_id == "request-1"


def test_mutation_interlock_model_is_nonexpiring_and_fenced():
    table = Base.metadata.tables["mutation_interlocks"]

    assert {column.name for column in table.columns} == {
        "resource_key",
        "owner",
        "generation",
        "operation",
        "state",
        "outcome_code",
        "worker_finished_at",
        "created_at",
        "updated_at",
    }
    assert "expires_at" not in table.columns
    assert table.columns.resource_key.primary_key is True
    assert table.columns.worker_finished_at.nullable is True


def test_sqlite_database_and_sidecars_are_owner_only(tmp_path):
    path = tmp_path / "private.db"
    engine = create_db_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE private_data (id INTEGER)")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(f"{path.name}{suffix}")
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
