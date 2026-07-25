"""Coherent point-in-time reads for persisted operator safety truth."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import event, update

from trading_assistant.broker.models import OrderStatus
from trading_assistant.db.models import Fill, Order, utcnow
from trading_assistant.orders.safety_state import (
    read_persisted_safety_truth,
)


def _terminal_order(session_factory, key: str) -> int:
    with session_factory() as session:
        order = Order(
            idempotency_key=key,
            ticker="AAPL",
            side="buy",
            order_type="market",
            notional=Decimal("100"),
            status=OrderStatus.FILLED.value,
            acceptance_state="accepted",
        )
        session.add(order)
        session.commit()
        return order.id


def _commit_unknown_order_and_orphan_fill(
    session_factory,
    order_id: int,
    *,
    fill_key: str,
) -> tuple[int, datetime]:
    with session_factory() as session:
        session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(
                status=OrderStatus.ACCEPTANCE_UNKNOWN.value,
                acceptance_state=OrderStatus.ACCEPTANCE_UNKNOWN.value,
                last_error_code="broker_submission_unknown",
                updated_at=utcnow(),
                version=Order.version + 1,
            )
        )
        fill = Fill(
            order_id=None,
            ticker="MSFT",
            side="sell",
            qty=Decimal("1"),
            price=Decimal("200"),
            broker_fill_id=fill_key,
        )
        session.add(fill)
        session.flush()
        fill_id = fill.id
        session.commit()
    return fill_id, utcnow()


def test_safety_read_never_mixes_categories_across_sqlite_wal_snapshots(
    engine,
    session_factory,
):
    order_id = _terminal_order(
        session_factory,
        "coherent-safety-snapshot-order",
    )
    interleaved = False
    fill_id: int | None = None
    writer_committed_at: datetime | None = None

    def commit_between_categories(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        nonlocal interleaved, fill_id, writer_committed_at
        if (
            not interleaved
            and "FROM orders" in statement
            and "orders.status IN" in statement
        ):
            interleaved = True
            fill_id, writer_committed_at = (
                _commit_unknown_order_and_orphan_fill(
                    session_factory,
                    order_id,
                    fill_key="coherent-safety-snapshot-fill",
                )
            )

    event.listen(
        engine,
        "after_cursor_execute",
        commit_between_categories,
    )
    try:
        truth = read_persisted_safety_truth(session_factory)
    finally:
        event.remove(
            engine,
            "after_cursor_execute",
            commit_between_categories,
        )

    assert interleaved is True
    assert fill_id is not None
    assert writer_committed_at is not None
    # The writer publishes both facts atomically. A reader may observe the
    # coherent state before or after that commit, never one category from each.
    observed_pair = (
        order_id
        in truth.unsafe_local_state.live_or_unknown_order_ids,
        fill_id in truth.unsafe_local_state.unsafe_fill_ids,
    )
    assert observed_pair in {(False, False), (True, True)}
    assert observed_pair == (False, False)
    assert truth.observed_at < writer_committed_at

    current = read_persisted_safety_truth(session_factory)
    assert order_id in (
        current.unsafe_local_state.live_or_unknown_order_ids
    )
    assert fill_id in current.unsafe_local_state.unsafe_fill_ids


def test_safety_read_includes_writer_committed_before_snapshot_acquisition(
    session_factory,
):
    order_id = _terminal_order(
        session_factory,
        "precommitted-safety-snapshot-order",
    )
    fill_id, writer_committed_at = (
        _commit_unknown_order_and_orphan_fill(
            session_factory,
            order_id,
            fill_key="precommitted-safety-snapshot-fill",
        )
    )

    truth = read_persisted_safety_truth(session_factory)

    assert truth.observed_at >= writer_committed_at
    assert order_id in (
        truth.unsafe_local_state.live_or_unknown_order_ids
    )
    assert fill_id in truth.unsafe_local_state.unsafe_fill_ids


def test_safety_read_reuses_an_active_sqlite_transaction_without_nested_begin(
    session_factory,
):
    with session_factory() as session:
        connection = session.connection()
        connection.exec_driver_sql("BEGIN")
        driver_connection = (
            connection.connection.driver_connection
        )
        assert driver_connection.in_transaction is True

        truth = read_persisted_safety_truth(session)

        assert truth.complete is True
        assert truth.state == "locally_clear"
        assert driver_connection.in_transaction is True
        session.rollback()
