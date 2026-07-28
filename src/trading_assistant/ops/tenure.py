"""Database-authoritative runtime ownership and maintenance exclusion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import errno
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


RuntimeRole = Literal["app", "daemon", "mcp", "validation"]
TenureRole = Literal[
    "app",
    "daemon",
    "mcp",
    "validation",
    "maintenance",
]
_RUNTIME_ROLES: tuple[RuntimeRole, ...] = (
    "app",
    "daemon",
    "mcp",
    "validation",
)
_RESOURCE_FOR_ROLE: dict[TenureRole, str] = {
    "app": "runtime:app",
    "daemon": "runtime:daemon",
    "mcp": "runtime:mcp",
    "validation": "runtime:validation",
    "maintenance": "sensitive-migration:global",
}
_INTERNAL_TENURE_SQL = "_trading_assistant_tenure_internal"
_FENCE_SCHEMA_REBUILD_SQL = (
    "_trading_assistant_tenure_fence_schema_rebuild"
)


class ProcessProof(str, Enum):
    SAME = "same"
    NOT_SAME = "not_same"
    UNKNOWN = "unknown"


class TenureCloseResult(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"


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

    def __init__(
        self,
        *,
        runner: Callable[..., object] = subprocess.run,
        process_probe: Callable[[int], None] | None = None,
    ) -> None:
        self._runner = runner
        self._process_probe = process_probe or (
            lambda pid: os.kill(pid, 0)
        )

    def _read_start(self, pid: int) -> tuple[ProcessProof, str | None]:
        try:
            self._process_probe(pid)
        except ProcessLookupError as exc:
            if exc.errno == errno.ESRCH:
                return ProcessProof.NOT_SAME, None
            return ProcessProof.UNKNOWN, None
        except PermissionError:
            return ProcessProof.UNKNOWN, None
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return ProcessProof.NOT_SAME, None
            return ProcessProof.UNKNOWN, None
        except Exception:
            return ProcessProof.UNKNOWN, None
        try:
            result = self._runner(
                ["/bin/ps", "-ww", "-p", str(pid), "-o", "lstart="],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            return ProcessProof.UNKNOWN, None
        except Exception:
            return ProcessProof.UNKNOWN, None
        if (
            getattr(result, "returncode", None) != 0
            or not isinstance(getattr(result, "stdout", None), str)
            or not isinstance(getattr(result, "stderr", None), str)
            or result.stderr.strip()
        ):
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
        try:
            released = self._service._release(
                self.resource_key,
                owner_id=self.owner_id,
                generation=self.generation,
            )
        except TenureUncertain:
            if not self._service._release_confirmed(
                self.resource_key,
                owner_id=self.owner_id,
                generation=self.generation,
                identity=self.identity,
            ):
                raise
            released = True
        if not released:
            released = self._service._release_confirmed(
                self.resource_key,
                owner_id=self.owner_id,
                generation=self.generation,
                identity=self.identity,
            )
        if released:
            self._released = True
        return released

    @property
    def internal_capability(self) -> object:
        return self._service._internal_capability

    def assert_owned_in_transaction(self, connection) -> None:
        if self._released:
            raise TenureLost()
        self._service._assert_owned(
            connection,
            self.resource_key,
            owner_id=self.owner_id,
            generation=self.generation,
        )


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
        self._close_result = TenureCloseResult.NOT_ATTEMPTED
        self._close_callbacks: list[Callable[[], None]] = []

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def close_result(self) -> TenureCloseResult:
        with self._lock:
            return self._close_result

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

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("tenure_close_callback_invalid")
        with self._lock:
            if self._closed:
                raise TenureLost()
            self._close_callbacks.append(callback)

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

    def assert_owned_in_transaction(self, connection) -> None:
        """Prove the exact fenced owner inside an existing writer txn."""
        self.ensure_owned()
        assertion = getattr(
            self.handle,
            "assert_owned_in_transaction",
            None,
        )
        if not callable(assertion):
            return
        try:
            assertion(connection)
        except Exception:
            self._mark_lost()
            raise TenureLost() from None
        self.ensure_owned()

    def start(self) -> None:
        start_failure: BaseException | None = None
        with self._lock:
            if self._closed or self._thread is not None:
                raise TenureLost()
            thread = Thread(
                target=self._run,
                name=f"{self.handle.role}-tenure-renewal",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException as exc:
                if not thread.is_alive():
                    self._thread = None
                start_failure = exc
        if start_failure is None:
            return
        try:
            released = self.close()
        except BaseException:
            raise TenureUncertain() from None
        if not released:
            raise TenureUncertain()
        raise start_failure

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
            callbacks = tuple(self._close_callbacks)
            self._close_callbacks.clear()
        if thread is not None and thread is not current_thread():
            thread.join(timeout=min(self.renewal_interval_seconds, 1.0))
        released = False
        if self._lost.is_set():
            released = False
        else:
            try:
                released = self.handle.release()
            except Exception:
                self._mark_lost()
                released = False
            if not released:
                self._mark_lost()
        try:
            for callback in callbacks:
                callback()
        except Exception:
            self._mark_lost()
            released = False
        with self._lock:
            self._close_result = (
                TenureCloseResult.CONFIRMED
                if released
                else TenureCloseResult.UNCERTAIN
            )
        return released


class TenureGuardedBroker:
    """Delegate broker reads, checking tenure at the final mutation seam."""

    def __init__(self, broker, guard: RuntimeTenureGuard) -> None:
        self.__broker = broker
        self.__guard = guard

    @property
    def reconciliation_key(self):
        return self.__broker.reconciliation_key

    def get_quote(self, ticker):
        return self.__broker.get_quote(ticker)

    def get_account(self):
        return self.__broker.get_account()

    def get_positions(self):
        return self.__broker.get_positions()

    def get_order_by_client_id(self, client_order_id):
        return self.__broker.get_order_by_client_id(client_order_id)

    def get_open_orders(self):
        return self.__broker.get_open_orders()

    def get_order_status(self, order_id):
        return self.__broker.get_order_status(order_id)

    def get_fill_activities(self, *, after=None):
        reader = getattr(self.__broker, "get_fill_activities", None)
        if not callable(reader):
            raise AttributeError("get_fill_activities")
        return reader(after=after)

    def submit_order(self, order):
        self.__guard.ensure_owned()
        return self.__broker.submit_order(order)

    def submit_bracket(self, order, take_profit, stop_loss):
        self.__guard.ensure_owned()
        return self.__broker.submit_bracket(
            order,
            take_profit,
            stop_loss,
        )

    def cancel_order(self, order_id):
        self.__guard.ensure_owned()
        return self.__broker.cancel_order(order_id)


class RuntimeMutationBarrier:
    """Removable SQL and transaction-commit fence for one tenure."""

    def __init__(
        self,
        engine: Engine,
        guard: RuntimeTenureGuard,
    ) -> None:
        self.engine = engine
        self.guard = guard
        try:
            self._capability = guard.handle.internal_capability
        except Exception:
            self._capability = None
        self._fence_schema_capability = object()
        self._closed = False
        self._installed_listeners: list[
            tuple[object, str, Callable[..., object]]
        ] = []
        registrations = (
            (
                engine,
                "before_cursor_execute",
                self._before_cursor_execute,
            ),
            (engine, "commit", self._before_commit),
            (
                engine,
                "release_savepoint",
                self._before_release_savepoint,
            ),
        )
        try:
            for target, identifier, callback in registrations:
                event.listen(target, identifier, callback)
                self._installed_listeners.append(
                    (target, identifier, callback)
                )
        except BaseException:
            for target, identifier, callback in reversed(
                self._installed_listeners
            ):
                try:
                    event.remove(target, identifier, callback)
                except Exception:
                    pass
            self._installed_listeners.clear()
            self._closed = True
            raise

    def _internal(self, connection, context=None) -> bool:
        if self._capability is None:
            return False
        option = connection.get_execution_options().get(
            _INTERNAL_TENURE_SQL
        )
        if option is self._capability:
            return True
        if context is not None:
            return (
                context.execution_options.get(_INTERNAL_TENURE_SQL)
                is self._capability
            )
        return False

    def _fence_schema_rebuild(
        self,
        connection,
        context=None,
    ) -> bool:
        option = connection.get_execution_options().get(
            _FENCE_SCHEMA_REBUILD_SQL
        )
        if option is self._fence_schema_capability:
            return True
        if context is not None:
            return (
                context.execution_options.get(
                    _FENCE_SCHEMA_REBUILD_SQL
                )
                is self._fence_schema_capability
            )
        return False

    @property
    def fence_schema_execution_option(self) -> tuple[str, object]:
        """Opaque, narrowly scoped capability for rebuilding the fence table."""
        return (
            _FENCE_SCHEMA_REBUILD_SQL,
            self._fence_schema_capability,
        )

    @staticmethod
    def _read_only(statement: object) -> bool:
        if not isinstance(statement, str):
            return False
        normalized = statement.lstrip().upper()
        return normalized.startswith(("SELECT", "EXPLAIN", "VALUES"))

    @staticmethod
    def _rollback_driver(connection) -> None:
        try:
            proxied = connection.connection
            driver = getattr(proxied, "driver_connection", proxied)
            driver.rollback()
        except Exception:
            pass

    def _fence_commit(self, connection) -> None:
        if self._internal(connection):
            return
        try:
            self.guard.assert_owned_in_transaction(connection)
        except TenureLost:
            self._rollback_driver(connection)
            raise

    def _before_cursor_execute(
        self,
        connection,
        _cursor,
        statement,
        _parameters,
        context,
        _executemany,
    ) -> None:
        if self._internal(connection, context):
            return
        if self._fence_schema_rebuild(connection, context):
            # The runtime_tenures table is temporarily unavailable while
            # SQLite recreates its CHECK constraint. The migration performs
            # exact owner/generation proofs immediately before and after this
            # narrow block; every statement still checks the latched guard.
            self.guard.ensure_owned()
            return
        if self._read_only(statement):
            return
        normalized = (
            statement.lstrip().upper()
            if isinstance(statement, str)
            else ""
        )
        if normalized.startswith("BEGIN"):
            # Proving ownership with SELECT would itself autobegin and make
            # an explicit BEGIN IMMEDIATE invalid. Every subsequent mutation
            # and the final commit perform the exact in-transaction proof.
            self.guard.ensure_owned()
            return
        self.guard.assert_owned_in_transaction(connection)

    def _before_commit(self, connection) -> None:
        self._fence_commit(connection)

    def _before_release_savepoint(
        self,
        connection,
        _name,
        _context,
    ) -> None:
        self._fence_commit(connection)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failure: Exception | None = None
        for target, identifier, callback in reversed(
            self._installed_listeners
        ):
            try:
                event.remove(target, identifier, callback)
            except Exception as exc:
                failure = failure or exc
        self._installed_listeners.clear()
        if failure is not None:
            raise failure


def install_runtime_mutation_barrier(
    engine: Engine,
    guard: RuntimeTenureGuard,
) -> RuntimeMutationBarrier:
    """Fence SQL execution and final commit to exact live ownership."""

    barrier = RuntimeMutationBarrier(engine, guard)
    try:
        guard.add_close_callback(barrier.close)
    except BaseException:
        barrier.close()
        raise
    return barrier


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
        self._internal_capability = object()

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
        if row.state in {"released", "fenced"}:
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
        # Preserve the distinction between an owner-confirmed graceful release
        # and an authority-revoking reclaim by a successor.  A predecessor may
        # resolve a lost release response only from the former.
        row.state = "fenced"
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
                            # A crashed/uncertain maintenance owner may have
                            # committed either side of a terminal transition.
                            # Only a new maintenance owner may reclaim and
                            # reconcile that state; runtime startup must not
                            # turn lease expiry into proof of completion.
                            raise TenureUnavailable(
                                "maintenance_recovery_required"
                            )

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
                connection = session.connection(
                    execution_options={
                        _INTERNAL_TENURE_SQL: self._internal_capability
                    }
                )
                connection.exec_driver_sql("BEGIN IMMEDIATE")
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
                connection = session.connection(
                    execution_options={
                        _INTERNAL_TENURE_SQL: self._internal_capability
                    }
                )
                connection.exec_driver_sql("BEGIN IMMEDIATE")
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
                )
                session.commit()
                return result.rowcount == 1
        except (SQLAlchemyError, OSError, ValueError, TypeError):
            raise TenureUncertain() from None

    def _release_confirmed(
        self,
        resource_key: str,
        *,
        owner_id: str,
        generation: int,
        identity: ProcessIdentity,
    ) -> bool:
        """Resolve an ambiguous release only from exact durable row truth."""
        try:
            with self._session_factory() as session:
                row = session.get(RuntimeTenure, resource_key)
                return bool(
                    row is not None
                    and row.state == "released"
                    and row.owner_id == owner_id
                    and row.generation == generation + 1
                    and row.pid == identity.pid
                    and row.process_start_identity
                    == identity.start_identity
                    and row.released_at is not None
                    and row.released_at == row.expires_at
                )
        except (SQLAlchemyError, OSError, ValueError, TypeError):
            return False

    def _assert_owned(
        self,
        connection,
        resource_key: str,
        *,
        owner_id: str,
        generation: int,
    ) -> None:
        """Verify exact ownership using the caller's locked transaction."""
        now = self._now()
        try:
            row = connection.execute(
                select(RuntimeTenure.resource_key).where(
                    RuntimeTenure.resource_key == resource_key,
                    RuntimeTenure.state == "held",
                    RuntimeTenure.owner_id == owner_id,
                    RuntimeTenure.generation == generation,
                    RuntimeTenure.expires_at > now,
                )
            ).one_or_none()
        except (SQLAlchemyError, OSError, ValueError, TypeError):
            raise TenureUncertain() from None
        if row is None:
            raise TenureLost()
