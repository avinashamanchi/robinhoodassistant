"""Database-authoritative runtime and sensitive-maintenance tenure tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from trading_assistant.bootstrap import (
    DatabaseRuntime,
    acquire_runtime_guard,
)
from trading_assistant.db.models import Base, RuntimeTenure
from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.ops.tenure import (
    LocalProcessInspector,
    ProcessIdentity,
    ProcessProof,
    RuntimeTenureGuard,
    RuntimeTenureService,
    TenureCloseResult,
    TenureGuardedBroker,
    TenureLost,
    TenureUncertain,
    TenureUnavailable,
    install_runtime_mutation_barrier,
)

_LOCAL_PROCESS_INSPECT = LocalProcessInspector.inspect
_LOCAL_PROCESS_CURRENT = LocalProcessInspector.current


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
CANONICAL_START = "ps-lstart-v1:Sun Jul 27 20:00:00 2026"
APP = ProcessIdentity(pid=4101, start_identity="app-start-20260727T120000Z")
DAEMON = ProcessIdentity(
    pid=4102,
    start_identity="daemon-start-20260727T120000Z",
)
MCP = ProcessIdentity(
    pid=4104,
    start_identity="mcp-start-20260727T120000Z",
)
VALIDATION = ProcessIdentity(
    pid=4105,
    start_identity="validation-start-20260727T120000Z",
)
MAINTENANCE = ProcessIdentity(
    pid=4103,
    start_identity="maintenance-start-20260727T120000Z",
)


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeProcessInspector:
    def __init__(self) -> None:
        self.proofs: dict[ProcessIdentity, ProcessProof] = {}
        self.inspected: list[ProcessIdentity] = []

    def set(self, identity: ProcessIdentity, proof: ProcessProof) -> None:
        self.proofs[identity] = proof

    def inspect(self, identity: ProcessIdentity) -> ProcessProof:
        self.inspected.append(identity)
        return self.proofs.get(identity, ProcessProof.UNKNOWN)


@pytest.fixture
def tenure_service(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path}/tenures.db")
    Base.metadata.create_all(engine)
    clock = MutableClock()
    inspector = FakeProcessInspector()
    service = RuntimeTenureService(
        make_session_factory(engine),
        process_inspector=inspector,
        clock=clock,
    )
    return service, clock, inspector


def test_runtime_first_blocks_maintenance_in_same_database(tenure_service):
    service, _clock, _inspector = tenure_service
    app = service.acquire_runtime("app", APP, ttl_seconds=30)

    with pytest.raises(TenureUnavailable) as exc:
        service.acquire_maintenance(MAINTENANCE, ttl_seconds=30)

    assert exc.value.stable_code == "runtime_tenure_active"
    assert app.role == "app"


def test_maintenance_first_blocks_runtime_in_same_database(tenure_service):
    service, _clock, _inspector = tenure_service
    maintenance = service.acquire_maintenance(
        MAINTENANCE,
        ttl_seconds=30,
    )

    with pytest.raises(TenureUnavailable) as exc:
        service.acquire_runtime("daemon", DAEMON, ttl_seconds=30)

    assert exc.value.stable_code == "maintenance_tenure_active"
    assert maintenance.role == "maintenance"


def test_app_daemon_and_mcp_runtime_tenures_coexist(tenure_service):
    service, _clock, _inspector = tenure_service

    app = service.acquire_runtime("app", APP, ttl_seconds=30)
    daemon = service.acquire_runtime("daemon", DAEMON, ttl_seconds=30)
    mcp = service.acquire_runtime("mcp", MCP, ttl_seconds=30)

    assert len({app.owner_id, daemon.owner_id, mcp.owner_id}) == 3
    assert UUID(app.owner_id).version == 4
    assert UUID(daemon.owner_id).version == 4
    assert UUID(mcp.owner_id).version == 4


def test_all_runtime_writer_roles_coexist_and_exclude_maintenance(
    tenure_service,
):
    service, _clock, _inspector = tenure_service

    app = service.acquire_runtime("app", APP, ttl_seconds=30)
    daemon = service.acquire_runtime("daemon", DAEMON, ttl_seconds=30)
    mcp = service.acquire_runtime("mcp", MCP, ttl_seconds=30)
    validation = service.acquire_runtime(
        "validation",
        VALIDATION,
        ttl_seconds=30,
    )

    assert len(
        {
            app.owner_id,
            daemon.owner_id,
            mcp.owner_id,
            validation.owner_id,
        }
    ) == 4
    with pytest.raises(TenureUnavailable) as exc:
        service.acquire_maintenance(MAINTENANCE, ttl_seconds=30)
    assert exc.value.stable_code == "runtime_tenure_active"


def test_maintenance_excludes_validation_runtime_writer(tenure_service):
    service, _clock, _inspector = tenure_service
    maintenance = service.acquire_maintenance(
        MAINTENANCE,
        ttl_seconds=30,
    )

    with pytest.raises(TenureUnavailable) as exc:
        service.acquire_runtime(
            "validation",
            VALIDATION,
            ttl_seconds=30,
        )

    assert exc.value.stable_code == "maintenance_tenure_active"
    assert maintenance.role == "maintenance"


def test_live_mcp_runtime_tenure_blocks_maintenance(tenure_service):
    service, _clock, _inspector = tenure_service
    mcp = service.acquire_runtime("mcp", MCP, ttl_seconds=30)

    with pytest.raises(TenureUnavailable) as exc:
        service.acquire_maintenance(MAINTENANCE, ttl_seconds=30)

    assert exc.value.stable_code == "runtime_tenure_active"
    assert mcp.role == "mcp"


def test_maintenance_blocks_mcp_runtime_tenure(tenure_service):
    service, _clock, _inspector = tenure_service
    maintenance = service.acquire_maintenance(
        MAINTENANCE,
        ttl_seconds=30,
    )

    with pytest.raises(TenureUnavailable) as exc:
        service.acquire_runtime("mcp", MCP, ttl_seconds=30)

    assert exc.value.stable_code == "maintenance_tenure_active"
    assert maintenance.role == "maintenance"


def test_expired_runtime_with_same_live_process_blocks_maintenance(
    tenure_service,
):
    service, clock, inspector = tenure_service
    service.acquire_runtime("app", APP, ttl_seconds=10)
    clock.advance(11)
    inspector.set(APP, ProcessProof.SAME)

    with pytest.raises(TenureUnavailable) as exc:
        service.acquire_maintenance(MAINTENANCE, ttl_seconds=30)

    assert exc.value.stable_code == "runtime_process_live"
    assert inspector.inspected == [APP]


def test_expired_runtime_with_unknown_process_proof_fails_closed(
    tenure_service,
):
    service, clock, inspector = tenure_service
    service.acquire_runtime("daemon", DAEMON, ttl_seconds=10)
    clock.advance(11)

    with pytest.raises(TenureUnavailable) as exc:
        service.acquire_maintenance(MAINTENANCE, ttl_seconds=30)

    assert exc.value.stable_code == "runtime_process_unknown"
    assert inspector.inspected == [DAEMON]


def test_expired_runtime_is_reclaimed_only_after_exact_not_same_proof(
    tenure_service,
):
    service, clock, inspector = tenure_service
    expired = service.acquire_runtime("app", APP, ttl_seconds=10)
    clock.advance(11)
    inspector.set(APP, ProcessProof.NOT_SAME)

    maintenance = service.acquire_maintenance(
        MAINTENANCE,
        ttl_seconds=30,
    )

    assert maintenance.generation == 1
    with pytest.raises(TenureLost):
        expired.renew(ttl_seconds=30)
    assert expired.release() is False
    with service._session_factory() as session:
        reclaimed = session.get(RuntimeTenure, "runtime:app")
        assert reclaimed is not None
        assert reclaimed.state == "fenced"


def test_expired_maintenance_requires_maintenance_recovery_before_runtime(
    tenure_service,
):
    service, clock, inspector = tenure_service
    expired = service.acquire_maintenance(MAINTENANCE, ttl_seconds=10)
    clock.advance(11)
    inspector.set(MAINTENANCE, ProcessProof.NOT_SAME)

    with pytest.raises(TenureUnavailable) as exc:
        service.acquire_runtime("app", APP, ttl_seconds=30)

    assert exc.value.stable_code == "maintenance_recovery_required"
    recovery = service.acquire_maintenance(
        ProcessIdentity(
            pid=4301,
            start_identity="maintenance-recovery-20260727T120011Z",
        ),
        ttl_seconds=30,
    )
    assert recovery.generation > expired.generation
    assert recovery.release() is True
    assert service.acquire_runtime("app", APP, ttl_seconds=30).role == "app"


def test_successor_generation_fences_predecessor_renew_and_release(
    tenure_service,
):
    service, clock, inspector = tenure_service
    predecessor = service.acquire_runtime("app", APP, ttl_seconds=10)
    clock.advance(11)
    inspector.set(APP, ProcessProof.NOT_SAME)
    successor_identity = ProcessIdentity(
        pid=4201,
        start_identity="app-successor-20260727T120011Z",
    )

    successor = service.acquire_runtime(
        "app",
        successor_identity,
        ttl_seconds=30,
    )

    assert successor.generation > predecessor.generation
    with pytest.raises(TenureLost):
        predecessor.renew(ttl_seconds=30)
    assert predecessor.release() is False
    successor.renew(ttl_seconds=30)


def test_graceful_release_requires_exact_live_owner_and_generation(
    tenure_service,
):
    service, _clock, _inspector = tenure_service
    app = service.acquire_runtime("app", APP, ttl_seconds=30)

    assert app.release() is True
    assert app.release() is False

    maintenance = service.acquire_maintenance(
        MAINTENANCE,
        ttl_seconds=30,
    )
    assert maintenance.release() is True


@pytest.mark.parametrize(
    "identity",
    [
        ProcessIdentity(pid=0, start_identity="start"),
        ProcessIdentity(pid=-1, start_identity="start"),
        ProcessIdentity(pid=1, start_identity=""),
        ProcessIdentity(pid=1, start_identity=" \t"),
    ],
)
def test_malformed_process_identity_is_never_accepted(identity):
    with pytest.raises(ValueError, match="process_identity_invalid"):
        identity.validate()


class FailingRenewHandle:
    role = "app"

    def __init__(self) -> None:
        self.release_calls = 0

    def renew(self, *, ttl_seconds: int) -> None:
        raise TenureUncertain()

    def release(self) -> bool:
        self.release_calls += 1
        return True


def test_renewal_uncertainty_immediately_latches_and_requests_shutdown():
    shutdowns: list[str] = []
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
        on_lost=lambda: shutdowns.append("requested"),
    )

    assert guard.renew_once() is False
    assert shutdowns == ["requested"]
    with pytest.raises(TenureLost):
        guard.ensure_owned()
    assert guard.renew_once() is False
    assert shutdowns == ["requested"]


def test_late_shutdown_owner_is_notified_if_started_guard_already_lost():
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    shutdowns: list[str] = []

    assert guard.renew_once() is False
    guard.set_on_lost(lambda: shutdowns.append("requested"))

    assert shutdowns == ["requested"]


def test_graceful_guard_close_releases_exact_handle_once():
    handle = FailingRenewHandle()
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )

    assert guard.close() is True
    assert guard.close() is False
    assert handle.release_calls == 1


def test_guard_resolves_release_response_loss_from_exact_database_truth(
    tenure_service,
    monkeypatch,
):
    service, _clock, _inspector = tenure_service
    handle = service.acquire_runtime("app", APP, ttl_seconds=30)
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    original_release = service._release

    def commit_then_lose_response(*args, **kwargs):
        assert original_release(*args, **kwargs) is True
        raise TenureUncertain()

    monkeypatch.setattr(service, "_release", commit_then_lose_response)

    assert guard.close() is True
    assert guard.close_result is TenureCloseResult.CONFIRMED


def test_started_guard_stops_renewal_worker_before_graceful_release():
    handle = FailingRenewHandle()
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    guard.start()

    assert guard.close() is True
    assert handle.release_calls == 1


def test_guard_start_failure_releases_exact_ownership(
    monkeypatch,
):
    import trading_assistant.ops.tenure as tenure_module

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread-start-failed")

        def is_alive(self):
            return False

    handle = FailingRenewHandle()
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    monkeypatch.setattr(tenure_module, "Thread", BrokenThread)

    with pytest.raises(RuntimeError, match="thread-start-failed"):
        guard.start()

    assert guard.closed
    assert guard.close_result is TenureCloseResult.CONFIRMED
    assert handle.release_calls == 1


def test_acquire_runtime_guard_preserves_confirmed_thread_start_failure(
    tmp_path,
    monkeypatch,
):
    import trading_assistant.ops.tenure as tenure_module

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread-start-failed")

        def is_alive(self):
            return False

    engine = create_db_engine(f"sqlite:///{tmp_path}/start-failure.db")
    Base.metadata.create_all(engine)
    runtime = DatabaseRuntime(
        engine=engine,
        session_factory=make_session_factory(engine),
    )
    monkeypatch.setattr(tenure_module, "Thread", BrokenThread)

    with pytest.raises(RuntimeError, match="thread-start-failed"):
        acquire_runtime_guard(
            runtime,
            "app",
            process_identity=APP,
            process_inspector=FakeProcessInspector(),
            tenure_clock=lambda: NOW,
        )

    with Session(engine) as session:
        row = session.get(RuntimeTenure, "runtime:app")
        assert row is not None
        assert row.state == "released"
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE listener_cleanup_probe "
            "(id INTEGER PRIMARY KEY)"
        )


def test_guard_close_preserves_uncertain_cleanup_result_across_rechecks():
    class UncertainReleaseHandle:
        role = "app"

        def renew(self, *, ttl_seconds):
            return None

        def release(self):
            return False

    guard = RuntimeTenureGuard(
        UncertainReleaseHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )

    assert guard.close() is False
    assert guard.close_result.value == "uncertain"
    assert guard.close() is False
    assert guard.close_result.value == "uncertain"


def test_app_rejects_mutations_immediately_after_tenure_loss(
    make_service,
    operator_token,
):
    from fastapi.testclient import TestClient

    from trading_assistant.app.main import create_test_app as create_app

    class StubAgent:
        def chat(self, _message, **_context):
            return {"reply": "unused", "tool_calls": []}

    shutdowns: list[str] = []
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
        on_lost=lambda: shutdowns.append("requested"),
    )
    app = create_app(
        service=make_service(),
        agent=StubAgent(),
        api_token=operator_token,
        planning=None,
    )
    app.state.runtime_tenure_guard = guard
    assert guard.renew_once() is False

    response = TestClient(app).post(
        "/auth/login",
        json={"secret": operator_token},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_tenure_lost"
    assert shutdowns == ["requested"]


def test_inflight_route_loss_blocks_broker_submission_at_authoritative_seam():
    class Broker:
        reconciliation_key = "fake"

        def __init__(self):
            self.submissions = 0

        def submit_order(self, _request):
            self.submissions += 1

    broker = Broker()
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    guarded = TenureGuardedBroker(broker, guard)

    guard.ensure_owned()  # route/cycle admission happened while owned
    assert guard.renew_once() is False  # loss races after admission
    with pytest.raises(TenureLost):
        guarded.submit_order(object())

    assert broker.submissions == 0


def test_inflight_cycle_loss_blocks_authoritative_database_write(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path}/mutation-race.db")
    Base.metadata.create_all(engine)
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    install_runtime_mutation_barrier(engine, guard)

    guard.ensure_owned()  # core cycle entry happened while owned
    assert guard.renew_once() is False  # loss races before state write
    with pytest.raises(TenureLost):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO heartbeats (source, at) "
                "VALUES ('daemon', CURRENT_TIMESTAMP)"
            )

    with engine.connect() as connection:
        count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM heartbeats"
        ).scalar_one()
    assert count == 0


def test_sql_comment_cannot_impersonate_internal_tenure_renewal(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path}/mutation-comment.db")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO heartbeats (source, at) "
            "VALUES ('app', CURRENT_TIMESTAMP)"
        )
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    install_runtime_mutation_barrier(engine, guard)
    assert guard.renew_once() is False

    with pytest.raises(TenureLost):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE heartbeats SET at = CURRENT_TIMESTAMP "
                "WHERE source = 'app' "
                "/* runtime_tenures */"
            )


def test_exact_internal_tenure_statement_can_renew_through_barrier(
    tenure_service,
):
    service, _clock, _inspector = tenure_service
    handle = service.acquire_runtime("app", APP, ttl_seconds=30)
    engine = service._session_factory.kw["bind"]
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    install_runtime_mutation_barrier(engine, guard)

    assert guard.renew_once() is True
    guard.ensure_owned()
    assert guard.close() is True


def test_barrier_callback_registration_failure_removes_sql_listeners(
    tmp_path,
    monkeypatch,
):
    engine = create_db_engine(f"sqlite:///{tmp_path}/barrier-setup.db")
    Base.metadata.create_all(engine)
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    monkeypatch.setattr(
        guard,
        "add_close_callback",
        lambda _callback: (_ for _ in ()).throw(
            RuntimeError("callback-registration-failed")
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="callback-registration-failed",
    ):
        install_runtime_mutation_barrier(engine, guard)

    assert guard.renew_once() is False
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO heartbeats (source, at) "
            "VALUES ('app', CURRENT_TIMESTAMP)"
        )
    assert _heartbeat_count(engine) == 1


def test_partial_barrier_listener_failure_removes_prior_listeners(
    tmp_path,
    monkeypatch,
):
    import trading_assistant.ops.tenure as tenure_module

    engine = create_db_engine(f"sqlite:///{tmp_path}/partial-barrier.db")
    Base.metadata.create_all(engine)
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    original_listen = tenure_module.event.listen

    def fail_commit_listener(target, identifier, callback, *args, **kwargs):
        if identifier == "commit":
            raise RuntimeError("commit-listener-install-failed")
        return original_listen(
            target,
            identifier,
            callback,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(
        tenure_module.event,
        "listen",
        fail_commit_listener,
    )
    with pytest.raises(
        RuntimeError,
        match="commit-listener-install-failed",
    ):
        install_runtime_mutation_barrier(engine, guard)
    monkeypatch.setattr(tenure_module.event, "listen", original_listen)

    assert guard.renew_once() is False
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO heartbeats (source, at) "
            "VALUES ('app', CURRENT_TIMESTAMP)"
        )
    assert _heartbeat_count(engine) == 1


def _guarded_runtime_engine(tmp_path, filename: str):
    engine = create_db_engine(f"sqlite:///{tmp_path}/{filename}")
    Base.metadata.create_all(engine)
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    install_runtime_mutation_barrier(engine, guard)
    return engine, guard


def _heartbeat_count(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT COUNT(*) FROM heartbeats")
        ).scalar_one()


def test_core_transaction_commit_is_fenced_if_tenure_is_lost_after_dml(
    tmp_path,
):
    engine, guard = _guarded_runtime_engine(tmp_path, "core-commit.db")
    connection = engine.connect()
    transaction = connection.begin()
    connection.execute(
        text(
            "INSERT INTO heartbeats (source, at) "
            "VALUES ('app', CURRENT_TIMESTAMP)"
        )
    )

    assert guard.renew_once() is False
    with pytest.raises(TenureLost):
        transaction.commit()
    connection.close()

    assert _heartbeat_count(engine) == 0


def test_engine_begin_context_commit_is_fenced_after_tenure_loss(tmp_path):
    engine, guard = _guarded_runtime_engine(tmp_path, "context-commit.db")

    with pytest.raises(TenureLost):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO heartbeats (source, at) "
                    "VALUES ('app', CURRENT_TIMESTAMP)"
                )
            )
            assert guard.renew_once() is False

    assert _heartbeat_count(engine) == 0


def test_orm_commit_is_fenced_if_tenure_is_lost_after_flush(tmp_path):
    engine, guard = _guarded_runtime_engine(tmp_path, "orm-commit.db")
    session = Session(engine)
    session.execute(
        text(
            "INSERT INTO heartbeats (source, at) "
            "VALUES ('app', CURRENT_TIMESTAMP)"
        )
    )

    assert guard.renew_once() is False
    with pytest.raises(TenureLost):
        session.commit()
    session.close()

    assert _heartbeat_count(engine) == 0


def test_nested_transaction_release_is_fenced_and_outer_cannot_commit(
    tmp_path,
):
    engine, guard = _guarded_runtime_engine(tmp_path, "nested-commit.db")
    connection = engine.connect()
    outer = connection.begin()
    nested = connection.begin_nested()
    connection.execute(
        text(
            "INSERT INTO heartbeats (source, at) "
            "VALUES ('app', CURRENT_TIMESTAMP)"
        )
    )

    assert guard.renew_once() is False
    with pytest.raises(TenureLost):
        nested.commit()
    with pytest.raises((TenureLost, RuntimeError)):
        outer.commit()
    connection.close()

    assert _heartbeat_count(engine) == 0


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE escaped_authority (id INTEGER PRIMARY KEY)",
        "PRAGMA user_version = 42",
        "ATTACH DATABASE ':memory:' AS escaped_authority",
    ],
)
def test_non_dml_sql_mutations_are_fenced_after_tenure_loss(
    tmp_path,
    statement,
):
    engine, guard = _guarded_runtime_engine(tmp_path, "non-dml.db")
    assert guard.renew_once() is False

    with pytest.raises(TenureLost):
        with engine.begin() as connection:
            connection.exec_driver_sql(statement)


def test_successor_generation_fences_mutation_at_statement_source(
    tenure_service,
):
    service, clock, inspector = tenure_service
    predecessor = service.acquire_runtime("app", APP, ttl_seconds=30)
    guard = RuntimeTenureGuard(
        predecessor,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    engine = service._session_factory.kw["bind"]
    install_runtime_mutation_barrier(engine, guard)
    clock.advance(31)
    inspector.set(APP, ProcessProof.NOT_SAME)
    successor_engine = create_db_engine(
        engine.url.render_as_string(hide_password=False)
    )
    successor_service = RuntimeTenureService(
        make_session_factory(successor_engine),
        process_inspector=inspector,
        clock=clock,
    )
    successor_service.acquire_runtime(
        "app",
        ProcessIdentity(4111, "app-successor-start"),
        ttl_seconds=30,
    )

    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(TenureLost):
            connection.exec_driver_sql(
                "CREATE TABLE escaped_generation (id INTEGER PRIMARY KEY)"
            )
        transaction.rollback()

    with engine.connect() as connection:
        assert "escaped_generation" not in {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def test_process_inspector_uses_absolute_ps_even_with_path_hijack(
    monkeypatch,
):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="Sun Jul 27 20:00:00 2026\n",
            stderr="",
        )

    monkeypatch.setenv("PATH", "/tmp/attacker-controlled")
    inspector = LocalProcessInspector(
        runner=runner,
        process_probe=lambda _pid: None,
    )

    assert (
        _LOCAL_PROCESS_INSPECT(
            inspector,
            ProcessIdentity(APP.pid, CANONICAL_START),
        )
        is ProcessProof.SAME
    )
    assert calls[0][0][0] == "/bin/ps"
    assert calls[0][1]["env"] == {
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
    }


def test_process_inspector_is_stable_across_caller_timezone_and_locale(
    monkeypatch,
):
    observed_environments: list[dict[str, str]] = []

    def runner(_argv, **kwargs):
        observed_environments.append(kwargs["env"])
        return SimpleNamespace(
            returncode=0,
            stdout="Sun Jul 27 20:00:00 2026\n",
            stderr="",
        )

    inspector = LocalProcessInspector(
        runner=runner,
        process_probe=lambda _pid: None,
    )
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    first = _LOCAL_PROCESS_CURRENT(inspector)
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")

    assert _LOCAL_PROCESS_INSPECT(inspector, first) is ProcessProof.SAME
    assert first.start_identity == CANONICAL_START
    assert observed_environments == [
        {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
        {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
    ]


def test_process_inspector_treats_legacy_unversioned_identity_as_unknown():
    inspector = LocalProcessInspector(
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="Sun Jul 27 20:00:00 2026\n",
            stderr="",
        ),
        process_probe=lambda _pid: None,
    )

    assert _LOCAL_PROCESS_INSPECT(inspector, APP) is ProcessProof.UNKNOWN


def test_process_inspector_rc1_with_stderr_is_unknown():
    inspector = LocalProcessInspector(
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="permission denied",
        ),
        process_probe=lambda _pid: None,
    )

    assert _LOCAL_PROCESS_INSPECT(inspector, APP) is ProcessProof.UNKNOWN


def test_process_inspector_permission_error_is_unknown():
    def denied(_pid):
        raise PermissionError(errno.EPERM, "not permitted")

    inspector = LocalProcessInspector(
        runner=lambda *_args, **_kwargs: pytest.fail(
            "ps must not run after uncertain process probe"
        ),
        process_probe=denied,
    )

    assert _LOCAL_PROCESS_INSPECT(inspector, APP) is ProcessProof.UNKNOWN


def test_process_inspector_esrch_is_exact_absence_proof():
    def absent(_pid):
        raise ProcessLookupError(errno.ESRCH, "no such process")

    inspector = LocalProcessInspector(
        runner=lambda *_args, **_kwargs: pytest.fail(
            "ps must not run after exact absence proof"
        ),
        process_probe=absent,
    )

    assert _LOCAL_PROCESS_INSPECT(inspector, APP) is ProcessProof.NOT_SAME


def test_process_inspector_detects_pid_reuse_by_start_identity():
    inspector = LocalProcessInspector(
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="Sun Jul 27 12:00:01 2026\n",
            stderr="",
        ),
        process_probe=lambda _pid: None,
    )

    assert (
        _LOCAL_PROCESS_INSPECT(
            inspector,
            ProcessIdentity(APP.pid, CANONICAL_START),
        )
        is ProcessProof.NOT_SAME
    )


def test_expired_live_owner_with_canonical_identity_blocks_maintenance(
    tenure_service,
):
    service, clock, _inspector = tenure_service
    runner = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout="Sun Jul 27 20:00:00 2026\n",
        stderr="",
    )
    local = LocalProcessInspector(
        runner=runner,
        process_probe=lambda _pid: None,
    )
    service._process_inspector = SimpleNamespace(
        inspect=lambda identity: _LOCAL_PROCESS_INSPECT(local, identity)
    )
    live = ProcessIdentity(pid=APP.pid, start_identity=CANONICAL_START)
    service.acquire_runtime("app", live, ttl_seconds=10)
    clock.advance(11)

    with pytest.raises(TenureUnavailable) as captured:
        service.acquire_maintenance(MAINTENANCE, ttl_seconds=30)

    assert captured.value.stable_code == "runtime_process_live"


def test_guarded_broker_does_not_expose_raw_broker_or_unknown_methods():
    class Broker:
        reconciliation_key = "fake"

        def get_fill_activities(self, *, after=None):
            return [after]

        def raw_sdk_mutation(self):
            raise AssertionError("unguarded mutation escaped")

    broker = Broker()
    guard = RuntimeTenureGuard(
        FailingRenewHandle(),
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    guarded = TenureGuardedBroker(broker, guard)

    assert not hasattr(guarded, "_broker")
    assert guarded.get_fill_activities(after="cursor") == ["cursor"]
    with pytest.raises(AttributeError):
        guarded.raw_sdk_mutation()
