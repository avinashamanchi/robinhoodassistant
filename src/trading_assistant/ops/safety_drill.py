"""Exercise the production safety barriers on an explicit SQLite copy."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import make_url

from ..app.auth import InvalidSession
from ..assets import AssetClass
from ..bootstrap import build_test_container
from ..broker.base import BrokerClient
from ..broker.models import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderTimeInForce,
    normalize_fill_economic,
    order_result_identity_error,
    valid_cumulative_filled_qty,
)
from ..config import (
    AppConfig,
    BrokerKind,
    Secrets,
    TradingMode,
    load_config,
)
from ..db.migrate import upgrade
from ..db.models import CircuitBreakerState, Order, Rule, RuleGroup
from ..db.schema import schema_status
from ..db.session import create_db_engine
from ..risk.breakers import BreakerScope
from ..risk.clock import FakeClock
from ..rules.repository import RuleRepository


_OCO_REPOSITORY_TIMEOUT_SECONDS = 2.0
_OCO_JOIN_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class SafetyDrillReport:
    schema_current: bool
    auth_fail_closed: bool
    crash_recovered_without_duplicate: bool
    oco_single_terminal: bool
    breakers_persisted: bool
    reconciliation_clean: bool
    safe: bool
    details: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)


class SafetyDrillError(RuntimeError):
    """Stable refusal that never includes a provider exception or secret."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _DrillCrash(BaseException):
    """Deliberate process-death boundary after one accepted submission."""


class _CrashAfterAcceptanceOnceBroker(BrokerClient):
    """Crash once after delegation accepts, before local result persistence."""

    def __init__(self, broker: BrokerClient) -> None:
        self._broker = broker
        self.reconciliation_key = broker.reconciliation_key
        self.submit_calls = 0
        self._lose_next_acceptance = True

    def get_quote(self, ticker: str):
        return self._broker.get_quote(ticker)

    def get_account(self):
        return self._broker.get_account()

    def get_positions(self):
        return self._broker.get_positions()

    def get_fill_activities(self, after=None):
        reader = getattr(self._broker, "get_fill_activities", None)
        return [] if reader is None else reader(after)

    def submit_order(self, order: OrderRequest) -> OrderResult:
        self.submit_calls += 1
        result = self._broker.submit_order(order)
        if self._lose_next_acceptance:
            self._lose_next_acceptance = False
            raise _DrillCrash
        return result

    def get_order_by_client_id(self, client_order_id: str):
        return self._broker.get_order_by_client_id(client_order_id)

    def get_open_orders(self):
        return self._broker.get_open_orders()

    def get_order_status(self, order_id: str):
        return self._broker.get_order_status(order_id)

    def cancel_order(self, order_id: str):
        return self._broker.cancel_order(order_id)


class _BoundedWriterBarrier:
    """Drill-only bounded form of the production repository writer barrier."""

    def __init__(self, barrier, *, timeout_seconds: float) -> None:
        self._paths = (
            (barrier.writer_gate_path, fcntl.LOCK_SH),
            (barrier.intent_path, fcntl.LOCK_SH),
            (barrier.path, fcntl.LOCK_EX),
        )
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _open(path: Path) -> int:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        return descriptor

    @contextmanager
    def hold_writer(self):
        deadline = time.monotonic() + self._timeout_seconds
        locked: list[int] = []
        try:
            for path, operation in self._paths:
                descriptor = self._open(path)
                while True:
                    try:
                        fcntl.flock(
                            descriptor,
                            operation | fcntl.LOCK_NB,
                        )
                        locked.append(descriptor)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            os.close(descriptor)
                            raise TimeoutError(
                                "bounded OCO repository writer timeout"
                            ) from None
                        time.sleep(min(0.01, remaining))
            yield
        finally:
            for descriptor in reversed(locked):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)


def _bounded_rule_repository(session_factory, *, owner: str) -> RuleRepository:
    repository = RuleRepository(session_factory, owner=owner)
    repository.submission_barrier = _BoundedWriterBarrier(
        repository.submission_barrier,
        timeout_seconds=_OCO_REPOSITORY_TIMEOUT_SECONDS,
    )
    return repository


def _database_source(database_url: str) -> Path:
    try:
        url = make_url(database_url)
    except Exception:
        raise SafetyDrillError("unsafe_primary_database") from None
    if (
        url.get_backend_name() != "sqlite"
        or not url.database
        or url.database == ":memory:"
    ):
        raise SafetyDrillError("unsafe_primary_database")
    source = Path(url.database).expanduser().resolve()
    if not source.is_file():
        raise SafetyDrillError("unsafe_primary_database")
    try:
        with source.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        raise SafetyDrillError("unsafe_primary_database") from None
    if header != b"SQLite format 3\x00":
        raise SafetyDrillError("invalid_primary_database")
    return source


def _online_copy(source: Path, destination: Path) -> Path:
    if (
        not destination.is_absolute()
        or not destination.name
        or any(part in {".", ".."} for part in destination.parts)
    ):
        raise SafetyDrillError("unsafe_database_copy")
    if destination == source:
        raise SafetyDrillError("unsafe_database_copy")

    def source_fingerprint() -> tuple[tuple[str, tuple[Any, ...]], ...]:
        files: list[tuple[str, tuple[Any, ...]]] = []
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = source.with_name(f"{source.name}{suffix}")
            try:
                metadata = os.lstat(candidate)
                if not stat.S_ISREG(metadata.st_mode):
                    raise SafetyDrillError("database_copy_failed")
                identity = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_nlink,
                )
                if suffix == "-shm":
                    # WAL readers may update ephemeral read marks in SHM.
                    # Its regular-file identity and presence must remain fixed,
                    # but SHM bytes/mtime are not logical database state.
                    files.append((suffix, identity))
                else:
                    digest = hashlib.sha256()
                    with candidate.open("rb") as handle:
                        for chunk in iter(
                            lambda: handle.read(1024 * 1024),
                            b"",
                        ):
                            digest.update(chunk)
                    files.append(
                        (
                            suffix,
                            identity
                            + (
                                metadata.st_size,
                                metadata.st_mtime_ns,
                                digest.digest(),
                            ),
                        )
                    )
            except FileNotFoundError:
                files.append((suffix, ()))
        return tuple(files)

    source_before = source_fingerprint()
    source_files = dict(source_before)
    try:
        with source.open("rb") as source_handle:
            source_handle.seek(18)
            journal_versions = source_handle.read(2)
    except OSError:
        raise SafetyDrillError("database_copy_failed") from None
    wal_format = journal_versions == b"\x02\x02"
    if (
        source_files["-wal"]
        and not source_files["-shm"]
    ) or (
        wal_format
        and (
            not source_files["-wal"]
            or not source_files["-shm"]
        )
    ):
        # A normal read-only WAL connection may use an existing SHM but must
        # never create WAL/SHM recovery or coordination state beside the
        # primary. A fully closed WAL database therefore fails before connect.
        raise SafetyDrillError("database_copy_failed")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open("/", directory_flags)
    temporary_name = f".{destination.name}.{uuid4().hex}.tmp"
    temporary_fd: int | None = None
    linked = False
    published = False
    try:
        for component in destination.parent.parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    directory_flags | nofollow,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(
                        component,
                        directory_flags | nofollow,
                        dir_fd=parent_fd,
                    )
                except OSError:
                    raise SafetyDrillError(
                        "unsafe_database_copy"
                    ) from None
                if stat.S_IMODE(os.fstat(next_fd).st_mode) != 0o700:
                    os.close(next_fd)
                    raise SafetyDrillError("unsafe_database_copy")
            except OSError:
                raise SafetyDrillError("unsafe_database_copy") from None
            os.close(parent_fd)
            parent_fd = next_fd

        parent_stat = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or stat.S_IMODE(parent_stat.st_mode) & 0o022
        ):
            raise SafetyDrillError("unsafe_database_copy")
        try:
            os.stat(
                destination.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise SafetyDrillError("unsafe_database_copy") from None

        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError:
            raise SafetyDrillError("unsafe_database_copy") from None
        expected = os.fstat(temporary_fd)
        if (
            not stat.S_ISREG(expected.st_mode)
            or stat.S_IMODE(expected.st_mode) != 0o600
            or expected.st_nlink != 1
        ):
            raise SafetyDrillError("unsafe_database_copy")

        temporary = destination.with_name(temporary_name)
        source_uri = f"file:{quote(str(source), safe='/')}?mode=ro"
        temporary_uri = (
            f"file:{quote(str(temporary), safe='/')}?mode=rw&nofollow=1"
        )
        try:
            before_connect = os.stat(
                temporary_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before_connect.st_mode)
                or (before_connect.st_dev, before_connect.st_ino)
                != (expected.st_dev, expected.st_ino)
                or before_connect.st_nlink != 1
            ):
                raise SafetyDrillError("unsafe_database_copy")
            with (
                sqlite3.connect(
                    source_uri,
                    uri=True,
                ) as source_connection,
                sqlite3.connect(temporary_uri, uri=True) as target_connection,
            ):
                opened = os.stat(
                    temporary_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (expected.st_dev, expected.st_ino)
                    or opened.st_nlink != 1
                ):
                    raise SafetyDrillError("unsafe_database_copy")
                source_connection.backup(target_connection)
                integrity = target_connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                if integrity != ("ok",):
                    raise SafetyDrillError("database_copy_failed")
                target_connection.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()
            if source_fingerprint() != source_before:
                raise SafetyDrillError("database_copy_failed")
        except SafetyDrillError:
            raise
        except Exception:
            raise SafetyDrillError("database_copy_failed") from None

        ready = os.stat(
            temporary_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(ready.st_mode)
            or stat.S_IMODE(ready.st_mode) != 0o600
            or (ready.st_dev, ready.st_ino)
            != (expected.st_dev, expected.st_ino)
            or ready.st_nlink != 1
        ):
            raise SafetyDrillError("unsafe_database_copy")
        try:
            # A same-directory hard link publishes atomically without ever
            # overwriting an operator-selected evidence destination.
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise SafetyDrillError("unsafe_database_copy") from None
        except OSError:
            raise SafetyDrillError("database_copy_failed") from None
        linked = True
        temporary_stat = os.stat(
            temporary_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        destination_stat = os.stat(
            destination.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(destination_stat.st_mode)
            or stat.S_IMODE(destination_stat.st_mode) != 0o600
            or (temporary_stat.st_dev, temporary_stat.st_ino)
            != (expected.st_dev, expected.st_ino)
            or (destination_stat.st_dev, destination_stat.st_ino)
            != (expected.st_dev, expected.st_ino)
            or temporary_stat.st_nlink != 2
            or destination_stat.st_nlink != 2
        ):
            raise SafetyDrillError("unsafe_database_copy")
        os.unlink(temporary_name, dir_fd=parent_fd)
        final = os.stat(
            destination.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(final.st_mode)
            or stat.S_IMODE(final.st_mode) != 0o600
            or (final.st_dev, final.st_ino)
            != (expected.st_dev, expected.st_ino)
            or final.st_nlink != 1
        ):
            raise SafetyDrillError("unsafe_database_copy")
        published = True
    finally:
        for name in (
            temporary_name,
            f"{temporary_name}-wal",
            f"{temporary_name}-shm",
            f"{temporary_name}-journal",
        ):
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if linked and not published:
            try:
                candidate = os.stat(
                    destination.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    temporary_fd is not None
                    and (candidate.st_dev, candidate.st_ino)
                    == (
                        os.fstat(temporary_fd).st_dev,
                        os.fstat(temporary_fd).st_ino,
                    )
                ):
                    os.unlink(destination.name, dir_fd=parent_fd)
            except (FileNotFoundError, OSError):
                pass
        if temporary_fd is not None:
            os.close(temporary_fd)
        os.close(parent_fd)
    if not published:
        raise SafetyDrillError("database_copy_failed")
    return destination


def _validate_safe_config(config: AppConfig) -> None:
    if (
        config.trading.mode is not TradingMode.PAPER
        or config.trading.broker is not BrokerKind.ALPACA
        or config.features.auto_execute_preapproved_rules
        or config.execution.prefer_bracket_orders
        or config.llm.fallback_provider is not None
    ):
        raise SafetyDrillError("unsafe_configuration")


def _validate_credentialed_paper(
    broker: BrokerClient,
    secrets: Secrets,
) -> None:
    from ..broker.alpaca import AlpacaBroker

    if not isinstance(broker, AlpacaBroker):
        raise SafetyDrillError("unsafe_configuration")
    target = getattr(broker, "execution_target", None)
    if target is None or target.is_official_paper is not True:
        raise SafetyDrillError("unsafe_configuration")
    if not (secrets.alpaca_api_key and secrets.alpaca_secret_key):
        raise SafetyDrillError("credentials_unavailable")
    if secrets.live_trading_confirm:
        raise SafetyDrillError("unsafe_configuration")
    endpoint = urlsplit(secrets.alpaca_paper_base_url)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "paper-api.alpaca.markets"
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port is not None
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise SafetyDrillError("unsafe_configuration")


def _open_order_manifest(broker: BrokerClient) -> frozenset[str]:
    manifest: set[str] = set()
    for order in broker.get_open_orders():
        if (
            not isinstance(order.broker_order_id, str)
            or not order.broker_order_id.strip()
        ):
            raise SafetyDrillError("paper_manifest_unconfirmed")
        manifest.add(order.broker_order_id)
    return frozenset(manifest)


def _position_manifest(broker: BrokerClient) -> dict[str, Decimal]:
    return {
        position.ticker.upper(): position.qty
        for position in broker.get_positions()
    }


def _position_delta(
    before: dict[str, Decimal],
    after: dict[str, Decimal],
) -> dict[str, Decimal]:
    return {
        symbol: delta
        for symbol in set(before) | set(after)
        if (
            delta := after.get(symbol, Decimal("0"))
            - before.get(symbol, Decimal("0"))
        )
        != 0
    }


def _derive_nonmarketable_buy_limit(
    *,
    last: Decimal,
    ask: Decimal,
    price_sanity_pct: float,
) -> Decimal | None:
    if (
        not isinstance(last, Decimal)
        or not last.is_finite()
        or last <= 0
        or not isinstance(ask, Decimal)
        or not ask.is_finite()
        or ask <= 0
    ):
        return None
    sanity = Decimal(str(price_sanity_pct))
    tick = Decimal("0.01")
    lower_bound = (
        last * (Decimal("100") - sanity) / Decimal("100")
    ).quantize(tick, rounding=ROUND_UP)
    preferred = (last * Decimal("0.96")).quantize(
        tick,
        rounding=ROUND_UP,
    )
    below_ask = (ask - tick).quantize(tick, rounding=ROUND_DOWN)
    candidate = max(lower_bound, min(preferred, below_ask))
    deviation = abs(candidate - last) / last * Decimal("100")
    if (
        candidate <= 0
        or candidate >= ask
        or deviation > sanity
    ):
        return None
    return candidate


def _rule_command(group_key: str, direction: str, price: str) -> dict[str, Any]:
    return {
        "ticker": "AAPL",
        "kind": "price",
        "condition": {
            "type": "price",
            "direction": direction,
            "price": price,
        },
        "action": {
            "side": "buy",
            "order_type": "limit",
            "qty": "0.010000",
            "limit_price": "96.000000",
        },
        "group_key": group_key,
        "pre_approved": False,
    }


def _reconcile_drill_orders(container, tag: str, stage: str) -> dict[str, Any]:
    return container.service.sync_open_orders(
        actor="operator:safety-drill",
        reason=f"safety drill {stage} order reconciliation",
        request_id=f"{tag}-{stage}-orders",
    )


def _attributed_signed_fill(
    broker: BrokerClient,
    result: OrderResult,
    *,
    client_order_id: str,
    symbol: str,
    expected_side: OrderSide,
) -> tuple[bool, Decimal]:
    if (
        order_result_identity_error(
            result,
            client_order_id,
            symbol,
        )
        is not None
        or not valid_cumulative_filled_qty(result.filled_qty)
    ):
        return False, Decimal("0")
    seen_fill_ids: set[str] = set()
    aggregate = Decimal("0")
    try:
        activities = broker.get_fill_activities()
    except Exception:
        return False, Decimal("0")
    for fill in activities:
        if fill.broker_order_id != result.broker_order_id:
            continue
        if (
            not isinstance(fill.broker_fill_id, str)
            or not fill.broker_fill_id.strip()
            or fill.broker_fill_id in seen_fill_ids
            or fill.ticker.upper() != symbol
            or fill.side != expected_side.value
            or normalize_fill_economic(fill.qty) is None
            or normalize_fill_economic(fill.price) is None
        ):
            return False, Decimal("0")
        seen_fill_ids.add(fill.broker_fill_id)
        aggregate += fill.qty
    if aggregate != result.filled_qty:
        return False, Decimal("0")
    signed = aggregate if expected_side is OrderSide.BUY else -aggregate
    return True, signed


def _resolve_tagged_terminal(
    container,
    *,
    client_order_id: str,
    symbol: str,
    tag: str,
    attempts: int = 5,
) -> OrderResult | None:
    """Bound broker reads and require identity-preserving terminal truth."""
    terminal = {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
    for attempt in range(attempts):
        by_client = container.broker.get_order_by_client_id(client_order_id)
        if (
            by_client is None
            or order_result_identity_error(
                by_client,
                client_order_id,
                symbol,
            )
            is not None
            or not isinstance(by_client.broker_order_id, str)
            or not by_client.broker_order_id.strip()
        ):
            return None
        observed = container.broker.get_order_status(
            by_client.broker_order_id
        )
        if (
            order_result_identity_error(
                observed,
                client_order_id,
                symbol,
            )
            is not None
            or observed.broker_order_id != by_client.broker_order_id
        ):
            return None
        sync = _reconcile_drill_orders(
            container,
            tag,
            f"terminal-read-{attempt}",
        )
        if sync["failed"] != 0:
            return None
        if observed.status in terminal:
            return observed
    return None


def _compensate_drill_fill(
    container,
    *,
    before_positions: dict[str, Decimal],
    tag: str,
    symbol: str,
) -> bool:
    """Flatten only exact, tagged broker fills with an exact manifest delta."""
    initial_client_id = f"{tag}-crash"
    initial = container.broker.get_order_by_client_id(initial_client_id)
    current_positions = _position_manifest(container.broker)
    if initial is None:
        return current_positions == before_positions
    attributed, signed_exposure = _attributed_signed_fill(
        container.broker,
        initial,
        client_order_id=initial_client_id,
        symbol=symbol,
        expected_side=OrderSide.BUY,
    )
    expected_delta = (
        {symbol: signed_exposure}
        if signed_exposure
        else {}
    )
    if (
        not attributed
        or _position_delta(before_positions, current_positions)
        != expected_delta
    ):
        return False
    if signed_exposure == 0:
        return True
    side = (
        OrderSide.SELL
        if signed_exposure > 0
        else OrderSide.BUY
    )
    compensation_qty = abs(signed_exposure)
    compensation_client_id = f"{tag}-compensate"
    proposal = container.service.propose_order(
        symbol,
        side.value,
        "market",
        qty=str(compensation_qty),
        idempotency_key=compensation_client_id,
        actor="operator:safety-drill",
        reason="compensate only the safety drill position delta",
        request_id=f"{tag}-compensate-propose",
    )
    if proposal["status"] != OrderStatus.PROPOSED.value:
        return False
    approved = container.service.approve_order(
        proposal["order_id"],
        actor="operator:safety-drill",
        reason="human safety drill compensation approval",
        request_id=f"{tag}-compensate-approve",
    )
    if approved["status"] in {
        OrderStatus.SUBMITTING.value,
        OrderStatus.ACCEPTANCE_UNKNOWN.value,
    }:
        container.reconciliation.reconcile_unknown(
            actor="operator:safety-drill",
            reason="resolve safety drill compensation by client identity",
            request_id=f"{tag}-compensate-resolve",
        )
    compensation = _resolve_tagged_terminal(
        container,
        client_order_id=compensation_client_id,
        symbol=symbol,
        tag=tag,
    )
    if compensation is None or compensation.status is not OrderStatus.FILLED:
        return False
    compensation_attributed, compensation_signed = _attributed_signed_fill(
        container.broker,
        compensation,
        client_order_id=compensation_client_id,
        symbol=symbol,
        expected_side=side,
    )
    if (
        not compensation_attributed
        or compensation_signed != -signed_exposure
        or compensation.filled_qty != compensation_qty
    ):
        return False
    final_sync = _reconcile_drill_orders(container, tag, "post-compensation")
    return (
        final_sync["failed"] == 0
        and _position_manifest(container.broker) == before_positions
    )


def _cancel_validated_tagged_open(
    container,
    *,
    tag: str,
    symbol: str,
) -> bool:
    confirmed = True
    tagged_open = tuple(
        order
        for order in container.broker.get_open_orders()
        if (
            isinstance(order.idempotency_key, str)
            and order.idempotency_key.startswith(f"{tag}-")
        )
    )
    for remote in tagged_open:
        try:
            if (
                not isinstance(remote.ticker, str)
                or not remote.ticker.strip()
                or order_result_identity_error(
                    remote,
                    remote.idempotency_key,
                    symbol,
                )
                is not None
            ):
                confirmed = False
                continue
            validated = container.broker.get_order_by_client_id(
                remote.idempotency_key,
            )
            if (
                validated is None
                or not isinstance(validated.ticker, str)
                or not validated.ticker.strip()
                or order_result_identity_error(
                    validated,
                    remote.idempotency_key,
                    symbol,
                )
                is not None
                or validated.broker_order_id != remote.broker_order_id
                or not isinstance(remote.broker_order_id, str)
                or not remote.broker_order_id.strip()
            ):
                confirmed = False
                continue
            canceled = container.broker.cancel_order(
                remote.broker_order_id
            )
            if (
                not isinstance(canceled.ticker, str)
                or not canceled.ticker.strip()
                or order_result_identity_error(
                    canceled,
                    remote.idempotency_key,
                    symbol,
                )
                is not None
                or canceled.broker_order_id != remote.broker_order_id
                or canceled.status is not OrderStatus.CANCELED
            ):
                confirmed = False
        except Exception:
            confirmed = False
    return confirmed


def _best_effort_cleanup(
    container,
    *,
    before_positions: dict[str, Decimal],
    tag: str,
    symbol: str,
) -> bool:
    """Cancel/flatten only tagged state; never expose a provider exception."""
    confirmed = True
    try:
        container.reconciliation.reconcile_unknown(
            actor="operator:safety-drill",
            reason="safety drill exception cleanup identity resolution",
            request_id=f"{tag}-cleanup-resolve",
        )
    except Exception:
        confirmed = False
    try:
        confirmed = (
            _cancel_validated_tagged_open(
                container,
                tag=tag,
                symbol=symbol,
            )
            and confirmed
        )
        order_sync = _reconcile_drill_orders(container, tag, "cleanup")
        confirmed = order_sync["failed"] == 0 and confirmed
    except Exception:
        confirmed = False
    compensated = False
    try:
        compensated = _compensate_drill_fill(
            container,
            before_positions=before_positions,
            tag=tag,
            symbol=symbol,
        )
    except Exception:
        compensated = False
    finally:
        try:
            remaining_canceled = _cancel_validated_tagged_open(
                container,
                tag=tag,
                symbol=symbol,
            )
            final_sync = _reconcile_drill_orders(
                container,
                tag,
                "cleanup-final",
            )
            confirmed = (
                confirmed
                and remaining_canceled
                and final_sync["failed"] == 0
            )
        except Exception:
            confirmed = False
    return compensated and confirmed


def run_safety_drill(
    *,
    database_copy: str | Path,
    config: AppConfig,
    broker: BrokerClient,
    credentialed_paper: bool = False,
    clock=None,
) -> SafetyDrillReport:
    """Copy the primary and derive every report gate from production behavior."""
    _validate_safe_config(config)
    primary_secrets = Secrets()
    if credentialed_paper:
        _validate_credentialed_paper(broker, primary_secrets)
        if clock is None:
            raise SafetyDrillError("unsafe_configuration")
    primary = _database_source(primary_secrets.database_url)
    copied = _online_copy(primary, Path(database_copy))
    copy_url = f"sqlite:///{copied}"
    copy_engine = create_db_engine(copy_url)
    try:
        upgrade(copy_engine)
        schema_current = schema_status(copy_engine).ready
    except Exception:
        raise SafetyDrillError("migration_failed") from None
    finally:
        copy_engine.dispose()

    drill_secrets = primary_secrets.model_copy(
        update={
            "database_url": copy_url,
            "app_api_token": (
                primary_secrets.app_api_token
                or "task-10-safety-drill-local-operator"
            ),
        }
    )
    crash_broker = _CrashAfterAcceptanceOnceBroker(broker)
    container = build_test_container(
        config,
        drill_secrets,
        broker=crash_broker,
        clock=clock or FakeClock(is_open=True),
    )
    tag = f"safety-drill-{uuid4().hex}"
    details: list[str] = [
        "mode:alpaca_paper" if credentialed_paper else "mode:mock"
    ]
    try:
        before_orders = _open_order_manifest(crash_broker)
        before_positions = _position_manifest(crash_broker)
    except Exception:
        raise SafetyDrillError("paper_manifest_unconfirmed") from None

    try:
        container.session_auth.authenticate(f"{tag}-invalid-session")
    except InvalidSession:
        auth_fail_closed = True
        details.append("auth:fail_closed")
    else:
        auth_fail_closed = False
        details.append("auth:unexpected_accept")

    symbol = "AAPL"
    restarted = container
    crash_core_confirmed = False
    cleanup_confirmed = False
    try:
        try:
            quote = crash_broker.get_quote(symbol)
            limit_price = _derive_nonmarketable_buy_limit(
                last=quote.last,
                ask=quote.ask,
                price_sanity_pct=config.risk.price_sanity_pct,
            )
            if limit_price is None:
                raise SafetyDrillError("quote_unconfirmed")
            quantity = (
                Decimal("1")
                if credentialed_paper
                else (Decimal("1.25") / limit_price).quantize(
                    Decimal("0.000001"),
                    rounding=ROUND_UP,
                )
            )
            proposal = container.service.propose_order(
                symbol,
                "buy",
                "limit",
                qty=str(quantity),
                limit_price=str(limit_price),
                idempotency_key=f"{tag}-crash",
                time_in_force=(
                    OrderTimeInForce.GTC.value
                    if credentialed_paper
                    else OrderTimeInForce.DAY.value
                ),
                actor="operator:safety-drill",
                reason="exercise process-death recovery",
                request_id=f"{tag}-crash",
            )
            if proposal["status"] != OrderStatus.PROPOSED.value:
                raise SafetyDrillError("proposal_unconfirmed")
            crashed_after_acceptance = False
            try:
                container.service.approve_order(
                    proposal["order_id"],
                    actor="operator:safety-drill",
                    reason="exercise process-death recovery",
                    request_id=f"{tag}-crash",
                )
            except _DrillCrash:
                crashed_after_acceptance = True
            with container.session_factory() as session:
                before_restart = session.get(Order, proposal["order_id"])
                left_submitting = (
                    before_restart is not None
                    and before_restart.status
                    == OrderStatus.SUBMITTING.value
                )
            container.engine.dispose()
            restarted = build_test_container(
                config,
                drill_secrets,
                broker=crash_broker,
                clock=clock or FakeClock(is_open=True),
            )
            with restarted.session_factory() as session:
                after_restart = session.get(Order, proposal["order_id"])
                reconstructed_submitting = (
                    after_restart is not None
                    and after_restart.status
                    == OrderStatus.SUBMITTING.value
                )
            resolved, unresolved = (
                restarted.reconciliation.reconcile_unknown(
                    actor="operator:safety-drill",
                    reason="resolve process-death acceptance by client identity",
                    request_id=f"{tag}-reconcile",
                )
            )
            replay = restarted.order_submission.submit(
                proposal["order_id"],
                actor="operator:safety-drill",
                reason="prove reconstructed order is not resubmitted",
                request_id=f"{tag}-replay",
            )
            remote = crash_broker.get_order_by_client_id(
                f"{tag}-crash"
            )
            remote_confirmed = (
                remote is not None
                and order_result_identity_error(
                    remote,
                    f"{tag}-crash",
                    symbol,
                )
                is None
            )
            crash_core_confirmed = (
                crashed_after_acceptance
                and left_submitting
                and reconstructed_submitting
                and resolved == 1
                and unresolved == ()
                and replay.status
                not in {
                    OrderStatus.SUBMITTING,
                    OrderStatus.ACCEPTANCE_UNKNOWN,
                }
                and crash_broker.submit_calls == 1
                and remote_confirmed
            )
        except Exception:
            crash_core_confirmed = False
    finally:
        cleanup_confirmed = _best_effort_cleanup(
            restarted,
            before_positions=before_positions,
            tag=tag,
            symbol=symbol,
        )
    crash_recovered_without_duplicate = (
        crash_core_confirmed and cleanup_confirmed
    )
    details.append(
        "crash:reconstructed_once"
        if crash_recovered_without_duplicate
        else "crash:unconfirmed"
    )

    try:
        group_key = f"{tag}-oco"
        first_rule = restarted.service.rule_application.create_rule(
            _rule_command(group_key, "below", "99"),
            actor="operator:safety-drill",
            reason="create OCO safety drill group",
            request_id=f"{tag}-oco-create-1",
        )
        second_rule = restarted.service.rule_application.create_rule(
            _rule_command(group_key, "above", "101"),
            actor="operator:safety-drill",
            reason="create OCO safety drill group",
            request_id=f"{tag}-oco-create-2",
        )
        with restarted.session_factory() as session:
            group_id = session.scalar(
                select(RuleGroup.id).where(RuleGroup.group_key == group_key)
            )
        assert group_id is not None
        lease_owner = f"{tag}-oco-contender"
        repositories = (
            _bounded_rule_repository(
                restarted.session_factory,
                owner=lease_owner,
            ),
            _bounded_rule_repository(
                restarted.session_factory,
                owner=lease_owner,
            ),
        )
        lease = repositories[0].lease_group(
            group_id,
            now=datetime.now(timezone.utc),
            actor="daemon:safety-drill",
            reason="exercise OCO terminal claim",
            request_id=f"{tag}-oco-lease",
        )
        claim_results: list[bool | None] = [None, None]
        claim_errors: list[bool] = []
        claim_barrier = threading.Barrier(3)

        def compete(
            index: int,
            repository: RuleRepository,
            rule_id: int,
        ) -> None:
            try:
                claim_barrier.wait(timeout=5)
                claim_results[index] = repository.claim_terminal(
                    lease,
                    rule_id,
                    now=datetime.now(timezone.utc),
                    actor="daemon:safety-drill",
                    reason="compete for OCO terminal claim",
                    request_id=f"{tag}-oco-terminal-{index}",
                )
            except Exception:
                claim_errors.append(True)

        threads = (
            threading.Thread(
                target=compete,
                args=(0, repositories[0], first_rule),
                daemon=False,
            ),
            threading.Thread(
                target=compete,
                args=(1, repositories[1], second_rule),
                daemon=False,
            ),
        )
        if lease is not None:
            for thread in threads:
                thread.start()
            try:
                claim_barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                claim_errors.append(True)
            for thread in threads:
                thread.join(timeout=_OCO_JOIN_TIMEOUT_SECONDS)
            still_running = tuple(
                thread for thread in threads if thread.is_alive()
            )
            for thread in still_running:
                # The repository writer acquisition is itself bounded. This
                # second bounded drain prevents any later gate from observing
                # or racing an OCO worker that outlived its first join.
                thread.join(
                    timeout=(
                        _OCO_REPOSITORY_TIMEOUT_SECONDS
                        + _OCO_JOIN_TIMEOUT_SECONDS
                    )
                )
        threads_stopped = all(not thread.is_alive() for thread in threads)
        if not threads_stopped:
            raise SafetyDrillError("oco_worker_timeout")
        with restarted.session_factory() as session:
            rule_states = dict(
                session.execute(
                    select(Rule.id, Rule.state).where(
                        Rule.group_id == group_id
                    )
                ).all()
            )
            group = session.get(RuleGroup, group_id)
            group_terminal_rule_id = (
                group.terminal_rule_id if group is not None else None
            )
        one_winner = claim_results.count(True) == 1
        winner_id = (
            (first_rule, second_rule)[claim_results.index(True)]
            if one_winner
            else None
        )
        loser_id = (
            second_rule if winner_id == first_rule else first_rule
        )
        oco_single_terminal = (
            lease is not None
            and threads_stopped
            and not claim_errors
            and one_winner
            and group_terminal_rule_id == winner_id
            and rule_states.get(winner_id) == "triggered"
            and rule_states.get(loser_id) == "canceled"
        )
        details.append(
            "oco:single_terminal"
            if oco_single_terminal
            else "oco:unconfirmed"
        )
    except SafetyDrillError:
        raise
    except Exception:
        oco_single_terminal = False
        details.append("oco:dependency_failed")

    try:
        data_scope = BreakerScope.data(AssetClass.EQUITY)
        liquidity_scope = BreakerScope.liquidity("AAPL")
        data_trip = restarted.breakers.trip(
            data_scope,
            "safety drill persisted data breaker",
            "operator:safety-drill",
            request_id=f"{tag}-breaker-data",
        )
        restarted.breakers.trip(
            liquidity_scope,
            "safety drill persisted liquidity breaker",
            "operator:safety-drill",
            request_id=f"{tag}-breaker-liquidity",
        )
        restarted.engine.dispose()
        restarted = build_test_container(
            config,
            drill_secrets,
            broker=crash_broker,
            clock=clock or FakeClock(is_open=True),
        )
        survived_restart = (
            restarted.breakers.is_tripped(data_scope)
            and restarted.breakers.is_tripped(liquidity_scope)
        )
        restarted.breakers.reset(
            data_scope,
            "operator:safety-drill",
            "scoped safety drill reset",
            {"broker_reconciled": True},
            expected_generation=data_trip.generation,
            request_id=f"{tag}-breaker-data-reset",
        )
        scoped_reset = (
            not restarted.breakers.is_tripped(data_scope)
            and restarted.breakers.is_tripped(liquidity_scope)
        )
        liquidity_state = restarted.breakers.get(liquidity_scope)
        if liquidity_state is not None:
            restarted.breakers.reset(
                liquidity_scope,
                "operator:safety-drill",
                "finish safety drill breaker cleanup",
                {"broker_reconciled": True},
                expected_generation=liquidity_state.generation,
                request_id=f"{tag}-breaker-liquidity-reset",
            )
        breakers_persisted = (
            survived_restart
            and scoped_reset
            and not restarted.breakers.is_tripped(liquidity_scope)
        )
        details.append(
            "breakers:persisted_scoped_reset"
            if breakers_persisted
            else "breakers:unconfirmed"
        )
    except Exception:
        breakers_persisted = False
        details.append("breakers:dependency_failed")

    try:
        order_sync = restarted.service.sync_open_orders(
            actor="operator:safety-drill",
            reason="prove final order reconciliation",
            request_id=f"{tag}-final-orders",
        )
        position_sync = restarted.service.reconcile_positions(
            actor="operator:safety-drill",
            reason="prove final position reconciliation",
            request_id=f"{tag}-final-positions",
        )
        with restarted.session_factory() as session:
            unsafe_local_orders = session.scalars(
                select(Order.id).where(
                    Order.idempotency_key.like(f"{tag}%"),
                    Order.status.in_(
                        (
                            OrderStatus.SUBMITTING.value,
                            OrderStatus.ACCEPTANCE_UNKNOWN.value,
                            OrderStatus.SUBMITTED.value,
                            OrderStatus.PARTIALLY_FILLED.value,
                        )
                    ),
                )
            ).all()
            active_breakers = session.scalars(
                select(CircuitBreakerState.scope_key).where(
                    CircuitBreakerState.tripped.is_(True)
                )
            ).all()
        tagged_broker_open = tuple(
            order
            for order in crash_broker.get_open_orders()
            if order.idempotency_key.startswith(tag)
        )
        after_orders = _open_order_manifest(crash_broker)
        after_positions = _position_manifest(crash_broker)
        paper_manifest_clean = (
            after_orders == before_orders
            and after_positions == before_positions
        )
        reconciliation_clean = (
            order_sync["failed"] == 0
            and position_sync["reconciled"]
            and not unsafe_local_orders
            and not tagged_broker_open
            and not active_breakers
            and paper_manifest_clean
        )
        details.append(
            "reconciliation:clean"
            if reconciliation_clean
            else "reconciliation:unconfirmed"
        )
        if credentialed_paper:
            details.append(
                "alpaca_paper:passed"
                if reconciliation_clean
                else "alpaca_paper:unconfirmed"
            )
    except Exception:
        reconciliation_clean = False
        details.append("reconciliation:dependency_failed")
        if credentialed_paper:
            details.append("alpaca_paper:unconfirmed")
    details.insert(
        1,
        "schema:current" if schema_current else "schema:not_current",
    )

    gates = (
        schema_current,
        auth_fail_closed,
        crash_recovered_without_duplicate,
        oco_single_terminal,
        breakers_persisted,
        reconciliation_clean,
    )
    return SafetyDrillReport(
        schema_current=schema_current,
        auth_fail_closed=auth_fail_closed,
        crash_recovered_without_duplicate=crash_recovered_without_duplicate,
        oco_single_terminal=oco_single_terminal,
        breakers_persisted=breakers_persisted,
        reconciliation_clean=reconciliation_clean,
        safe=all(gates),
        details=tuple(details),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-copy", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--alpaca-paper", action="store_true")
    args = parser.parse_args(argv)

    secrets = Secrets()
    from ..logging import runtime_startup

    try:
        with runtime_startup("safety-drill", secrets):
            config = load_config()
            if args.mock:
                from ..broker.mock import MockBroker

                broker: BrokerClient = MockBroker(
                    prices={"AAPL": Decimal("100")}
                )
                clock = FakeClock(is_open=True)
            else:
                if not (
                    secrets.alpaca_api_key
                    and secrets.alpaca_secret_key
                ):
                    raise SafetyDrillError("credentials_unavailable")
                from ..broker.alpaca import AlpacaBroker, AlpacaClock

                broker = AlpacaBroker.from_credentials(
                    secrets.alpaca_api_key,
                    secrets.alpaca_secret_key,
                    paper=True,
                    timeout_seconds=config.trading.request_timeout_seconds,
                )
                clock = AlpacaClock.from_credentials(
                    secrets.alpaca_api_key,
                    secrets.alpaca_secret_key,
                    paper=True,
                    timeout_seconds=config.trading.request_timeout_seconds,
                )
            report = run_safety_drill(
                database_copy=args.database_copy,
                config=config,
                broker=broker,
                credentialed_paper=args.alpaca_paper,
                clock=clock,
            )
    except SafetyDrillError as exc:
        print(
            json.dumps(
                {"error": exc.code, "safe": False},
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"error": "drill_failed", "safe": False},
                sort_keys=True,
            )
        )
        return 2
    print(report.to_json())
    return 0 if report.safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
