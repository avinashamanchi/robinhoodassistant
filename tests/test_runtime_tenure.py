"""Database-authoritative runtime and sensitive-maintenance tenure tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from trading_assistant.db.models import Base
from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.ops.tenure import (
    ProcessIdentity,
    ProcessProof,
    RuntimeTenureGuard,
    RuntimeTenureService,
    TenureGuardedBroker,
    TenureLost,
    TenureUncertain,
    TenureUnavailable,
    install_runtime_mutation_barrier,
)


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
APP = ProcessIdentity(pid=4101, start_identity="app-start-20260727T120000Z")
DAEMON = ProcessIdentity(
    pid=4102,
    start_identity="daemon-start-20260727T120000Z",
)
MCP = ProcessIdentity(
    pid=4104,
    start_identity="mcp-start-20260727T120000Z",
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


def test_app_rejects_mutations_immediately_after_tenure_loss(
    make_service,
    operator_token,
):
    from fastapi.testclient import TestClient

    from trading_assistant.app.main import create_app

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
