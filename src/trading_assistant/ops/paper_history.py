"""Adopt exact, closed Alpaca paper history into an otherwise fresh database.

This is a one-time stopped-writer recovery tool. It never changes broker state,
never clears a breaker, and refuses any account with current exposure.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from time import monotonic
from typing import Callable, Iterable
from uuid import uuid4

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from ..broker.models import (
    BrokerOrderType,
    BrokerFill,
    FILL_ECONOMIC_QUANTUM,
    FillQuantityRelation,
    OrderResult,
    OrderSide,
    OrderStatus,
    fill_quantity_relation,
    valid_cumulative_filled_qty,
    valid_fill_economic,
)
from ..config import BrokerKind, TradingMode, load_config
from ..db.models import (
    AuditEvent,
    Base,
    CircuitBreakerState,
    FILL_RECONCILIATION_TRUSTED,
    Fill,
    Order,
    Proposal,
    Rule,
    RuntimeTenure,
    SensitiveMigrationState,
)
from ..risk.breakers import BreakerKind, BreakerScope
from ..security.sensitive_fields import (
    persist_sensitive,
    sensitive_store,
)


class PaperHistoryAdoptionError(RuntimeError):
    """A stable refusal from the stopped-writer paper-history boundary."""

    def __init__(self, stable_code: str) -> None:
        self.stable_code = stable_code
        super().__init__(stable_code)


class PaperHistoryCommitUncertain(PaperHistoryAdoptionError):
    """The commit was attempted but durable outcome could not be proven."""

    def __init__(self, receipt: "PaperHistoryAdoptionReceipt") -> None:
        self.receipt = receipt
        super().__init__("paper_history_commit_outcome_unknown")


@dataclass(frozen=True)
class PaperHistoryAdoptionReceipt:
    orders_imported: int
    fills_imported: int
    breaker_generation: int
    source_fingerprint: str
    history_digest: str
    broker_digest: str
    import_digest: str
    commit_reconciled_after_error: bool = False


@dataclass(frozen=True)
class _LegacyOrder:
    idempotency_key: str
    ticker: str
    side: str
    order_type: str
    qty: Decimal
    notional: Decimal | None
    limit_price: Decimal | None
    broker_order_id: str
    status: str
    submission_started_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class _LegacyFill:
    broker_fill_id: str
    broker_order_id: str
    ticker: str
    side: str
    qty: Decimal
    price: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class _LegacyHistory:
    orders: tuple[_LegacyOrder, ...]
    fills: tuple[_LegacyFill, ...]


_REQUIRED_ORDER_COLUMNS = frozenset(
    {
        "id",
        "idempotency_key",
        "ticker",
        "side",
        "order_type",
        "qty",
        "notional",
        "limit_price",
        "status",
        "broker_order_id",
        "submission_started_at",
        "created_at",
        "updated_at",
    }
)
_REQUIRED_FILL_COLUMNS = frozenset(
    {
        "id",
        "order_id",
        "ticker",
        "side",
        "qty",
        "price",
        "broker_fill_id",
        "filled_at",
    }
)
_ORDER_ECONOMIC_QUANTUM = Decimal("0.000001")
_ORDER_ECONOMIC_MAX_EXCLUSIVE = Decimal("100000000000000")


def _valid_order_economic(value: Decimal) -> bool:
    try:
        normalized = value.quantize(_ORDER_ECONOMIC_QUANTUM)
    except InvalidOperation:
        return False
    return (
        value.is_finite()
        and value > 0
        and value < _ORDER_ECONOMIC_MAX_EXCLUSIVE
        and normalized == value
    )


def _decimal(value: object, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PaperHistoryAdoptionError(
            "paper_history_legacy_invalid"
        ) from None
    if not parsed.is_finite() or parsed <= 0:
        raise PaperHistoryAdoptionError("paper_history_legacy_invalid")
    return parsed


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PaperHistoryAdoptionError("paper_history_legacy_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PaperHistoryAdoptionError(
            "paper_history_legacy_invalid"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_text(
    value: object,
    *,
    maximum: int,
    case: str | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise PaperHistoryAdoptionError("paper_history_legacy_invalid")
    if case == "upper" and value != value.upper():
        raise PaperHistoryAdoptionError("paper_history_legacy_invalid")
    if case == "lower" and value != value.lower():
        raise PaperHistoryAdoptionError("paper_history_legacy_invalid")
    return value


def _safe_source(path: Path) -> Path:
    if not path.is_absolute():
        raise PaperHistoryAdoptionError("paper_history_source_invalid")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        parent = resolved.parent.stat()
    except OSError:
        raise PaperHistoryAdoptionError(
            "paper_history_source_invalid"
        ) from None
    if (
        path != resolved
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise PaperHistoryAdoptionError("paper_history_source_invalid")
    return resolved


def _source_members(path: Path) -> tuple[Path, ...]:
    members = [path]
    for suffix in ("-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if not os.path.lexists(candidate):
            continue
        try:
            metadata = candidate.lstat()
        except OSError:
            raise PaperHistoryAdoptionError(
                "paper_history_source_invalid"
            ) from None
        if (
            candidate.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
        ):
            raise PaperHistoryAdoptionError(
                "paper_history_source_invalid"
            )
        members.append(candidate)
    return tuple(members)


def _source_is_quiescent(path: Path) -> bool:
    """Prove no process currently has the checked source or sidecars open."""
    executable = next(
        (
            candidate
            for candidate in (
                Path("/usr/sbin/lsof"),
                Path("/usr/bin/lsof"),
            )
            if candidate.is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )
    if executable is None:
        return False
    try:
        result = subprocess.run(
            [
                str(executable),
                "-F",
                "p",
                "--",
                *(str(member) for member in _source_members(path)),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
            env={"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        result.returncode == 1
        and not result.stdout.strip()
        and not result.stderr.strip()
    )


def _source_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for member in _source_members(path):
        digest.update(member.name.encode("utf-8"))
        is_shared_memory = member == Path(f"{path}-shm")
        try:
            descriptor = os.open(
                member,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError:
            raise PaperHistoryAdoptionError(
                "paper_history_source_invalid"
            ) from None
        try:
            opened = os.fstat(descriptor)
            named = member.lstat()
            if (
                opened.st_dev != named.st_dev
                or opened.st_ino != named.st_ino
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise PaperHistoryAdoptionError(
                    "paper_history_source_changed"
                )
            digest.update(
                (
                    f"{opened.st_dev}:{opened.st_ino}:"
                    f"{stat.S_IMODE(opened.st_mode)}:{opened.st_size}"
                ).encode("ascii")
            )
            # SQLite readers may update read marks inside -shm even through a
            # query-only connection. Its identity and shape must stay stable,
            # while durable database and WAL bytes must match exactly.
            if is_shared_memory:
                continue
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(descriptor)
    return digest.hexdigest()


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> frozenset[str]:
    if table == "orders":
        rows = connection.execute("PRAGMA table_info(orders)")
    elif table == "fills":
        rows = connection.execute("PRAGMA table_info(fills)")
    else:
        raise PaperHistoryAdoptionError(
            "paper_history_legacy_schema_invalid"
        )
    return frozenset(str(row[1]) for row in rows)


def _load_legacy_history(path: Path) -> _LegacyHistory:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        database_rows = connection.execute(
            "PRAGMA database_list"
        ).fetchall()
        main_paths = [
            Path(str(row[2]))
            for row in database_rows
            if str(row[1]) == "main"
        ]
        if (
            len(main_paths) != 1
            or not main_paths[0].is_absolute()
            or not os.path.samefile(main_paths[0], path)
        ):
            raise PaperHistoryAdoptionError(
                "paper_history_source_changed"
            )
    except PaperHistoryAdoptionError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error:
        if connection is not None:
            connection.close()
        raise PaperHistoryAdoptionError(
            "paper_history_source_invalid"
        ) from None
    assert connection is not None
    try:
        if not _REQUIRED_ORDER_COLUMNS.issubset(
            _table_columns(connection, "orders")
        ) or not _REQUIRED_FILL_COLUMNS.issubset(
            _table_columns(connection, "fills")
        ):
            raise PaperHistoryAdoptionError(
                "paper_history_legacy_schema_invalid"
            )
        rows = connection.execute(
            """
            SELECT
                o.idempotency_key,o.ticker,o.side,o.order_type,o.qty,
                o.notional,o.limit_price,o.broker_order_id,o.status,
                o.created_at,
                o.updated_at,o.submission_started_at,
                f.broker_fill_id,f.ticker AS fill_ticker,
                f.side AS fill_side,f.qty AS fill_qty,f.price AS fill_price,
                f.filled_at
            FROM orders AS o
            JOIN fills AS f ON f.order_id=o.id
            WHERE o.status IN ('filled','canceled','expired')
            ORDER BY f.filled_at,f.id
            """
        ).fetchall()
        total_fills = connection.execute(
            "SELECT count(*) FROM fills"
        ).fetchone()[0]
    except PaperHistoryAdoptionError:
        raise
    except (OSError, sqlite3.Error):
        raise PaperHistoryAdoptionError(
            "paper_history_legacy_schema_invalid"
        ) from None
    finally:
        connection.close()
    if not rows or total_fills != len(rows):
        raise PaperHistoryAdoptionError("paper_history_legacy_invalid")

    orders: dict[str, _LegacyOrder] = {}
    fills: list[_LegacyFill] = []
    seen_fill_ids: set[str] = set()
    for row in rows:
        broker_order_id = _canonical_text(
            row["broker_order_id"],
            maximum=64,
        )
        broker_fill_id = _canonical_text(
            row["broker_fill_id"],
            maximum=64,
        )
        idempotency_key = _canonical_text(
            row["idempotency_key"],
            maximum=64,
        )
        ticker = _canonical_text(
            row["ticker"],
            maximum=16,
            case="upper",
        )
        side = _canonical_text(
            row["side"],
            maximum=8,
            case="lower",
        )
        order_type = _canonical_text(
            row["order_type"],
            maximum=8,
            case="lower",
        )
        order_status = _canonical_text(
            row["status"],
            maximum=20,
            case="lower",
        )
        fill_ticker = _canonical_text(
            row["fill_ticker"],
            maximum=16,
            case="upper",
        )
        fill_side = _canonical_text(
            row["fill_side"],
            maximum=8,
            case="lower",
        )
        if (
            broker_fill_id in seen_fill_ids
            or side not in {"buy", "sell"}
            or order_type not in {"market", "limit"}
            or order_status
            not in {"filled", "canceled", "expired"}
            or fill_ticker != ticker
            or fill_side != side
        ):
            raise PaperHistoryAdoptionError(
                "paper_history_legacy_invalid"
            )
        qty = _decimal(row["qty"])
        notional = _decimal(row["notional"], optional=True)
        limit_price = _decimal(row["limit_price"], optional=True)
        fill_qty = _decimal(row["fill_qty"])
        fill_price = _decimal(row["fill_price"])
        created_at = _utc(row["created_at"])
        updated_at = _utc(row["updated_at"])
        submission_started_at = _utc(
            row["submission_started_at"]
        )
        filled_at = _utc(row["filled_at"])
        if (
            qty is None
            or notional is not None
            or not _valid_order_economic(qty)
            or fill_qty is None
            or not valid_fill_economic(fill_qty)
            or fill_price is None
            or not valid_fill_economic(fill_price)
            or (order_type == "market" and limit_price is not None)
            or (order_type == "limit" and limit_price is None)
            or (
                limit_price is not None
                and not _valid_order_economic(limit_price)
            )
            or not (
                created_at
                <= submission_started_at
                <= filled_at
                <= updated_at
            )
        ):
            raise PaperHistoryAdoptionError(
                "paper_history_legacy_invalid"
            )
        order = _LegacyOrder(
            idempotency_key=idempotency_key,
            ticker=ticker,
            side=side,
            order_type=order_type,
            qty=qty,
            notional=None,
            limit_price=limit_price,
            broker_order_id=broker_order_id,
            status=order_status,
            submission_started_at=submission_started_at,
            created_at=created_at,
            updated_at=updated_at,
        )
        prior = orders.setdefault(broker_order_id, order)
        if prior != order:
            raise PaperHistoryAdoptionError(
                "paper_history_legacy_invalid"
            )
        fills.append(
            _LegacyFill(
                broker_fill_id=broker_fill_id,
                broker_order_id=broker_order_id,
                ticker=fill_ticker,
                side=fill_side,
                qty=fill_qty,
                price=fill_price,
                filled_at=filled_at,
            )
        )
        seen_fill_ids.add(broker_fill_id)
    return _LegacyHistory(
        orders=tuple(
            sorted(orders.values(), key=lambda order: order.broker_order_id)
        ),
        fills=tuple(
            sorted(fills, key=lambda fill: fill.broker_fill_id)
        ),
    )


def _activity_tuple(
    activities: Iterable[BrokerFill],
) -> tuple[BrokerFill, ...]:
    normalized = tuple(
        sorted(activities, key=lambda activity: activity.broker_fill_id)
    )
    fill_ids = [activity.broker_fill_id for activity in normalized]
    if (
        not normalized
        or len(fill_ids) != len(set(fill_ids))
        or any(
            not isinstance(activity.broker_fill_id, str)
            or not activity.broker_fill_id
            or activity.broker_fill_id
            != activity.broker_fill_id.strip()
            or not isinstance(activity.broker_order_id, str)
            or not activity.broker_order_id
            or activity.broker_order_id
            != activity.broker_order_id.strip()
            or not isinstance(activity.ticker, str)
            or not activity.ticker
            or activity.ticker != activity.ticker.strip()
            or activity.ticker != activity.ticker.upper()
            or activity.side not in {"buy", "sell"}
            or not valid_fill_economic(activity.qty)
            or not valid_fill_economic(activity.price)
            or not isinstance(activity.filled_at, datetime)
            or activity.filled_at.tzinfo is None
            for activity in normalized
        )
    ):
        raise PaperHistoryAdoptionError(
            "paper_history_broker_invalid"
        )
    return normalized


def _account_is_flat(broker) -> bool:
    positions = list(broker.get_positions())
    open_orders = list(broker.get_open_orders())
    return not positions and not open_orders


def _current_database_is_fresh(
    session: Session,
) -> bool:
    allowed_nonempty = {
        AuditEvent,
        CircuitBreakerState,
        RuntimeTenure,
        SensitiveMigrationState,
    }
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if model in allowed_nonempty:
            continue
        if (
            session.scalar(
                select(func.count()).select_from(model)
            )
            != 0
        ):
            return False
    return True


def _broker_drift_breaker_generation(
    session: Session,
    *,
    expected_generation: int | None = None,
) -> int:
    scope = BreakerScope.broker_drift()
    row = session.get(CircuitBreakerState, scope.key)
    valid = bool(
        row is not None
        and row.scope_key == scope.key
        and row.kind == BreakerKind.BROKER_DRIFT.value
        and row.target == ""
        and row.tripped is True
        and isinstance(row.generation, int)
        and not isinstance(row.generation, bool)
        and row.generation > 0
    )
    if not valid:
        code = (
            "paper_history_broker_drift_breaker_required"
            if expected_generation is None
            else "paper_history_broker_drift_breaker_changed"
        )
        raise PaperHistoryAdoptionError(code)
    assert row is not None
    if (
        expected_generation is not None
        and row.generation != expected_generation
    ):
        raise PaperHistoryAdoptionError(
            "paper_history_broker_drift_breaker_changed"
        )
    return row.generation


def _validate_target_not_source(
    session_factory: sessionmaker[Session],
    source: Path,
) -> None:
    engine = session_factory.kw.get("bind")
    if not isinstance(engine, Engine):
        raise PaperHistoryAdoptionError(
            "paper_history_target_invalid"
        )
    target_name = engine.url.database
    if not target_name:
        raise PaperHistoryAdoptionError(
            "paper_history_target_invalid"
        )
    target = Path(target_name)
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    else:
        target = target.resolve()
    try:
        aliases = target == source or (
            target.exists() and os.path.samefile(target, source)
        )
    except OSError:
        raise PaperHistoryAdoptionError(
            "paper_history_target_invalid"
        ) from None
    if aliases:
        raise PaperHistoryAdoptionError(
            "paper_history_source_is_target"
        )


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _history_digest(history: _LegacyHistory) -> str:
    return _canonical_digest(
        {
            "schema": "paper-history-v1",
            "orders": [
                {
                    "broker_order_id": order.broker_order_id,
                    "created_at": _timestamp_text(order.created_at),
                    "idempotency_key": order.idempotency_key,
                    "limit_price": _decimal_text(order.limit_price),
                    "order_type": order.order_type,
                    "qty": _decimal_text(order.qty),
                    "side": order.side,
                    "status": order.status,
                    "submission_started_at": _timestamp_text(
                        order.submission_started_at
                    ),
                    "ticker": order.ticker,
                    "updated_at": _timestamp_text(order.updated_at),
                }
                for order in history.orders
            ],
            "fills": [
                {
                    "broker_fill_id": fill.broker_fill_id,
                    "broker_order_id": fill.broker_order_id,
                    "filled_at": _timestamp_text(fill.filled_at),
                    "price": _decimal_text(fill.price),
                    "qty": _decimal_text(fill.qty),
                    "side": fill.side,
                    "ticker": fill.ticker,
                }
                for fill in history.fills
            ],
        }
    )


def _broker_digest(
    activities: tuple[BrokerFill, ...],
    remote_orders: dict[str, OrderResult],
) -> str:
    return _canonical_digest(
        {
            "schema": "paper-broker-proof-v1",
            "fills": [
                {
                    "broker_fill_id": activity.broker_fill_id,
                    "broker_order_id": activity.broker_order_id,
                    "filled_at": _timestamp_text(activity.filled_at),
                    "price": _decimal_text(activity.price),
                    "qty": _decimal_text(activity.qty),
                    "side": activity.side,
                    "ticker": activity.ticker,
                }
                for activity in activities
            ],
            "orders": [
                {
                    "avg_fill_price": _decimal_text(
                        remote.avg_fill_price
                    ),
                    "broker_order_id": remote.broker_order_id,
                    "filled_qty": _decimal_text(remote.filled_qty),
                    "idempotency_key": remote.idempotency_key,
                    "limit_price": _decimal_text(remote.limit_price),
                    "order_type": (
                        remote.order_type.value
                        if remote.order_type is not None
                        else None
                    ),
                    "requested_notional": _decimal_text(
                        remote.requested_notional
                    ),
                    "requested_qty": _decimal_text(
                        remote.requested_qty
                    ),
                    "side": (
                        remote.side.value
                        if remote.side is not None
                        else None
                    ),
                    "status": remote.status.value,
                    "submitted_at": _timestamp_text(
                        remote.submitted_at
                    ),
                    "ticker": remote.ticker,
                }
                for _, remote in sorted(remote_orders.items())
            ],
        }
    )


def _persisted_import_matches(
    session: Session,
    history: _LegacyHistory,
) -> bool:
    session.flush()
    session.expire_all()
    orders = list(
        session.scalars(
            select(Order).order_by(Order.broker_order_id)
        )
    )
    fills = list(
        session.scalars(
            select(Fill).order_by(Fill.broker_fill_id)
        )
    )
    if (
        len(orders) != len(history.orders)
        or len(fills) != len(history.fills)
    ):
        return False
    legacy_orders = {
        order.broker_order_id: order for order in history.orders
    }
    persisted_order_ids: dict[str, int] = {}
    for order in orders:
        legacy = legacy_orders.get(order.broker_order_id or "")
        if (
            legacy is None
            or order.id is None
            or order.idempotency_key != legacy.idempotency_key
            or order.ticker != legacy.ticker
            or order.side != legacy.side
            or order.order_type != legacy.order_type
            or order.qty != legacy.qty
            or order.notional is not None
            or order.limit_price != legacy.limit_price
            or order.status != legacy.status
            or order.approval_actor
            != "history_import:broker_truth"
            or order.approved_at is not None
            or order.submission_kind != "history_import"
            or order.submission_attempt != 0
            or order.submission_started_at
            != legacy.submission_started_at
            or order.acceptance_state != legacy.status
        ):
            return False
        persisted_order_ids[legacy.broker_order_id] = order.id
    legacy_fills = {
        fill.broker_fill_id: fill for fill in history.fills
    }
    for fill in fills:
        if fill.broker_fill_id is None:
            return False
        legacy = legacy_fills.get(fill.broker_fill_id)
        if (
            legacy is None
            or fill.order_id
            != persisted_order_ids.get(legacy.broker_order_id)
            or fill.ticker != legacy.ticker
            or fill.side != legacy.side
            or fill.qty != legacy.qty
            or fill.price != legacy.price
            or fill.filled_at != legacy.filled_at
            or fill.reconciliation_state
            != FILL_RECONCILIATION_TRUSTED
        ):
            return False
    return True


def _committed_import_is_durable(
    session_factory: sessionmaker[Session],
    *,
    history: _LegacyHistory,
    receipt: PaperHistoryAdoptionReceipt,
    request_id: str,
    broker_key: str,
) -> bool:
    try:
        with session_factory() as session:
            audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "paper_history.adopt",
                        AuditEvent.request_id == request_id,
                        AuditEvent.target_type == "broker",
                        AuditEvent.target_id == broker_key,
                        AuditEvent.result_code == "imported",
                    )
                )
            )
            if len(audits) != 1:
                return False
            detail = json.loads(
                sensitive_store(session).read(
                    audits[0],
                    "detail_json",
                )
            )
            if (
                not isinstance(detail, dict)
                or detail.get("import_digest")
                != receipt.import_digest
                or detail.get("breaker_generation")
                != receipt.breaker_generation
                or detail.get("orders_imported")
                != receipt.orders_imported
                or detail.get("fills_imported")
                != receipt.fills_imported
            ):
                return False
            _broker_drift_breaker_generation(
                session,
                expected_generation=receipt.breaker_generation,
            )
            return _persisted_import_matches(session, history)
    except Exception:
        return False


def _legacy_matches_broker(
    history: _LegacyHistory,
    activities: tuple[BrokerFill, ...],
    remote_orders: dict[str, OrderResult],
    *,
    observed_at: datetime,
) -> bool:
    legacy_by_fill = {
        fill.broker_fill_id: fill for fill in history.fills
    }
    if set(legacy_by_fill) != {
        activity.broker_fill_id for activity in activities
    }:
        return False
    net: dict[str, Decimal] = {}
    quantity_by_order: dict[str, Decimal] = {}
    notional_by_order: dict[str, Decimal] = {}
    fills_by_order: dict[str, list[_LegacyFill]] = {}
    order_by_id = {
        order.broker_order_id: order for order in history.orders
    }
    for activity in activities:
        legacy = legacy_by_fill[activity.broker_fill_id]
        normalized_time = activity.filled_at.astimezone(timezone.utc)
        if (
            legacy.broker_order_id != activity.broker_order_id
            or legacy.ticker != activity.ticker.upper()
            or legacy.side != activity.side
            or legacy.qty != activity.qty
            or legacy.price != activity.price
            or legacy.filled_at != normalized_time
            or normalized_time > observed_at
        ):
            return False
        signed = activity.qty if activity.side == "buy" else -activity.qty
        net[activity.ticker.upper()] = (
            net.get(activity.ticker.upper(), Decimal(0)) + signed
        )
        quantity_by_order[activity.broker_order_id] = (
            quantity_by_order.get(
                activity.broker_order_id,
                Decimal(0),
            )
            + activity.qty
        )
        notional_by_order[activity.broker_order_id] = (
            notional_by_order.get(
                activity.broker_order_id,
                Decimal(0),
            )
            + activity.qty * activity.price
        )
        fills_by_order.setdefault(
            activity.broker_order_id,
            [],
        ).append(legacy)
    if any(quantity != 0 for quantity in net.values()):
        return False
    if set(remote_orders) != set(order_by_id):
        return False
    for broker_order_id, remote in remote_orders.items():
        legacy = order_by_id[broker_order_id]
        order_fills = fills_by_order[broker_order_id]
        first_fill_at = min(fill.filled_at for fill in order_fills)
        last_fill_at = max(fill.filled_at for fill in order_fills)
        submitted_at = remote.submitted_at
        average_fill_price = (
            notional_by_order[broker_order_id]
            / quantity_by_order[broker_order_id]
        )
        quantity_relation = fill_quantity_relation(
            quantity_by_order[broker_order_id],
            legacy.qty,
        )
        expected_status = OrderStatus(legacy.status)
        if (
            remote.broker_order_id != broker_order_id
            or remote.idempotency_key != legacy.idempotency_key
            or remote.status is not expected_status
            or not valid_cumulative_filled_qty(remote.filled_qty)
            or remote.filled_qty != quantity_by_order[broker_order_id]
            or quantity_relation is None
            or quantity_relation is FillQuantityRelation.AHEAD
            or (
                expected_status is OrderStatus.FILLED
                and quantity_relation is not FillQuantityRelation.EXACT
            )
            or remote.ticker is None
            or remote.ticker != legacy.ticker
            or remote.side is not OrderSide(legacy.side)
            or remote.order_type is not BrokerOrderType(
                legacy.order_type
            )
            or remote.requested_qty != legacy.qty
            or remote.requested_notional is not None
            or remote.limit_price != legacy.limit_price
            or not valid_fill_economic(remote.avg_fill_price)
            or abs(remote.avg_fill_price - average_fill_price)
            > FILL_ECONOMIC_QUANTUM / 2
            or not isinstance(submitted_at, datetime)
            or submitted_at.tzinfo is None
            or not (
                legacy.created_at
                <= submitted_at.astimezone(timezone.utc)
                <= first_fill_at
                <= last_fill_at
                <= legacy.updated_at
                <= observed_at
            )
            or abs(
                submitted_at.astimezone(timezone.utc)
                - legacy.submission_started_at
            )
            > timedelta(minutes=5)
        ):
            return False
    return True


def adopt_flat_paper_history(
    session_factory: sessionmaker[Session],
    broker,
    legacy_database: Path,
    *,
    actor: str,
    reason: str,
    request_id: str,
    observed_at: datetime | None = None,
    source_quiescence_checker: Callable[[Path], bool],
    maintenance_guard,
) -> PaperHistoryAdoptionReceipt:
    """Import exact terminal history while preserving every active breaker."""

    actor = actor.strip()
    reason = reason.strip()
    request_id = request_id.strip()
    observed = observed_at or datetime.now(timezone.utc)
    if (
        not actor
        or not reason
        or not request_id
        or observed.tzinfo is None
        or not callable(source_quiescence_checker)
        or not callable(
            getattr(
                maintenance_guard,
                "exclusive_transaction_renewal",
                None,
            )
        )
        or not callable(
            getattr(
                maintenance_guard,
                "renew_in_transaction",
                None,
            )
        )
    ):
        raise PaperHistoryAdoptionError(
            "paper_history_context_invalid"
        )
    observed = observed.astimezone(timezone.utc)
    source = _safe_source(Path(legacy_database))
    _validate_target_not_source(session_factory, source)
    try:
        source_quiescent = source_quiescence_checker(source)
    except Exception:
        source_quiescent = False
    if source_quiescent is not True:
        raise PaperHistoryAdoptionError(
            "paper_history_source_quiescence_unproven"
        )

    with session_factory() as session:
        if not _current_database_is_fresh(session):
            raise PaperHistoryAdoptionError(
                "paper_history_target_not_fresh"
            )
        breaker_generation = _broker_drift_breaker_generation(
            session
        )

    before_fingerprint = _source_fingerprint(source)
    history = _load_legacy_history(source)
    history_digest = _history_digest(history)

    try:
        if not _account_is_flat(broker):
            raise PaperHistoryAdoptionError(
                "paper_history_account_not_flat"
            )
        first_activities = _activity_tuple(
            broker.get_fill_activities(after=None)
        )
        remote_orders = {
            broker_order_id: broker.get_order_status(
                broker_order_id
            )
            for broker_order_id in sorted(
                {
                    activity.broker_order_id
                    for activity in first_activities
                }
            )
        }
        second_activities = _activity_tuple(
            broker.get_fill_activities(after=None)
        )
        if not _account_is_flat(broker):
            raise PaperHistoryAdoptionError(
                "paper_history_account_not_flat"
            )
    except PaperHistoryAdoptionError:
        raise
    except Exception:
        raise PaperHistoryAdoptionError(
            "paper_history_broker_unavailable"
        ) from None
    if first_activities != second_activities:
        raise PaperHistoryAdoptionError(
            "paper_history_broker_changed"
        )
    try:
        source_quiescent = source_quiescence_checker(source)
    except Exception:
        source_quiescent = False
    if source_quiescent is not True:
        raise PaperHistoryAdoptionError(
            "paper_history_source_quiescence_unproven"
        )
    if before_fingerprint != _source_fingerprint(source):
        raise PaperHistoryAdoptionError(
            "paper_history_source_changed"
        )
    if not _legacy_matches_broker(
        history,
        first_activities,
        remote_orders,
        observed_at=observed,
    ):
        raise PaperHistoryAdoptionError(
            "paper_history_broker_mismatch"
        )
    broker_digest = _broker_digest(
        first_activities,
        remote_orders,
    )
    import_digest = _canonical_digest(
        {
            "breaker_generation": breaker_generation,
            "broker_digest": broker_digest,
            "history_digest": history_digest,
            "schema": "paper-history-import-v1",
            "source_fingerprint": before_fingerprint,
        }
    )
    receipt = PaperHistoryAdoptionReceipt(
        orders_imported=len(history.orders),
        fills_imported=len(history.fills),
        breaker_generation=breaker_generation,
        source_fingerprint=before_fingerprint,
        history_digest=history_digest,
        broker_digest=broker_digest,
        import_digest=import_digest,
    )

    order_ids: dict[str, int] = {}
    commit_attempted = False
    try:
        with maintenance_guard.exclusive_transaction_renewal():
            with session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                connection = session.connection()
                last_tenure_renewal = monotonic()

                def renew_tenure(*, force: bool = False) -> None:
                    nonlocal last_tenure_renewal
                    now_monotonic = monotonic()
                    if (
                        force
                        or now_monotonic - last_tenure_renewal >= 1
                    ):
                        maintenance_guard.renew_in_transaction(
                            connection
                        )
                        last_tenure_renewal = now_monotonic

                renew_tenure(force=True)
                if not _current_database_is_fresh(session):
                    raise PaperHistoryAdoptionError(
                        "paper_history_target_not_fresh"
                    )
                _broker_drift_breaker_generation(
                    session,
                    expected_generation=breaker_generation,
                )
                if (
                    before_fingerprint
                    != _source_fingerprint(source)
                ):
                    raise PaperHistoryAdoptionError(
                        "paper_history_source_changed"
                    )
                renew_tenure(force=True)
                for legacy in history.orders:
                    renew_tenure()
                    order = Order(
                        idempotency_key=legacy.idempotency_key,
                        ticker=legacy.ticker,
                        side=legacy.side,
                        order_type=legacy.order_type,
                        qty=legacy.qty,
                        notional=None,
                        limit_price=legacy.limit_price,
                        status=legacy.status,
                        broker_order_id=legacy.broker_order_id,
                        approval_actor=(
                            "history_import:broker_truth"
                        ),
                        approved_at=None,
                        submission_kind="history_import",
                        submission_payload_json=json.dumps(
                            {
                                "original_approval": "unavailable",
                                "provenance": (
                                    "broker_history_import"
                                ),
                                "schema": 1,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        submission_attempt=0,
                        submission_started_at=(
                            legacy.submission_started_at
                        ),
                        acceptance_state=legacy.status,
                        last_reconciled_at=observed,
                        last_error_code="",
                        plan_cancel_state="none",
                        version=0,
                        created_at=observed,
                        updated_at=observed,
                    )
                    persist_sensitive(
                        session,
                        order,
                        {
                            "approval_reason": (
                                "Imported terminal broker history; "
                                "original approval provenance is "
                                "unavailable. Import authorized by "
                                f"{actor}: {reason}"
                            )
                        },
                        session_factory=session_factory,
                    )
                    session.flush()
                    order_ids[legacy.broker_order_id] = order.id
                for legacy in history.fills:
                    renew_tenure()
                    session.add(
                        Fill(
                            order_id=order_ids[
                                legacy.broker_order_id
                            ],
                            ticker=legacy.ticker,
                            side=legacy.side,
                            qty=legacy.qty,
                            price=legacy.price,
                            broker_fill_id=legacy.broker_fill_id,
                            reconciliation_state=(
                                FILL_RECONCILIATION_TRUSTED
                            ),
                            filled_at=legacy.filled_at,
                        )
                    )
                renew_tenure(force=True)
                if not _persisted_import_matches(session, history):
                    raise PaperHistoryAdoptionError(
                        "paper_history_persisted_mismatch"
                    )
                persist_sensitive(
                    session,
                    AuditEvent(
                        actor=actor,
                        action="paper_history.adopt",
                        target_type="broker",
                        target_id=str(broker.reconciliation_key),
                        request_id=request_id,
                        result_code="imported",
                    ),
                    {
                        "reason": reason,
                        "detail_json": json.dumps(
                            {
                                "breaker_generation": (
                                    breaker_generation
                                ),
                                "broker_digest": broker_digest,
                                "fills_imported": len(
                                    history.fills
                                ),
                                "history_digest": history_digest,
                                "import_digest": import_digest,
                                "orders_imported": len(
                                    history.orders
                                ),
                                "source_fingerprint": (
                                    before_fingerprint
                                ),
                            },
                            sort_keys=True,
                        ),
                    },
                    session_factory=session_factory,
                )
                renew_tenure(force=True)
                commit_attempted = True
                session.commit()
    except PaperHistoryAdoptionError:
        raise
    except Exception:
        if commit_attempted:
            if _committed_import_is_durable(
                session_factory,
                history=history,
                receipt=receipt,
                request_id=request_id,
                broker_key=str(broker.reconciliation_key),
            ):
                return replace(
                    receipt,
                    commit_reconciled_after_error=True,
                )
            raise PaperHistoryCommitUncertain(receipt) from None
        raise PaperHistoryAdoptionError(
            "paper_history_import_failed"
        ) from None
    return receipt


def _receipt_payload(
    receipt: PaperHistoryAdoptionReceipt,
    *,
    status: str,
) -> dict[str, object]:
    return {
        "breaker_generation": receipt.breaker_generation,
        "broker_digest": receipt.broker_digest,
        "commit_reconciled_after_error": (
            receipt.commit_reconciled_after_error
        ),
        "fills_imported": receipt.fills_imported,
        "history_digest": receipt.history_digest,
        "import_digest": receipt.import_digest,
        "orders_imported": receipt.orders_imported,
        "source_fingerprint": receipt.source_fingerprint,
        "status": status,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-database",
        type=Path,
        required=True,
    )
    args = parser.parse_args(argv)
    guard = None
    runtime = None
    committed_receipt: PaperHistoryAdoptionReceipt | None = None
    committed_status = "imported"
    committed_exit_code = 0
    try:
        from ..bootstrap import (
            acquire_maintenance_guard,
            prepare_database_runtime,
        )
        from ..broker.alpaca import AlpacaBroker
        from ..logging import runtime_startup
        from ..security.crypto import build_sensitive_data_cipher
        from ..security.secrets import (
            MacOSKeychainSecretProvider,
            load_role_secrets,
            secret_value,
        )
        from ..security.sensitive_fields import bind_sensitive_cipher

        config = load_config()
        if (
            config.trading.mode is not TradingMode.PAPER
            or config.trading.broker is not BrokerKind.ALPACA
        ):
            raise PaperHistoryAdoptionError(
                "paper_history_paper_configuration_required"
            )
        secrets = load_role_secrets(
            "paper-drill",
            config=config,
            provider=MacOSKeychainSecretProvider(),
        )
        with runtime_startup("paper-drill", secrets):
            runtime = prepare_database_runtime(secrets)
            bind_sensitive_cipher(
                runtime.session_factory,
                build_sensitive_data_cipher(
                    config.encryption,
                    secrets,
                ),
            )
            guard = acquire_maintenance_guard(runtime)
            broker = AlpacaBroker.from_credentials(
                secret_value(secrets.alpaca_api_key),
                secret_value(secrets.alpaca_secret_key),
                paper=True,
                runtime_role="paper-drill",
            )
            broker.arm_paper_only_mutations()
            broker.validate_armed_paper_target()
            receipt = adopt_flat_paper_history(
                runtime.session_factory,
                broker,
                args.legacy_database,
                actor="operator:paper-history-adoption",
                reason=(
                    "operator-authorized import of exact flat Alpaca "
                    "paper history"
                ),
                request_id=uuid4().hex,
                source_quiescence_checker=_source_is_quiescent,
                maintenance_guard=guard,
            )
            committed_receipt = receipt
            if receipt.commit_reconciled_after_error:
                committed_status = "imported_commit_reconciled"
            try:
                released = guard.close()
            except Exception:
                released = False
            guard = None
            if not released:
                committed_status = (
                    "committed_tenure_release_uncertain"
                )
                committed_exit_code = 2
        print(
            json.dumps(
                _receipt_payload(
                    receipt,
                    status=committed_status,
                ),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        if committed_exit_code:
            print(
                "paper_history_committed_tenure_release_uncertain",
                file=sys.stderr,
            )
        return committed_exit_code
    except PaperHistoryCommitUncertain as exc:
        print(
            json.dumps(
                _receipt_payload(
                    exc.receipt,
                    status="commit_outcome_unknown",
                ),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        print(exc.stable_code, file=sys.stderr)
        return 2
    except PaperHistoryAdoptionError as exc:
        if committed_receipt is not None:
            print(
                json.dumps(
                    _receipt_payload(
                        committed_receipt,
                        status="committed_cleanup_uncertain",
                    ),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            print(
                "paper_history_committed_cleanup_uncertain",
                file=sys.stderr,
            )
            return 2
        print(exc.stable_code, file=sys.stderr)
        return 1
    except Exception:
        if committed_receipt is not None:
            print(
                json.dumps(
                    _receipt_payload(
                        committed_receipt,
                        status="committed_cleanup_uncertain",
                    ),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            print(
                "paper_history_committed_cleanup_uncertain",
                file=sys.stderr,
            )
            return 2
        print("paper_history_adoption_failed", file=sys.stderr)
        return 1
    finally:
        if guard is not None:
            try:
                guard.close()
            except Exception:
                pass
        if runtime is not None:
            try:
                runtime.engine.dispose()
            except Exception:
                print(
                    "paper_history_engine_dispose_uncertain",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    raise SystemExit(main())
