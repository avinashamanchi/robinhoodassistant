from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session as SASession

from trading_assistant.broker.models import (
    BrokerOrderType,
    BrokerFill,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
)
from trading_assistant.db.models import (
    AccountRiskState,
    AuditEvent,
    CircuitBreakerState,
    Fill,
    Order,
    ReconciliationCursor,
    RuleGroup,
)
from trading_assistant.orders.reconciliation import ReconciliationService
from trading_assistant.orders.repository import OrderRepository
from trading_assistant.ops.tenure import (
    ProcessIdentity,
    RuntimeTenureGuard,
    RuntimeTenureService,
    install_runtime_mutation_barrier,
)
from trading_assistant.risk.breakers import BreakerScope, BreakerService
from tests.conftest import decrypt_test_sensitive


SELL_TIME = datetime(2026, 7, 17, 13, 31, 24, tzinfo=timezone.utc)
BUY_TIME = datetime(2026, 7, 20, 13, 31, 16, tzinfo=timezone.utc)


class _HistoryBroker:
    reconciliation_key = "alpaca"

    def __init__(
        self,
        *,
        positions: list[Position] | None = None,
        fill_price: Decimal = Decimal("100.00"),
    ) -> None:
        self.positions = positions or []
        self.activities = [
            BrokerFill(
                broker_fill_id="fill-sell",
                broker_order_id="broker-sell",
                ticker="AAPL",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("100.00"),
                filled_at=SELL_TIME,
            ),
            BrokerFill(
                broker_fill_id="fill-buy",
                broker_order_id="broker-buy",
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=fill_price,
                filled_at=BUY_TIME,
            ),
        ]
        self.orders = {
            "broker-sell": OrderResult(
                idempotency_key="legacy-sell",
                broker_order_id="broker-sell",
                status=OrderStatus.FILLED,
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("100.00"),
                submitted_at=SELL_TIME,
                ticker="AAPL",
                side=OrderSide.SELL,
                order_type=BrokerOrderType.MARKET,
                requested_qty=Decimal("1"),
                requested_notional=None,
                limit_price=None,
            ),
            "broker-buy": OrderResult(
                idempotency_key="legacy-buy",
                broker_order_id="broker-buy",
                status=OrderStatus.FILLED,
                filled_qty=Decimal("1"),
                avg_fill_price=fill_price,
                submitted_at=BUY_TIME,
                ticker="AAPL",
                side=OrderSide.BUY,
                order_type=BrokerOrderType.LIMIT,
                requested_qty=Decimal("1"),
                requested_notional=None,
                limit_price=Decimal("101"),
            ),
        }
        self.activity_reads = 0

    def get_positions(self):
        return list(self.positions)

    def get_open_orders(self):
        return []

    def get_fill_activities(self, after=None):
        assert after is None
        self.activity_reads += 1
        return list(self.activities)

    def get_order_status(self, order_id):
        return self.orders[order_id]


class _AdoptionGuard:
    def __init__(self):
        self.transaction_entries = 0
        self.transaction_renewals = 0

    @contextmanager
    def exclusive_transaction_renewal(self):
        self.transaction_entries += 1
        yield

    def renew_in_transaction(self, _connection):
        self.transaction_renewals += 1


def _legacy_database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                idempotency_key VARCHAR(64) NOT NULL,
                ticker VARCHAR(16) NOT NULL,
                side VARCHAR(8) NOT NULL,
                order_type VARCHAR(8) NOT NULL,
                qty NUMERIC,
                notional NUMERIC,
                limit_price NUMERIC,
                status VARCHAR(20) NOT NULL,
                broker_order_id VARCHAR(64),
                submission_started_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE fills (
                id INTEGER PRIMARY KEY,
                order_id INTEGER,
                ticker VARCHAR(16) NOT NULL,
                side VARCHAR(8) NOT NULL,
                qty NUMERIC NOT NULL,
                price NUMERIC NOT NULL,
                broker_fill_id VARCHAR(64),
                filled_at DATETIME NOT NULL
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO orders (
                id,idempotency_key,ticker,side,order_type,qty,notional,
                limit_price,status,broker_order_id,submission_started_at,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    1,
                    "legacy-sell",
                    "AAPL",
                    "sell",
                    "market",
                    "1",
                    None,
                    None,
                    "filled",
                    "broker-sell",
                    SELL_TIME.isoformat(),
                    SELL_TIME.isoformat(),
                    SELL_TIME.isoformat(),
                ),
                (
                    2,
                    "legacy-buy",
                    "AAPL",
                    "buy",
                    "limit",
                    "1",
                    None,
                    "101",
                    "filled",
                    "broker-buy",
                    BUY_TIME.isoformat(),
                    BUY_TIME.isoformat(),
                    BUY_TIME.isoformat(),
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO fills (
                id,order_id,ticker,side,qty,price,broker_fill_id,filled_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    1,
                    1,
                    "AAPL",
                    "sell",
                    "1",
                    "100",
                    "fill-sell",
                    SELL_TIME.isoformat(),
                ),
                (
                    2,
                    2,
                    "AAPL",
                    "buy",
                    "1",
                    "100",
                    "fill-buy",
                    BUY_TIME.isoformat(),
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)
    return path


def _trip_broker_drift(session_factory):
    return BreakerService(session_factory).trip(
        BreakerScope.broker_drift(),
        "history is not adopted",
        "operator:test",
        request_id="trip-before-adoption",
        audit_reason="test setup",
    )


def _adopt(
    session_factory,
    broker,
    legacy,
    *,
    request_id="adopt-history",
    maintenance_guard=None,
):
    from trading_assistant.ops.paper_history import adopt_flat_paper_history

    return adopt_flat_paper_history(
        session_factory,
        broker,
        legacy,
        actor="operator:test",
        reason="adopt exact flat paper history",
        request_id=request_id,
        observed_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        source_quiescence_checker=lambda _path: True,
        maintenance_guard=maintenance_guard or _AdoptionGuard(),
    )


def test_source_fingerprint_allows_only_sqlite_shm_read_mark_changes(
    tmp_path,
):
    from trading_assistant.ops.paper_history import _source_fingerprint

    legacy = _legacy_database(tmp_path / "legacy.db")
    shared_memory = Path(f"{legacy}-shm")
    shared_memory.write_bytes(b"\x00" * 64)
    shared_memory.chmod(0o600)

    before = _source_fingerprint(legacy)
    shared_memory.write_bytes(b"\x01" * 64)
    after = _source_fingerprint(legacy)

    assert after == before


def test_reader_and_fingerprint_cover_uncheckpointed_wal_bytes(tmp_path):
    from trading_assistant.ops.paper_history import (
        _load_legacy_history,
        _source_fingerprint,
    )

    legacy = _legacy_database(tmp_path / "legacy-wal.db")
    connection = sqlite3.connect(legacy)
    try:
        assert connection.execute(
            "PRAGMA journal_mode=WAL"
        ).fetchone()[0].lower() == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "UPDATE fills SET price='100.00' WHERE id=2"
        )
        connection.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{legacy}{suffix}")
            assert sidecar.exists()
            sidecar.chmod(0o600)

        before = _source_fingerprint(legacy)
        history = _load_legacy_history(legacy)

        connection.execute(
            "UPDATE fills SET price='100.01' WHERE id=2"
        )
        connection.commit()
        for suffix in ("-wal", "-shm"):
            Path(f"{legacy}{suffix}").chmod(0o600)
        after = _source_fingerprint(legacy)
    finally:
        connection.close()

    assert {
        fill.price
        for fill in history.fills
        if fill.broker_fill_id == "fill-buy"
    } == {Decimal("100")}
    assert after != before


def test_legacy_reader_opens_uri_metacharacter_path_without_confusion(
    tmp_path,
):
    from trading_assistant.ops.paper_history import _load_legacy_history

    legacy = _legacy_database(tmp_path / "legacy # percent% question?.db")

    history = _load_legacy_history(legacy)

    assert {order.broker_order_id for order in history.orders} == {
        "broker-buy",
        "broker-sell",
    }
    assert {fill.broker_fill_id for fill in history.fills} == {
        "fill-buy",
        "fill-sell",
    }


def test_adoption_imports_only_exact_flat_broker_history_and_keeps_breaker(
    tmp_path,
    session_factory,
):
    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker()
    tripped = _trip_broker_drift(session_factory)
    guard = _AdoptionGuard()

    receipt = _adopt(
        session_factory,
        broker,
        legacy,
        maintenance_guard=guard,
    )

    with session_factory() as session:
        orders = session.scalars(select(Order).order_by(Order.id)).all()
        fills = session.scalars(select(Fill).order_by(Fill.filled_at)).all()
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "paper_history.adopt"
            )
        )
        breaker = session.get(
            CircuitBreakerState,
            BreakerScope.broker_drift().key,
        )

    assert receipt.orders_imported == 2
    assert receipt.fills_imported == 2
    assert receipt.breaker_generation == tripped.generation
    assert len(receipt.source_fingerprint) == 64
    assert len(receipt.history_digest) == 64
    assert len(receipt.broker_digest) == 64
    assert len(receipt.import_digest) == 64
    assert broker.activity_reads == 2
    assert guard.transaction_entries == 1
    assert guard.transaction_renewals >= 3
    assert {order.status for order in orders} == {"filled"}
    assert {order.acceptance_state for order in orders} == {"filled"}
    assert {
        order.approval_actor for order in orders
    } == {"history_import:broker_truth"}
    assert {order.approved_at for order in orders} == {None}
    assert {order.submission_kind for order in orders} == {
        "history_import"
    }
    assert {order.submission_attempt for order in orders} == {0}
    assert {order.submission_started_at for order in orders} == {
        SELL_TIME,
        BUY_TIME,
    }
    assert all(
        "original approval provenance is unavailable"
        in decrypt_test_sensitive(order, "approval_reason")
        for order in orders
    )
    assert {fill.broker_fill_id for fill in fills} == {
        "fill-buy",
        "fill-sell",
    }
    assert sum(
        fill.qty if fill.side == "buy" else -fill.qty
        for fill in fills
    ) == Decimal(0)
    assert audit is not None
    audit_detail = json.loads(
        decrypt_test_sensitive(audit, "detail_json")
    )
    assert audit_detail == {
        "breaker_generation": receipt.breaker_generation,
        "broker_digest": receipt.broker_digest,
        "fills_imported": receipt.fills_imported,
        "history_digest": receipt.history_digest,
        "import_digest": receipt.import_digest,
        "orders_imported": receipt.orders_imported,
        "source_fingerprint": receipt.source_fingerprint,
    }
    assert breaker is not None
    assert breaker.tripped is True
    assert breaker.generation == tripped.generation


def test_adopted_history_replays_idempotently_through_normal_reconciliation(
    tmp_path,
    session_factory,
):
    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker()
    _trip_broker_drift(session_factory)
    _adopt(
        session_factory,
        broker,
        legacy,
        request_id="adopt-before-reconciliation",
    )

    report = ReconciliationService(
        session_factory,
        broker,
        OrderRepository(session_factory),
    ).reconcile(
        actor="runtime:test",
        reason="prove imported history against broker truth",
        request_id="reconcile-adopted-history",
    )

    with session_factory() as session:
        fill_count = session.scalar(select(func.count(Fill.id)))
        cursor = session.get(
            ReconciliationCursor,
            (broker.reconciliation_key, "fills"),
        )

    assert report.broker_drift == ()
    assert report.inserted_fills == 0
    assert fill_count == 2
    assert cursor is not None
    assert cursor.last_activity_id == "fill-buy"
    assert broker.activity_reads == 3


def test_adoption_refuses_non_flat_broker_before_any_import(
    tmp_path,
    session_factory,
):
    from trading_assistant.ops.paper_history import (
        PaperHistoryAdoptionError,
        adopt_flat_paper_history,
    )

    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker(
        positions=[
            Position(
                ticker="AAPL",
                qty=Decimal("1"),
                avg_entry_price=Decimal("100"),
                current_price=Decimal("100"),
            )
        ]
    )
    _trip_broker_drift(session_factory)

    try:
        adopt_flat_paper_history(
            session_factory,
            broker,
            legacy,
            actor="operator:test",
            reason="must stay flat",
            request_id="reject-non-flat",
            observed_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            source_quiescence_checker=lambda _path: True,
            maintenance_guard=_AdoptionGuard(),
        )
    except PaperHistoryAdoptionError as exc:
        assert exc.stable_code == "paper_history_account_not_flat"
    else:
        raise AssertionError("non-flat account was adopted")

    with session_factory() as session:
        assert session.scalar(select(func.count(Order.id))) == 0
        assert session.scalar(select(func.count(Fill.id))) == 0


def test_adoption_refuses_legacy_economics_that_do_not_match_broker(
    tmp_path,
    session_factory,
):
    from trading_assistant.ops.paper_history import (
        PaperHistoryAdoptionError,
        adopt_flat_paper_history,
    )

    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker(fill_price=Decimal("101.00"))
    _trip_broker_drift(session_factory)

    try:
        adopt_flat_paper_history(
            session_factory,
            broker,
            legacy,
            actor="operator:test",
            reason="economics must match",
            request_id="reject-mismatch",
            observed_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            source_quiescence_checker=lambda _path: True,
            maintenance_guard=_AdoptionGuard(),
        )
    except PaperHistoryAdoptionError as exc:
        assert exc.stable_code == "paper_history_broker_mismatch"
    else:
        raise AssertionError("mismatched history was adopted")

    with session_factory() as session:
        assert session.scalar(select(func.count(Order.id))) == 0
        assert session.scalar(select(func.count(Fill.id))) == 0


def test_adoption_requires_existing_tripped_broker_drift_breaker(
    tmp_path,
    session_factory,
):
    from trading_assistant.ops.paper_history import (
        PaperHistoryAdoptionError,
        adopt_flat_paper_history,
    )

    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker()

    with pytest.raises(
        PaperHistoryAdoptionError,
        match="^paper_history_broker_drift_breaker_required$",
    ):
        adopt_flat_paper_history(
            session_factory,
            broker,
            legacy,
            actor="operator:test",
            reason="breaker is mandatory",
            request_id="reject-missing-breaker",
            observed_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            source_quiescence_checker=lambda _path: True,
            maintenance_guard=_AdoptionGuard(),
        )

    assert broker.activity_reads == 0
    with session_factory() as session:
        assert session.scalar(select(func.count(Order.id))) == 0
        assert session.scalar(select(func.count(Fill.id))) == 0


def test_adoption_rejects_stale_reconciliation_cursor_as_nonfresh_state(
    tmp_path,
    session_factory,
):
    from trading_assistant.ops.paper_history import (
        PaperHistoryAdoptionError,
    )

    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker()
    _trip_broker_drift(session_factory)
    with session_factory() as session:
        session.add(
            ReconciliationCursor(
                broker="alpaca",
                stream="fills",
                last_activity_id="future-fill",
                last_activity_at=BUY_TIME,
                version=1,
            )
        )
        session.commit()

    with pytest.raises(
        PaperHistoryAdoptionError,
        match="^paper_history_target_not_fresh$",
    ):
        _adopt(session_factory, broker, legacy)

    assert broker.activity_reads == 0


@pytest.mark.parametrize(
    "state_kind",
    ("rule_group", "account_risk"),
)
def test_adoption_rejects_other_nonfresh_execution_state(
    tmp_path,
    session_factory,
    state_kind,
):
    from trading_assistant.ops.paper_history import (
        PaperHistoryAdoptionError,
    )

    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker()
    _trip_broker_drift(session_factory)
    with session_factory() as session:
        if state_kind == "rule_group":
            session.add(
                RuleGroup(
                    group_key="stale-rule-group",
                    state="active",
                )
            )
        else:
            session.add(
                AccountRiskState(
                    asset_class="equity",
                    high_water_mark=Decimal("100000"),
                    last_equity=Decimal("100000"),
                    updated_at=BUY_TIME,
                )
            )
        session.commit()

    with pytest.raises(
        PaperHistoryAdoptionError,
        match="^paper_history_target_not_fresh$",
    ):
        _adopt(session_factory, broker, legacy)

    assert broker.activity_reads == 0


def test_adoption_revalidates_exact_breaker_generation_inside_import(
    tmp_path,
    session_factory,
):
    from trading_assistant.ops.paper_history import PaperHistoryAdoptionError

    class _GenerationChangingBroker(_HistoryBroker):
        def get_fill_activities(self, after=None):
            result = super().get_fill_activities(after=after)
            if self.activity_reads == 2:
                BreakerService(session_factory).trip(
                    BreakerScope.broker_drift(),
                    "new drift evidence",
                    "runtime:test",
                    request_id="trip-during-adoption",
                    audit_reason="simulate concurrent drift",
                )
            return result

    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _GenerationChangingBroker()
    _trip_broker_drift(session_factory)

    with pytest.raises(
        PaperHistoryAdoptionError,
        match="^paper_history_broker_drift_breaker_changed$",
    ):
        _adopt(session_factory, broker, legacy)

    with session_factory() as session:
        assert session.scalar(select(func.count(Order.id))) == 0
        assert session.scalar(select(func.count(Fill.id))) == 0


def test_adoption_requires_order_type_side_limit_and_average_identity(
    tmp_path,
    session_factory,
):
    from trading_assistant.ops.paper_history import PaperHistoryAdoptionError

    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker()
    broker.orders["broker-buy"] = replace(
        broker.orders["broker-buy"],
        order_type=BrokerOrderType.MARKET,
        limit_price=None,
    )
    _trip_broker_drift(session_factory)

    with pytest.raises(
        PaperHistoryAdoptionError,
        match="^paper_history_broker_mismatch$",
    ):
        _adopt(session_factory, broker, legacy)

    with session_factory() as session:
        assert session.scalar(select(func.count(Order.id))) == 0


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("qty", "1.000000001"),
        ("limit_price", "101.000000001"),
        ("qty", "100000000000000"),
        ("limit_price", "100000000000000"),
    ),
)
def test_legacy_order_economics_must_fit_persisted_numeric_shape(
    tmp_path,
    column,
    value,
):
    from trading_assistant.ops.paper_history import (
        PaperHistoryAdoptionError,
        _load_legacy_history,
    )

    legacy = _legacy_database(tmp_path / "legacy.db")
    connection = sqlite3.connect(legacy)
    try:
        if column == "qty":
            connection.execute(
                "UPDATE orders SET qty=? WHERE id=2",
                (value,),
            )
        else:
            connection.execute(
                "UPDATE orders SET limit_price=? WHERE id=2",
                (value,),
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        PaperHistoryAdoptionError,
        match="^paper_history_legacy_invalid$",
    ):
        _load_legacy_history(legacy)


def test_adoption_supports_partially_filled_canceled_terminal_order(
    tmp_path,
    session_factory,
):
    legacy = _legacy_database(tmp_path / "legacy.db")
    connection = sqlite3.connect(legacy)
    try:
        connection.execute(
            "UPDATE orders SET status='canceled',qty='2' WHERE id=1"
        )
        connection.commit()
    finally:
        connection.close()
    broker = _HistoryBroker()
    broker.orders["broker-sell"] = replace(
        broker.orders["broker-sell"],
        status=OrderStatus.CANCELED,
        requested_qty=Decimal("2"),
    )
    _trip_broker_drift(session_factory)

    receipt = _adopt(session_factory, broker, legacy)

    with session_factory() as session:
        imported = session.scalar(
            select(Order).where(
                Order.broker_order_id == "broker-sell"
            )
        )
        imported_fill_qty = session.scalar(
            select(func.sum(Fill.qty)).where(
                Fill.order_id == imported.id
            )
        )
    assert receipt.orders_imported == 2
    assert imported is not None
    assert imported.status == OrderStatus.CANCELED.value
    assert imported.qty == Decimal("2")
    assert imported_fill_qty == Decimal("1")


def test_adoption_rejects_partial_order_with_unproven_requested_quantity(
    tmp_path,
    session_factory,
):
    from trading_assistant.ops.paper_history import PaperHistoryAdoptionError

    legacy = _legacy_database(tmp_path / "legacy.db")
    connection = sqlite3.connect(legacy)
    try:
        connection.execute(
            "UPDATE orders SET status='canceled',qty='500' WHERE id=1"
        )
        connection.commit()
    finally:
        connection.close()
    broker = _HistoryBroker()
    broker.orders["broker-sell"] = replace(
        broker.orders["broker-sell"],
        status=OrderStatus.CANCELED,
        requested_qty=Decimal("2"),
    )
    _trip_broker_drift(session_factory)

    with pytest.raises(
        PaperHistoryAdoptionError,
        match="^paper_history_broker_mismatch$",
    ):
        _adopt(session_factory, broker, legacy)

    with session_factory() as session:
        assert session.scalar(select(func.count(Order.id))) == 0


def test_adoption_accepts_broker_rounded_multi_fill_average(
    tmp_path,
    session_factory,
):
    legacy = _legacy_database(tmp_path / "legacy.db")
    connection = sqlite3.connect(legacy)
    try:
        connection.execute("DELETE FROM fills")
        connection.execute("UPDATE orders SET qty='3'")
        connection.executemany(
            """
            INSERT INTO fills (
                id,order_id,ticker,side,qty,price,broker_fill_id,filled_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    1,
                    1,
                    "AAPL",
                    "sell",
                    "1",
                    "100",
                    "fill-sell-1",
                    SELL_TIME.isoformat(),
                ),
                (
                    2,
                    1,
                    "AAPL",
                    "sell",
                    "2",
                    "101",
                    "fill-sell-2",
                    SELL_TIME.isoformat(),
                ),
                (
                    3,
                    2,
                    "AAPL",
                    "buy",
                    "1",
                    "100",
                    "fill-buy-1",
                    BUY_TIME.isoformat(),
                ),
                (
                    4,
                    2,
                    "AAPL",
                    "buy",
                    "2",
                    "101",
                    "fill-buy-2",
                    BUY_TIME.isoformat(),
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    broker = _HistoryBroker()
    broker.activities = [
        BrokerFill(
            broker_fill_id=f"fill-{side}-{index}",
            broker_order_id=f"broker-{side}",
            ticker="AAPL",
            side=side,
            qty=qty,
            price=price,
            filled_at=(
                SELL_TIME if side == "sell" else BUY_TIME
            ),
        )
        for side in ("sell", "buy")
        for index, qty, price in (
            (1, Decimal("1"), Decimal("100")),
            (2, Decimal("2"), Decimal("101")),
        )
    ]
    for side in ("sell", "buy"):
        broker.orders[f"broker-{side}"] = replace(
                broker.orders[f"broker-{side}"],
                filled_qty=Decimal("3"),
                avg_fill_price=Decimal("100.666666667"),
                requested_qty=Decimal("3"),
            )
    _trip_broker_drift(session_factory)

    receipt = _adopt(session_factory, broker, legacy)

    assert receipt.fills_imported == 4


def test_commit_response_loss_is_reconciled_from_durable_audit_and_rows(
    tmp_path,
    session_factory,
    monkeypatch,
):
    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker()
    _trip_broker_drift(session_factory)
    original_commit = SASession.commit
    calls = 0

    def commit_then_lose_response(session):
        nonlocal calls
        calls += 1
        original_commit(session)
        raise RuntimeError("simulated lost commit response")

    monkeypatch.setattr(SASession, "commit", commit_then_lose_response)

    receipt = _adopt(session_factory, broker, legacy)

    assert calls == 1
    assert receipt.commit_reconciled_after_error is True
    with session_factory() as session:
        assert session.scalar(select(func.count(Order.id))) == 2
        assert session.scalar(select(func.count(Fill.id))) == 2


def test_adoption_renews_real_maintenance_tenure_in_import_transaction(
    tmp_path,
    session_factory,
):
    legacy = _legacy_database(tmp_path / "legacy.db")
    broker = _HistoryBroker()
    _trip_broker_drift(session_factory)

    class _UnusedInspector:
        def inspect(self, _identity):
            raise AssertionError("fresh target needs no reclaim inspection")

    handle = RuntimeTenureService(
        session_factory,
        process_inspector=_UnusedInspector(),
    ).acquire_maintenance(
        ProcessIdentity(
            pid=4321,
            start_identity="paper-history-integration",
        ),
        ttl_seconds=30,
    )
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    engine = session_factory.kw["bind"]
    install_runtime_mutation_barrier(engine, guard)
    guard.start()

    try:
        receipt = _adopt(
            session_factory,
            broker,
            legacy,
            maintenance_guard=guard,
        )
    finally:
        released = guard.close()

    assert receipt.orders_imported == 2
    assert guard.lost is False
    assert released is True
