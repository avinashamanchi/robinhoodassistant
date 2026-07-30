"""Capability-minimal, read-only broker/local preflight reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ..assets import broker_symbol_matches_local
from ..broker.base import BrokerClient
from ..broker.models import OrderStatus
from ..db.models import (
    FILL_RECONCILIATION_SUPERSEDED,
    FILL_RECONCILIATION_TRUSTED,
    PLAN_CANCEL_INDETERMINATE,
    PLAN_CANCEL_REQUESTED,
    Fill,
    Order,
)


_LOCAL_BROKER_OPEN_STATUSES = frozenset(
    {
        OrderStatus.SUBMITTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    }
)
_LOCAL_UNCERTAIN_STATUSES = frozenset(
    {
        OrderStatus.SUBMITTING.value,
        OrderStatus.ACCEPTANCE_UNKNOWN.value,
    }
)
_REMOTE_OPEN_STATUSES = frozenset(
    {
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
    }
)
_UNRESOLVED_CANCEL_STATES = frozenset(
    {PLAN_CANCEL_REQUESTED, PLAN_CANCEL_INDETERMINATE}
)


@dataclass(frozen=True, slots=True)
class ReadOnlyReconciliationSnapshot:
    """Value-only proof returned by the preflight reconciliation capability."""

    orders_match: bool
    positions_match: bool
    broker_open_order_count: int
    local_open_order_count: int
    drift_symbols: tuple[str, ...]


class PreflightReconciliationProbe(Protocol):
    """The complete service capability visible to morning preflight."""

    def inspect_reconciliation(self) -> ReadOnlyReconciliationSnapshot: ...


class ReadOnlyPreflightService:
    """Observe broker and local state without writes, decrypts, or mutations."""

    __slots__ = ("_broker", "_session_factory")

    def __init__(
        self,
        broker: BrokerClient,
        session_factory: sessionmaker,
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory

    def inspect_reconciliation(self) -> ReadOnlyReconciliationSnapshot:
        remote_orders = self._broker.get_open_orders()
        remote_positions = self._broker.get_positions()

        with self._session_factory() as session:
            local_orders = session.execute(
                select(
                    Order.idempotency_key,
                    Order.ticker,
                    Order.status,
                    Order.broker_order_id,
                    Order.plan_cancel_state,
                    Order.qty,
                ).where(
                    Order.status.in_(
                        _LOCAL_BROKER_OPEN_STATUSES
                        | _LOCAL_UNCERTAIN_STATUSES
                    )
                )
            ).all()
            local_fills = session.execute(
                select(
                    Fill.ticker,
                    Fill.side,
                    Fill.qty,
                    Fill.broker_fill_id,
                    Fill.reconciliation_state,
                )
            ).all()

        local_by_client: dict[
            str,
            tuple[str, str, str, str, Decimal],
        ] = {}
        local_broker_ids: set[str] = set()
        orders_match = True
        for (
            client_id,
            ticker,
            status,
            broker_id,
            cancel_state,
            qty,
        ) in local_orders:
            if (
                not isinstance(client_id, str)
                or not client_id.strip()
                or client_id in local_by_client
                or status in _LOCAL_UNCERTAIN_STATUSES
                or not isinstance(broker_id, str)
                or not broker_id.strip()
                or broker_id in local_broker_ids
                or cancel_state in _UNRESOLVED_CANCEL_STATES
                or not isinstance(qty, Decimal)
                or not qty.is_finite()
                or qty <= 0
            ):
                orders_match = False
            else:
                local_broker_ids.add(broker_id)
                local_by_client[client_id] = (
                    ticker,
                    status,
                    broker_id,
                    cancel_state,
                    qty,
                )

        remote_by_client: dict[str, object] = {}
        remote_broker_ids: set[str] = set()
        for remote in remote_orders:
            client_id = getattr(remote, "idempotency_key", None)
            broker_id = getattr(remote, "broker_order_id", None)
            status = getattr(remote, "status", None)
            ticker = getattr(remote, "ticker", None)
            filled_qty = getattr(remote, "filled_qty", None)
            if (
                not isinstance(client_id, str)
                or not client_id.strip()
                or client_id in remote_by_client
                or not isinstance(broker_id, str)
                or not broker_id.strip()
                or broker_id in remote_broker_ids
                or status not in _REMOTE_OPEN_STATUSES
                or not isinstance(ticker, str)
                or not ticker.strip()
                or not isinstance(filled_qty, Decimal)
                or not filled_qty.is_finite()
                or filled_qty < 0
            ):
                orders_match = False
                continue
            remote_broker_ids.add(broker_id)
            remote_by_client[client_id] = remote

        if set(local_by_client) != set(remote_by_client):
            orders_match = False
        for client_id in set(local_by_client).intersection(remote_by_client):
            (
                local_ticker,
                local_status,
                local_broker_id,
                _,
                local_qty,
            ) = local_by_client[client_id]
            remote = remote_by_client[client_id]
            remote_status = getattr(remote, "status")
            remote_ticker = getattr(remote, "ticker")
            remote_filled_qty = getattr(remote, "filled_qty")
            if (
                getattr(remote, "broker_order_id") != local_broker_id
                or remote_status.value != local_status
                or remote_filled_qty > local_qty
                or (
                    remote_status is OrderStatus.SUBMITTED
                    and remote_filled_qty != 0
                )
                or (
                    remote_status is OrderStatus.PARTIALLY_FILLED
                    and not (0 < remote_filled_qty < local_qty)
                )
            ):
                orders_match = False
            try:
                ticker_matches = broker_symbol_matches_local(
                    remote_ticker,
                    local_ticker,
                )
            except ValueError:
                ticker_matches = False
            if not ticker_matches:
                orders_match = False

        local_position: dict[str, Decimal] = {}
        positions_match = True
        drift_symbols: set[str] = set()
        for (
            ticker,
            side,
            qty,
            broker_fill_id,
            reconciliation_state,
        ) in local_fills:
            if reconciliation_state == FILL_RECONCILIATION_SUPERSEDED:
                continue
            symbol = str(ticker).upper()
            if (
                reconciliation_state != FILL_RECONCILIATION_TRUSTED
                or not isinstance(broker_fill_id, str)
                or not broker_fill_id.strip()
                or not isinstance(qty, Decimal)
                or not qty.is_finite()
                or qty <= 0
                or side not in {"buy", "sell"}
            ):
                positions_match = False
                drift_symbols.add(symbol)
                continue
            delta = qty if side == "buy" else -qty
            local_position[symbol] = (
                local_position.get(symbol, Decimal(0)) + delta
            )

        broker_position: dict[str, Decimal] = {}
        for position in remote_positions:
            symbol = str(getattr(position, "ticker", "")).upper()
            qty = getattr(position, "qty", None)
            if (
                not symbol
                or symbol in broker_position
                or not isinstance(qty, Decimal)
                or not qty.is_finite()
                or qty == 0
            ):
                positions_match = False
                if symbol:
                    drift_symbols.add(symbol)
                continue
            broker_position[symbol] = qty

        for symbol in set(local_position).union(broker_position):
            if local_position.get(symbol, Decimal(0)) != broker_position.get(
                symbol,
                Decimal(0),
            ):
                positions_match = False
                drift_symbols.add(symbol)

        return ReadOnlyReconciliationSnapshot(
            orders_match=orders_match,
            positions_match=positions_match,
            broker_open_order_count=len(remote_orders),
            local_open_order_count=len(local_orders),
            drift_symbols=tuple(sorted(drift_symbols)),
        )
