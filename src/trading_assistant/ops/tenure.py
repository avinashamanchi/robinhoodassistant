"""Database-authoritative runtime ownership and maintenance exclusion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import os
import subprocess
from threading import Event, Lock, Thread, current_thread
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import RuntimeTenure, utcnow


RuntimeRole = Literal["app", "daemon", "mcp"]
TenureRole = Literal["app", "daemon", "mcp", "maintenance"]
_RUNTIME_ROLES: tuple[RuntimeRole, ...] = ("app", "daemon", "mcp")
_RESOURCE_FOR_ROLE: dict[TenureRole, str] = {
    "app": "runtime:app",
    "daemon": "runtime:daemon",
    "mcp": "runtime:mcp",
    "maintenance": "sensitive-migration:global",
}
_INTERNAL_TENURE_SQL = "_trading_assistant_tenure_internal"


class ProcessProof(str, Enum):
    SAME = "same"
    NOT_SAME = "not_same"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_identity: str

    def validate(self) -> None:
        if (
            isinstance(self.pid, bool)
            or not isinstance(self.pid, int)
            or self.pid <= 0
            or not isinstance(self.start_identity, str)
            or not self.start_identity.strip()
            or len(self.start_identity) > 256
            or "\x00" in self.start_identity
        ):
            raise ValueError("process_identity_invalid")


class ProcessInspector(Protocol):
    def inspect(self, identity: ProcessIdentity) -> ProcessProof: ...


class RenewableTenure(Protocol):
    role: TenureRole

    def renew(self, *, ttl_seconds: int) -> None: ...

    def release(self) -> bool: ...


class TenureUnavailable(RuntimeError):
    def __init__(self, stable_code: str) -> None:
        self.stable_code = stable_code
        super().__init__(stable_code)


class TenureLost(RuntimeError):
    stable_code = "runtime_tenure_lost"

    def __init__(self) -> None:
        super().__init__(self.stable_code)


class TenureUncertain(RuntimeError):
    stable_code = "runtime_tenure_uncertain"

    def __init__(self) -> None:
        super().__init__(self.stable_code)


class LocalProcessInspector:
    """Compare PID/start identity without signalling another process."""

    @staticmethod
    def _read_start(pid: int) -> tuple[ProcessProof, str | None]:
        try:
            result = subprocess.run(
                ["ps", "-ww", "-p", str(pid), "-o", "lstart="],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            return ProcessProof.UNKNOWN, None
        if result.returncode == 1 and not result.stdout.strip():
            return ProcessProof.NOT_SAME, None
        if result.returncode != 0:
            return ProcessProof.UNKNOWN, None
        value = " ".join(result.stdout.split())
        if not value:
            return ProcessProof.UNKNOWN, None
        return ProcessProof.SAME, value

    def current(self) -> ProcessIdentity:
        pid = os.getpid()
        proof, start_identity = self._read_start(pid)
        if proof is not ProcessProof.SAME or start_identity is None:
            raise TenureUnavailable("current_process_identity_unknown")
        return ProcessIdentity(pid=pid, start_identity=start_identity)

    def inspect(self, identity: ProcessIdentity) -> ProcessProof:
        try:
            identity.validate()
        except ValueError:
            return ProcessProof.UNKNOWN
        proof, current_start = self._read_start(identity.pid)
        if proof is not ProcessProof.SAME:
            return proof
        return (
            ProcessProof.SAME
            if current_start == identity.start_identity
            else ProcessProof.NOT_SAME
        )


@dataclass
class RuntimeTenureHandle:
    _service: "RuntimeTenureService"
    resource_key: str
    role: TenureRole
    owner_id: str
    generation: int
    identity: ProcessIdentity
    expires_at: datetime
    _released: bool = False

    def renew(self, *, ttl_seconds: int) -> None:
        if self._released:
            raise TenureLost()
        self.expires_at = self._service._renew(
            self.resource_key,
            owner_id=self.owner_id,
            generation=self.generation,
            ttl_seconds=ttl_seconds,
        )

    def release(self) -> bool:
        if self._released:
            return False
        released = self._service._release(
            self.resource_key,
            owner_id=self.owner_id,
            generation=self.generation,
        )
        if released:
            self._released = True
        return released


class RuntimeTenureGuard:
    """Continuously renew one tenure and latch ownership loss once."""

    def __init__(
        self,
        handle: RenewableTenure,
        *,
        ttl_seconds: int,
        renewal_interval_seconds: float,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        if (
            ttl_seconds <= 0
            or renewal_interval_seconds <= 0
            or renewal_interval_seconds >= ttl_seconds
        ):
            raise ValueError("tenure_renewal_interval_invalid")
        self.handle = handle
        self.ttl_seconds = ttl_seconds
        self.renewal_interval_seconds = renewal_interval_seconds
        self._on_lost = on_lost
        self._lost = Event()
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._closed = False

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _mark_lost(self) -> None:
        callback: Callable[[], None] | None = None
        with self._lock:
            if not self._lost.is_set():
                self._lost.set()
                callback = self._on_lost
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def set_on_lost(self, callback: Callable[[], None]) -> None:
        """Install the lifecycle owner, notifying it if loss already latched."""
        if not callable(callback):
            raise TypeError("tenure_loss_callback_invalid")
        notify_now = False
        with self._lock:
            self._on_lost = callback
            notify_now = self._lost.is_set()
        if notify_now:
            try:
                callback()
            except Exception:
                pass

    def ensure_owned(self) -> None:
        if self._lost.is_set() or self._closed:
            raise TenureLost()

    def renew_once(self) -> bool:
        if self._lost.is_set() or self._closed:
            return False
        try:
            self.handle.renew(ttl_seconds=self.ttl_seconds)
        except Exception:
            self._mark_lost()
            return False
        return True

    def start(self) -> None:
        with self._lock:
            if self._closed or self._thread is not None:
                raise TenureLost()
            self._thread = Thread(
                target=self._run,
                name=f"{self.handle.role}-tenure-renewal",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.renewal_interval_seconds):
            if not self.renew_once():
                return

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=min(self.renewal_interval_seconds, 1.0))
        if self._lost.is_set():
            return False
        try:
            released = self.handle.release()
        except Exception:
            self._mark_lost()
            return False
        if not released:
            self._mark_lost()
        return released


class TenureGuardedBroker:
    """Delegate broker reads, checking tenure at the final mutation seam."""

    def __init__(self, broker, guard: RuntimeTenureGuard) -> None:
        self._broker = broker
        self._guard = guard

    @property
    def reconciliation_key(self):
        return self._broker.reconciliation_key

    def get_quote(self, ticker):
        return self._broker.get_quote(ticker)

    def get_account(self):
        return self._broker.get_account()

    def get_positions(self):
        return self._broker.get_positions()

    def get_order_by_client_id(self, client_order_id):
        return self._broker.get_order_by_client_id(client_order_id)

    def get_open_orders(self):
        return self._broker.get_open_orders()

    def get_order_status(self, order_id):
        return self._broker.get_order_status(order_id)

    def submit_order(self, order):
        self._guard.ensure_owned()
        return self._broker.submit_order(order)

    def submit_bracket(self, order, take_profit, stop_loss):
        self._guard.ensure_owned()
        return self._broker.submit_bracket(
            order,
            take_profit,
            stop_loss,
        )

    def cancel_order(self, order_id):
        self._guard.ensure_owned()
        return self._broker.cancel_order(order_id)

    def __getattr__(self, name: str):
        return getattr(self._broker, name)


def install_runtime_mutation_barrier(
    engine: Engine,
    guard: RuntimeTenureGuard,
) -> None:
    """Block authoritative SQL mutations after runtime ownership is lost."""

    def before_cursor_execute(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = statement.lstrip().upper()
        mutating = normalized.startswith(
            ("INSERT", "UPDATE", "DELETE", "REPLACE", "WITH")
        )
        if not mutating:
            return
        # Only the service's exact fenced renew/release statements receive
        # this private execution option. SQL text mentioning the table is not
        # an exemption.
        if _context.execution_options.get(_INTERNAL_TENURE_SQL) is True:
            return
        guard.ensure_owned()

    event.listen(engine, "before_cursor_execute", before_cursor_execute)


class RuntimeTenureService:
    """Serialize runtime and maintenance acquisition in BEGIN IMMEDIATE."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        process_inspector: ProcessInspector,
        clock: Callable[[], datetime] = utcnow,
        owner_factory: Callable[[], object] = uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._process_inspector = process_inspector
        self._clock = clock
        self._owner_factory = owner_factory

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TenureUncertain()
        return value.astimezone(timezone.utc)

    def _now(self) -> datetime:
        return self._aware(self._clock())

    def _new_owner(self) -> str:
        try:
            owner = str(self._owner_factory())
            parsed = UUID(owner)
        except (TypeError, ValueError, AttributeError):
            raise TenureUncertain() from None
        if parsed.version != 4 or str(parsed) != owner:
            raise TenureUncertain()
        return owner

    @staticmethod
    def _validate_ttl(ttl_seconds: int) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds <= 0
        ):
            raise ValueError("tenure_ttl_invalid")

    @staticmethod
    def _identity(row: RuntimeTenure) -> ProcessIdentity:
        identity = ProcessIdentity(
            pid=row.pid,
            start_identity=row.process_start_identity,
        )
        identity.validate()
        return identity

    def _require_reclaimable(
        self,
        row: RuntimeTenure,
        *,
        now: datetime,
        active_code: str,
        live_code: str,
        unknown_code: str,
    ) -> None:
        if row.state == "released":
            return
        if row.state != "held" or row.expires_at is None:
            raise TenureUnavailable(unknown_code)
        if row.expires_at > now:
            raise TenureUnavailable(active_code)
        try:
            identity = self._identity(row)
            proof = self._process_inspector.inspect(identity)
        except Exception:
            raise TenureUnavailable(unknown_code) from None
        if proof is ProcessProof.SAME:
            raise TenureUnavailable(live_code)
        if proof is not ProcessProof.NOT_SAME:
            raise TenureUnavailable(unknown_code)

    @staticmethod
    def _fence_reclaimed(row: RuntimeTenure, now: datetime) -> None:
        row.state = "released"
        row.generation += 1
        row.renewed_at = now
        row.expires_at = now
        row.released_at = now

    def acquire_runtime(
        self,
        role: RuntimeRole,
        identity: ProcessIdentity,
        *,
        ttl_seconds: int,
    ) -> RuntimeTenureHandle:
        if role not in _RUNTIME_ROLES:
            raise ValueError("runtime_role_invalid")
        return self._acquire(role, identity, ttl_seconds=ttl_seconds)

    def acquire_maintenance(
        self,
        identity: ProcessIdentity,
        *,
        ttl_seconds: int,
    ) -> RuntimeTenureHandle:
        return self._acquire(
            "maintenance",
            identity,
            ttl_seconds=ttl_seconds,
        )

    def _acquire(
        self,
        role: TenureRole,
        identity: ProcessIdentity,
        *,
        ttl_seconds: int,
    ) -> RuntimeTenureHandle:
        identity.validate()
        self._validate_ttl(ttl_seconds)
        now = self._now()
        owner_id = self._new_owner()
        resource_key = _RESOURCE_FOR_ROLE[role]
        try:
            with self._session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                rows = {
                    row.resource_key: row
                    for row in session.scalars(
                        select(RuntimeTenure)
                    ).all()
                }
                if role == "maintenance":
                    for runtime_role in _RUNTIME_ROLES:
                        runtime = rows.get(_RESOURCE_FOR_ROLE[runtime_role])
                        if runtime is None:
                            continue
                        self._require_reclaimable(
                            runtime,
                            now=now,
                            active_code="runtime_tenure_active",
                            live_code="runtime_process_live",
                            unknown_code="runtime_process_unknown",
                        )
                        if runtime.state == "held":
                            self._fence_reclaimed(runtime, now)
                else:
                    maintenance = rows.get(
                        _RESOURCE_FOR_ROLE["maintenance"]
                    )
                    if maintenance is not None:
                        self._require_reclaimable(
                            maintenance,
                            now=now,
                            active_code="maintenance_tenure_active",
                            live_code="maintenance_process_live",
                            unknown_code="maintenance_process_unknown",
                        )
                        if maintenance.state == "held":
                            self._fence_reclaimed(maintenance, now)

                current = rows.get(resource_key)
                if current is not None:
                    self._require_reclaimable(
                        current,
                        now=now,
                        active_code=(
                            "maintenance_tenure_active"
                            if role == "maintenance"
                            else "runtime_tenure_active"
                        ),
                        live_code=(
                            "maintenance_process_live"
                            if role == "maintenance"
                            else "runtime_process_live"
                        ),
                        unknown_code=(
                            "maintenance_process_unknown"
                            if role == "maintenance"
                            else "runtime_process_unknown"
                        ),
                    )
                    generation = current.generation + 1
                    current.role = role
                    current.state = "held"
                    current.owner_id = owner_id
                    current.generation = generation
                    current.pid = identity.pid
                    current.process_start_identity = (
                        identity.start_identity
                    )
                    current.acquired_at = now
                    current.renewed_at = now
                    current.expires_at = now + timedelta(
                        seconds=ttl_seconds
                    )
                    current.released_at = None
                    row = current
                else:
                    generation = 1
                    row = RuntimeTenure(
                        resource_key=resource_key,
                        role=role,
                        state="held",
                        owner_id=owner_id,
                        generation=generation,
                        pid=identity.pid,
                        process_start_identity=identity.start_identity,
                        acquired_at=now,
                        renewed_at=now,
                        expires_at=now + timedelta(seconds=ttl_seconds),
                        released_at=None,
                    )
                    session.add(row)
                session.commit()
                expires_at = row.expires_at
        except TenureUnavailable:
            raise
        except (SQLAlchemyError, OSError, ValueError, TypeError):
            raise TenureUncertain() from None
        return RuntimeTenureHandle(
            _service=self,
            resource_key=resource_key,
            role=role,
            owner_id=owner_id,
            generation=generation,
            identity=identity,
            expires_at=expires_at,
        )

    def _renew(
        self,
        resource_key: str,
        *,
        owner_id: str,
        generation: int,
        ttl_seconds: int,
    ) -> datetime:
        self._validate_ttl(ttl_seconds)
        now = self._now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        try:
            with self._session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                result = session.execute(
                    update(RuntimeTenure)
                    .where(
                        RuntimeTenure.resource_key == resource_key,
                        RuntimeTenure.state == "held",
                        RuntimeTenure.owner_id == owner_id,
                        RuntimeTenure.generation == generation,
                        RuntimeTenure.expires_at > now,
                    )
                    .values(
                        renewed_at=now,
                        expires_at=expires_at,
                    )
                    .execution_options(
                        **{_INTERNAL_TENURE_SQL: True}
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    raise TenureLost()
                session.commit()
        except TenureLost:
            raise
        except (SQLAlchemyError, OSError, ValueError, TypeError):
            raise TenureUncertain() from None
        return expires_at

    def _release(
        self,
        resource_key: str,
        *,
        owner_id: str,
        generation: int,
    ) -> bool:
        now = self._now()
        try:
            with self._session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                result = session.execute(
                    update(RuntimeTenure)
                    .where(
                        RuntimeTenure.resource_key == resource_key,
                        RuntimeTenure.state == "held",
                        RuntimeTenure.owner_id == owner_id,
                        RuntimeTenure.generation == generation,
                        RuntimeTenure.expires_at > now,
                    )
                    .values(
                        state="released",
                        generation=RuntimeTenure.generation + 1,
                        renewed_at=now,
                        expires_at=now,
                        released_at=now,
                    )
                    .execution_options(
                        **{_INTERNAL_TENURE_SQL: True}
                    )
                )
                session.commit()
                return result.rowcount == 1
        except (SQLAlchemyError, OSError, ValueError, TypeError):
            raise TenureUncertain() from None
