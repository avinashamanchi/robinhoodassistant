import json
import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic import command, op as alembic_op
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    BrokerFill,
    OrderResult,
    OrderStatus,
)
from trading_assistant.db.models import (
    Fill,
    Order,
    ReconciliationCursor,
    Rule,
    RuleGroup,
)
from trading_assistant.db.migrate import adopt_existing, upgrade
from trading_assistant.db.migration_authority import (
    issue_bootstrap_authority,
    issue_maintenance_downgrade_authority,
    issue_maintenance_authority,
)
from trading_assistant.db import migration_authority as authority_module
from trading_assistant.db.schema import SchemaOutOfDate, require_current_schema
from trading_assistant.db.session import (
    create_db_engine,
    make_session_factory,
)
from trading_assistant.orders.reconciliation import ReconciliationService
from trading_assistant.orders.repository import OrderRepository
from trading_assistant.ops.tenure import (
    ProcessIdentity,
    ProcessProof,
    RuntimeTenureGuard,
    RuntimeTenureHandle,
    RuntimeTenureService,
    TenureLost,
    TenureUnavailable,
    install_runtime_mutation_barrier,
)
from trading_assistant.security.crypto import (
    SensitiveDataCipher,
    SensitiveFieldRef,
)
from trading_assistant.security.sensitive_fields import bind_sensitive_cipher
from tests.safety_helpers import bootstrap_database_to_revision


MIGRATION_BACKUP_KEY = b"m" * 32
MIGRATION_BACKUP_KEY_ID = "migration-backup-key-2026"
_RAW_ALEMBIC_UPGRADE = command.upgrade
_RAW_ALEMBIC_DOWNGRADE = command.downgrade
_RAW_ALEMBIC_STAMP = command.stamp


def _migration_backup_args(tmp_path: Path) -> dict[str, object]:
    return {
        "backup_key": MIGRATION_BACKUP_KEY,
        "backup_key_id": MIGRATION_BACKUP_KEY_ID,
        "backup_directory": tmp_path / "encrypted-migration-backups",
    }


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


@contextmanager
def _held_migration_authority(
    engine,
    connection,
    *,
    pid: int,
    downgrade_to: str | None = None,
):
    service = RuntimeTenureService(
        make_session_factory(engine),
        process_inspector=_AbsentProcessInspector(),
    )
    handle = service.acquire_maintenance(
        ProcessIdentity(pid, f"migration-authority-{pid}"),
        ttl_seconds=30,
    )
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    guard.start()
    barrier = install_runtime_mutation_barrier(engine, guard)
    try:
        issuer = (
            issue_maintenance_authority(
                connection,
                guard=guard,
                barrier=barrier,
            )
            if downgrade_to is None
            else issue_maintenance_downgrade_authority(
                connection,
                downgrade_to,
                guard=guard,
                barrier=barrier,
            )
        )
        yield issuer, guard
    finally:
        if not guard.closed:
            guard.close()


class _AuthorizedTestAlembic:
    """Run historical revision tests under an isolated test-only authority."""

    class _HistoricalAuthority:
        def __init__(self, connection, operation, destination):
            self.connection = connection
            self.operation = operation
            self.destination = destination
            self.activated = False
            self.observed = False
            self.retired = False

    @staticmethod
    def _activate_historical(
        authority,
        connection,
        *,
        destination_revisions,
    ):
        destination = (
            destination_revisions
            if isinstance(destination_revisions, str)
            else tuple(destination_revisions or ())
        )
        if (
            type(authority)
            is not _AuthorizedTestAlembic._HistoricalAuthority
            or authority.connection is not connection
            or authority.destination != destination
            or authority.activated
            or authority.retired
        ):
            raise RuntimeError("schema_migration_authority_required")
        authority.activated = True
        return "maintenance"

    @staticmethod
    def _assert_historical(
        authority,
        connection,
        *,
        allowed_modes,
    ):
        del allowed_modes
        if (
            type(authority)
            is not _AuthorizedTestAlembic._HistoricalAuthority
            or authority.connection is not connection
            or not authority.activated
            or authority.retired
        ):
            raise RuntimeError("schema_migration_authority_required")
        return authority

    @staticmethod
    @contextmanager
    def _schema_fence_historical(authority, connection):
        if (
            type(authority)
            is not _AuthorizedTestAlembic._HistoricalAuthority
            or authority.connection is not connection
            or not authority.activated
            or authority.retired
        ):
            raise RuntimeError("schema_migration_authority_required")
        yield

    @staticmethod
    def _observe_historical(
        authority,
        connection,
        *,
        step,
        heads,
    ):
        del heads
        if (
            type(authority)
            is not _AuthorizedTestAlembic._HistoricalAuthority
            or authority.connection is not connection
            or not authority.activated
            or authority.retired
            or step.is_stamp != (authority.operation == "stamp")
            or (
                authority.operation == "upgrade"
                and not step.is_upgrade
            )
            or (
                authority.operation == "downgrade"
                and step.is_upgrade
            )
        ):
            raise RuntimeError("schema_migration_authority_required")
        authority.observed = True

    @staticmethod
    def _finish_historical(authority, connection):
        if (
            type(authority)
            is not _AuthorizedTestAlembic._HistoricalAuthority
            or authority.connection is not connection
            or not authority.activated
            or authority.retired
        ):
            raise RuntimeError("schema_migration_authority_required")

    @staticmethod
    def _retire_historical(authority) -> None:
        if type(authority) is _AuthorizedTestAlembic._HistoricalAuthority:
            authority.retired = True

    @classmethod
    def _run(cls, operation, cfg, *args, **kwargs):
        if (
            cfg.attributes.get("connection") is not None
            and cfg.attributes.get("migration_authority") is not None
        ):
            return operation(cfg, *args, **kwargs)
        engine = create_db_engine(
            cfg.get_main_option("sqlalchemy.url")
        )
        original = dict(cfg.attributes)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql(
                    "PRAGMA foreign_keys=OFF"
                )
                if connection.in_transaction():
                    connection.rollback()
                cfg.attributes["connection"] = connection
                operation_name = {
                    _RAW_ALEMBIC_UPGRADE: "upgrade",
                    _RAW_ALEMBIC_DOWNGRADE: "downgrade",
                    _RAW_ALEMBIC_STAMP: "stamp",
                }[operation]
                destination = ScriptDirectory.from_config(
                    cfg
                ).as_revision_number(args[0])
                # Historical tests intentionally build old schemas. This
                # capability exists only through process-local monkeypatches;
                # production exposes no Boolean or Config escape hatch.
                authority = cls._HistoricalAuthority(
                    connection,
                    operation_name,
                    destination,
                )
                cfg.attributes["migration_authority"] = authority
                revision = ScriptDirectory.from_config(cfg).get_revision(
                    "20260727_0015"
                )
                candidate_revision = ScriptDirectory.from_config(
                    cfg
                ).get_revision("20260728_0016")
                artifact_revision = ScriptDirectory.from_config(
                    cfg
                ).get_revision("20260729_0017")
                assert revision is not None
                assert candidate_revision is not None
                assert artifact_revision is not None
                revision_module = revision.module
                candidate_revision_module = candidate_revision.module
                artifact_revision_module = artifact_revision.module
                with (
                    patch.object(
                        authority_module,
                        "activate_migration_authority",
                        cls._activate_historical,
                    ),
                    patch.object(
                        authority_module,
                        "retire_migration_authority",
                        cls._retire_historical,
                    ),
                    patch.object(
                        authority_module,
                        "observe_migration_step",
                        cls._observe_historical,
                    ),
                    patch.object(
                        authority_module,
                        "finish_migration_authority",
                        cls._finish_historical,
                    ),
                    patch.object(
                        authority_module,
                        "assert_migration_authority",
                        cls._assert_historical,
                    ),
                    patch.object(
                        authority_module,
                        "migration_schema_fence",
                        cls._schema_fence_historical,
                    ),
                    patch.object(
                        revision_module,
                        "assert_migration_authority",
                        cls._assert_historical,
                    ),
                    patch.object(
                        revision_module,
                        "migration_schema_fence",
                        cls._schema_fence_historical,
                    ),
                    patch.object(
                        candidate_revision_module,
                        "assert_migration_authority",
                        cls._assert_historical,
                    ),
                    patch.object(
                        candidate_revision_module,
                        "migration_schema_fence",
                        cls._schema_fence_historical,
                    ),
                    patch.object(
                        artifact_revision_module,
                        "assert_migration_authority",
                        cls._assert_historical,
                    ),
                    patch.object(
                        artifact_revision_module,
                        "migration_schema_fence",
                        cls._schema_fence_historical,
                    ),
                ):
                    return operation(cfg, *args, **kwargs)
        finally:
            cfg.attributes.clear()
            cfg.attributes.update(original)
            engine.dispose()

    def upgrade(self, cfg, *args, **kwargs):
        return self._run(
            _RAW_ALEMBIC_UPGRADE,
            cfg,
            *args,
            **kwargs,
        )

    def downgrade(self, cfg, *args, **kwargs):
        return self._run(
            _RAW_ALEMBIC_DOWNGRADE,
            cfg,
            *args,
            **kwargs,
        )

    def stamp(self, cfg, *args, **kwargs):
        return self._run(
            _RAW_ALEMBIC_STAMP,
            cfg,
            *args,
            **kwargs,
        )


command = _AuthorizedTestAlembic()


def _legacy_engine(path: Path):
    bootstrap_database_to_revision(_url(path), "20260724_0001")
    engine = create_db_engine(_url(path))
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    return engine


def _engine_at_revision(path: Path, revision: str):
    bootstrap_database_to_revision(_url(path), revision)
    engine = create_db_engine(_url(path))
    direct_cfg = Config("alembic.ini")
    direct_cfg.set_main_option("sqlalchemy.url", _url(path))
    return engine, direct_cfg


def _database_fingerprint(engine) -> tuple[object, ...]:
    with engine.connect() as connection:
        version = connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        schema = tuple(
            connection.execute(
                text(
                    "SELECT type,name,coalesce(sql,'') "
                    "FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' "
                    "ORDER BY type,name"
                )
            )
        )
        tenures = (
            tuple(
                connection.execute(
                    text(
                        "SELECT resource_key,role,state,owner_id,generation "
                        "FROM runtime_tenures ORDER BY resource_key"
                    )
                )
            )
            if "runtime_tenures" in inspect(engine).get_table_names()
            else ()
        )
        plans = (
            tuple(
                connection.execute(
                    text("SELECT * FROM trade_plans ORDER BY id")
                )
            )
            if "trade_plans" in inspect(engine).get_table_names()
            else ()
        )
    return version, schema, tenures, plans


def _seed_migration_refusal_row(engine, marker: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(symbol,action,status,paper_only,shadow,plan_json,sized_json,"
                "entry_filled_qty,exit_filled_qty,residual_generation,"
                "created_at) VALUES "
                "('AAPL','hold','proposed',1,0,:marker,:marker,"
                "0,0,0,CURRENT_TIMESTAMP)"
            ),
            {"marker": marker},
        )


def _insert_legacy_rule(
    conn,
    *,
    rule_id: int,
    plan_id: int | None,
    state: str,
    pre_approved: bool = False,
    ticker: str = "AAPL",
    fraction=None,
    hwm=None,
    action: dict | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO rules "
            "(id,ticker,condition_json,action_json,state,created_at,plan_id,kind,"
            "fraction,hwm,deadline,pre_approved) "
            "VALUES (:id,:ticker,:condition,:action,:state,CURRENT_TIMESTAMP,"
            ":plan_id,'price',:fraction,:hwm,NULL,:pre_approved)"
        ),
        {
            "id": rule_id,
            "ticker": ticker,
            "condition": json.dumps({"price_below": 100 + rule_id}),
            "action": json.dumps(
                action
                if action is not None
                else {"side": "buy", "notional": "50"}
            ),
            "state": state,
            "plan_id": plan_id,
            "pre_approved": pre_approved,
            "fraction": fraction,
            "hwm": hwm,
        },
    )


def test_fresh_database_upgrades_to_head(tmp_path):
    engine = create_db_engine(_url(tmp_path / "fresh.db"))
    upgrade(engine)
    require_current_schema(engine)
    assert "orders" in inspect(engine).get_table_names()
    assert "reconciliation_cursors" in inspect(engine).get_table_names()
    assert (
        "startup_reconciliation_state"
        in inspect(engine).get_table_names()
    )
    assert "auth_sessions" in inspect(engine).get_table_names()
    assert {
        "rate_windows",
        "concurrency_leases",
        "provider_budget_days",
        "provider_reservations",
        "panic_receipts",
        "mutation_interlocks",
        "sensitive_migration_state",
        "candidate_nonces",
        "candidate_queue_receipts",
        "untrusted_ingest_events",
        "runtime_tenures",
    } <= set(inspect(engine).get_table_names())
    assert "alembic_version" in inspect(engine).get_table_names()
    rule_columns = {
        column["name"]
        for column in inspect(engine).get_columns("rules")
    }
    assert {"activation", "terminal_on_trigger"} <= rule_columns
    proposal_columns = {
        column["name"]
        for column in inspect(engine).get_columns("proposals")
    }
    assert {"source_rule_id", "plan_generation"} <= proposal_columns
    plan_columns = {
        column["name"]
        for column in inspect(engine).get_columns("trade_plans")
    }
    assert {
        "entry_filled_qty",
        "exit_filled_qty",
        "residual_generation",
        "authority_version",
        "authority_digest",
    } <= plan_columns
    order_columns = {
        column["name"]
        for column in inspect(engine).get_columns("orders")
    }
    assert "plan_cancel_state" in order_columns
    order_indexes = {
        index["name"]: index
        for index in inspect(engine).get_indexes("orders")
    }
    assert (
        order_indexes["ix_orders_plan_cancel_state"]["unique"]
        == 0
    )


class _AbsentProcessInspector:
    def inspect(self, _identity):
        return ProcessProof.NOT_SAME


def test_schema_upgrade_is_blocked_by_active_runtime_tenure(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.db import migrate as migrate_module

    engine = create_db_engine(_url(tmp_path / "runtime-blocks-upgrade.db"))
    upgrade(engine)
    factory = make_session_factory(engine)
    inspector = _AbsentProcessInspector()
    service = RuntimeTenureService(
        factory,
        process_inspector=inspector,
    )
    service.acquire_runtime(
        "app",
        ProcessIdentity(8001, "schema-upgrade-app"),
        ttl_seconds=30,
    )
    monkeypatch.setattr(
        migrate_module,
        "schema_status",
        lambda _engine: SimpleNamespace(
            versioned=True,
            ready=False,
            current="20260727_0014",
        ),
    )

    with pytest.raises(TenureUnavailable) as exc:
        upgrade(
            engine,
            **_migration_backup_args(tmp_path),
            process_identity=ProcessIdentity(
                8002,
                "schema-upgrade-maintenance",
            ),
            process_inspector=inspector,
        )

    assert exc.value.stable_code == "runtime_tenure_active"


def test_schema_upgrade_holds_maintenance_through_backup_and_ddl(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.db import migrate as migrate_module

    engine = create_db_engine(_url(tmp_path / "maintenance-upgrade.db"))
    upgrade(engine)
    factory = make_session_factory(engine)
    inspector = _AbsentProcessInspector()
    observed: list[str] = []
    backup_receipt = object()
    monkeypatch.setattr(
        migrate_module,
        "schema_status",
        lambda _engine: SimpleNamespace(
            versioned=True,
            ready=False,
            current="20260727_0014",
        ),
    )

    def backup_while_held(*_args, **_kwargs):
        contender = RuntimeTenureService(
            factory,
            process_inspector=inspector,
        )
        with pytest.raises(TenureUnavailable) as exc:
            contender.acquire_runtime(
                "daemon",
                ProcessIdentity(8011, "upgrade-backup-daemon"),
                ttl_seconds=30,
            )
        observed.append(exc.value.stable_code)
        return backup_receipt

    def ddl_while_held(*_args, **_kwargs):
        contender = RuntimeTenureService(
            factory,
            process_inspector=inspector,
        )
        with pytest.raises(TenureUnavailable) as exc:
            contender.acquire_runtime(
                "validation",
                ProcessIdentity(8012, "upgrade-ddl-validation"),
                ttl_seconds=30,
            )
        observed.append(exc.value.stable_code)

    monkeypatch.setattr(migrate_module, "_backup", backup_while_held)
    monkeypatch.setattr(migrate_module.command, "upgrade", ddl_while_held)

    receipt = upgrade(
        engine,
        **_migration_backup_args(tmp_path),
        process_identity=ProcessIdentity(
            8010,
            "schema-upgrade-maintenance",
        ),
        process_inspector=inspector,
    )

    assert receipt is backup_receipt
    assert observed == [
        "maintenance_tenure_active",
        "maintenance_tenure_active",
    ]
    successor = RuntimeTenureService(
        factory,
        process_inspector=inspector,
    ).acquire_runtime(
        "app",
        ProcessIdentity(8013, "post-upgrade-app"),
        ttl_seconds=30,
    )
    assert successor.role == "app"


def test_schema_upgrade_starts_renewal_only_after_snapshot_closes(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.db import migrate as migrate_module

    engine = create_db_engine(_url(tmp_path / "ordered-upgrade.db"))
    upgrade(engine)
    starts: list[str] = []
    backup_receipt = object()

    monkeypatch.setattr(
        migrate_module,
        "schema_status",
        lambda _engine: SimpleNamespace(
            versioned=True,
            ready=False,
            current="20260727_0014",
        ),
    )
    monkeypatch.setattr(
        RuntimeTenureGuard,
        "start",
        lambda _guard: starts.append("started"),
    )

    def snapshot_probe(*_args, **kwargs):
        assert starts == []
        assert "ensure_maintenance" not in kwargs
        maintenance = kwargs["maintenance"]
        maintenance.check_snapshot()
        assert starts == []
        maintenance.complete_snapshot()
        assert starts == ["started"]
        return backup_receipt

    monkeypatch.setattr(migrate_module, "_backup", snapshot_probe)
    monkeypatch.setattr(
        migrate_module.command,
        "upgrade",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop-after-backup")
        ),
    )

    with pytest.raises(RuntimeError, match="stop-after-backup"):
        upgrade(
            engine,
            **_migration_backup_args(tmp_path),
            process_identity=ProcessIdentity(
                8015,
                "schema-ordered-maintenance",
            ),
            process_inspector=_AbsentProcessInspector(),
        )


def test_schema_upgrade_ddl_is_source_fenced_after_successor_generation(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.db import migrate as migrate_module

    engine = create_db_engine(_url(tmp_path / "fenced-schema-upgrade.db"))
    upgrade(engine)
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

    class Clock:
        value = now

        def __call__(self):
            return self.value

    clock = Clock()
    inspector = _AbsentProcessInspector()
    monkeypatch.setattr(
        migrate_module,
        "schema_status",
        lambda _engine: SimpleNamespace(
            versioned=True,
            ready=False,
            current="20260727_0014",
        ),
    )
    monkeypatch.setattr(
        migrate_module,
        "_backup",
        lambda *_args, **_kwargs: object(),
    )

    def successor_wins_before_ddl(config, _revision):
        clock.value = now.replace(second=31)
        successor_engine = create_db_engine(
            engine.url.render_as_string(hide_password=False)
        )
        RuntimeTenureService(
            make_session_factory(successor_engine),
            process_inspector=inspector,
            clock=clock,
        ).acquire_maintenance(
            ProcessIdentity(8021, "schema-successor-maintenance"),
            ttl_seconds=30,
        )
        connection = config.attributes["connection"]
        connection.exec_driver_sql(
            "CREATE TABLE escaped_schema_generation "
            "(id INTEGER PRIMARY KEY)"
        )

    monkeypatch.setattr(
        migrate_module.command,
        "upgrade",
        successor_wins_before_ddl,
    )

    with pytest.raises(TenureLost):
        upgrade(
            engine,
            **_migration_backup_args(tmp_path),
            process_identity=ProcessIdentity(
                8020,
                "schema-predecessor-maintenance",
            ),
            process_inspector=inspector,
            tenure_clock=clock,
        )

    assert "escaped_schema_generation" not in inspect(
        engine
    ).get_table_names()


def test_schema_upgrade_barrier_setup_failure_releases_maintenance(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.db import migrate as migrate_module

    engine = create_db_engine(_url(tmp_path / "schema-barrier-failure.db"))
    upgrade(engine)
    captured = []
    monkeypatch.setattr(
        migrate_module,
        "schema_status",
        lambda _engine: SimpleNamespace(
            versioned=True,
            ready=False,
            current="20260727_0014",
        ),
    )

    def fail_barrier(_engine, guard):
        captured.append(guard)
        raise RuntimeError("schema-barrier-install-failed")

    monkeypatch.setattr(
        migrate_module,
        "install_runtime_mutation_barrier",
        fail_barrier,
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="schema-barrier-install-failed",
        ):
            upgrade(
                engine,
                **_migration_backup_args(tmp_path),
                process_identity=ProcessIdentity(
                    8030,
                    "schema-barrier-maintenance",
                ),
                process_inspector=_AbsentProcessInspector(),
            )

        assert captured[0].closed
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT state FROM runtime_tenures "
                    "WHERE resource_key='sensitive-migration:global'"
                )
            ) == "released"
    finally:
        if captured and not captured[0].closed:
            captured[0].close()


def test_runtime_tenure_migration_is_successor_0014():
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    sensitive_revision = script.get_revision("20260727_0013")
    tenure_revision = script.get_revision("20260727_0014")

    assert script.get_current_head() == "20260730_0018"
    assert sensitive_revision is not None
    assert sensitive_revision.down_revision == "20260727_0012"
    assert sensitive_revision.path.endswith(
        "20260727_0013_sensitive_trust_state.py"
    )
    assert tenure_revision is not None
    assert tenure_revision.down_revision == "20260727_0013"
    assert tenure_revision.path.endswith(
        "20260727_0014_runtime_tenures.py"
    )


def test_plan_authority_migration_is_successor_0015():
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    authority_revision = script.get_revision("20260727_0015")

    assert script.get_current_head() == "20260730_0018"
    assert authority_revision is not None
    assert authority_revision.down_revision == "20260727_0014"
    assert authority_revision.path.endswith(
        "20260727_0015_plan_authority_and_validation_tenure.py"
    )


def test_candidate_queue_receipt_migration_is_successor_0016():
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    receipt_revision = script.get_revision("20260728_0016")

    assert script.get_current_head() == "20260730_0018"
    assert receipt_revision is not None
    assert receipt_revision.down_revision == "20260727_0015"
    assert receipt_revision.path.endswith(
        "20260728_0016_candidate_queue_receipts.py"
    )


def test_backtest_artifact_migration_is_successor_0017():
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    artifact_revision = script.get_revision("20260729_0017")

    assert script.get_current_head() == "20260730_0018"
    assert artifact_revision is not None
    assert artifact_revision.down_revision == "20260728_0016"
    assert artifact_revision.path.endswith(
        "20260729_0017_backtest_artifacts.py"
    )


def test_rule_cancel_interlock_migration_is_successor_0018():
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    rule_cancel_revision = script.get_revision("20260730_0018")

    assert script.get_current_head() == "20260730_0018"
    assert rule_cancel_revision is not None
    assert rule_cancel_revision.down_revision == "20260729_0017"
    assert rule_cancel_revision.path.endswith(
        "20260730_0018_rule_cancel_interlock.py"
    )


def test_rule_cancel_interlock_downgrade_refuses_durable_state(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "rule-cancel-interlock-downgrade.db",
        "head",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mutation_interlocks "
                "(resource_key,owner,generation,operation,state,outcome_code,"
                "created_at,updated_at) VALUES "
                "('route:rule-cancel:1','operator:local',1,'rule_cancel',"
                "'active','',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="^rule_cancel_interlock_downgrade_blocked$",
    ):
        command.downgrade(cfg, "20260729_0017")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260730_0018"
        assert connection.scalar(
            text("SELECT count(*) FROM mutation_interlocks")
        ) == 1


def test_rule_cancel_interlock_clean_downgrade_restores_old_constraint(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "rule-cancel-clean-downgrade.db",
        "head",
    )

    command.downgrade(cfg, "20260729_0017")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260729_0017"
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO mutation_interlocks "
                    "(resource_key,owner,generation,operation,state,outcome_code,"
                    "created_at,updated_at) VALUES "
                    "('route:rule-cancel:1','operator:local',1,'rule_cancel',"
                    "'active','',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                )
            )


def test_rule_cancel_interlock_downgrade_lock_failure_refuses_before_ddl(
    tmp_path,
):
    database_path = tmp_path / "rule-cancel-downgrade-lock-failure.db"
    engine, cfg = _engine_at_revision(database_path, "head")
    cfg.set_main_option(
        "sqlalchemy.url",
        f"{_url(database_path)}?timeout=0.05",
    )

    with engine.connect() as blocker:
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        blocker.execute(
            text(
                "INSERT INTO mutation_interlocks "
                "(resource_key,owner,generation,operation,state,outcome_code,"
                "created_at,updated_at) VALUES "
                "('route:rule-cancel:lock','operator:local',1,'rule_cancel',"
                "'active','',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        with pytest.raises(
            RuntimeError,
            match="^runtime_tenure_downgrade_blocked$",
        ):
            command.downgrade(cfg, "20260729_0017")
        blocker.commit()

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260730_0018"
        assert connection.scalar(
            text("SELECT count(*) FROM mutation_interlocks")
        ) == 1


def test_backtest_artifact_schema_is_bounded_and_run_scoped(tmp_path):
    engine, _cfg = _engine_at_revision(
        tmp_path / "backtest-artifact-schema.db",
        "head",
    )
    inspector = inspect(engine)

    columns = {
        column["name"]: column
        for column in inspector.get_columns("backtest_artifacts")
    }
    assert set(columns) == {
        "id",
        "run_id",
        "artifact_key",
        "schema_version",
        "payload_json",
        "created_at",
    }
    assert columns["run_id"]["nullable"] is False
    assert columns["artifact_key"]["type"].length == 160
    assert columns["schema_version"]["nullable"] is False
    assert columns["payload_json"]["nullable"] is False
    assert {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "backtest_artifacts"
        )
    } == {("run_id", "artifact_key")}
    assert {
        tuple(index["column_names"])
        for index in inspector.get_indexes("backtest_artifacts")
    } == {("run_id",)}
    assert {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
        )
        for foreign_key in inspector.get_foreign_keys(
            "backtest_artifacts"
        )
    } == {(("run_id",), "backtest_runs", ("id",))}


def test_candidate_queue_receipt_downgrade_refuses_durable_state(
    tmp_path,
):
    database_path = tmp_path / "candidate-receipt-downgrade.db"
    engine, cfg = _engine_at_revision(database_path, "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO candidate_queue_receipts "
                "(session_binding_hash,actor_hash,kind,"
                "idempotency_key_hash,candidate_hash,reason_hash,nonce_hash,"
                "state,outcome_code,target_id,http_status,request_id,"
                "created_at,updated_at,completed_at) VALUES "
                "(:session_hash,:actor_hash,'order',:idempotency_hash,"
                ":candidate_hash,:reason_hash,:nonce_hash,'completed',"
                "'candidate_expired',NULL,409,'candidate-downgrade',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {
                "session_hash": "a" * 64,
                "actor_hash": "b" * 64,
                "idempotency_hash": "c" * 64,
                "candidate_hash": "d" * 64,
                "reason_hash": "f" * 64,
                "nonce_hash": "e" * 64,
            },
        )

    with pytest.raises(
        RuntimeError,
        match="^candidate_queue_receipt_downgrade_blocked$",
    ):
        command.downgrade(cfg, "20260727_0015")

    assert "candidate_queue_receipts" in inspect(engine).get_table_names()
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM candidate_queue_receipts"))
    command.downgrade(cfg, "20260727_0015")
    assert "candidate_queue_receipts" not in inspect(engine).get_table_names()


def test_backtest_artifact_downgrade_refuses_durable_state(tmp_path):
    database_path = tmp_path / "backtest-artifact-downgrade.db"
    engine, cfg = _engine_at_revision(database_path, "head")
    with engine.begin() as connection:
        run_id = connection.execute(
            text(
                "INSERT INTO backtest_runs "
                "(label, config_json, created_at) VALUES "
                "('artifact downgrade', '{}', CURRENT_TIMESTAMP)"
            )
        ).lastrowid
        connection.execute(
            text(
                "INSERT INTO backtest_artifacts "
                "(run_id, artifact_key, schema_version, payload_json, "
                "created_at) VALUES "
                "(:run_id, 'manifest', 1, '{}', CURRENT_TIMESTAMP)"
            ),
            {"run_id": run_id},
        )

    with pytest.raises(
        RuntimeError,
        match="^backtest_artifact_downgrade_blocked$",
    ):
        command.downgrade(cfg, "20260728_0016")

    assert "backtest_artifacts" in inspect(engine).get_table_names()
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM backtest_artifacts"))
    command.downgrade(cfg, "20260728_0016")
    assert "backtest_artifacts" not in inspect(engine).get_table_names()


@pytest.mark.parametrize(
    "role",
    [None, "app", "daemon", "mcp", "maintenance"],
)
def test_direct_alembic_upgrade_from_0014_requires_opaque_authority(
    tmp_path,
    role,
):
    path = tmp_path / f"direct-upgrade-{role or 'none'}.db"
    engine, cfg = _engine_at_revision(path, "20260727_0014")
    _seed_migration_refusal_row(engine, f"upgrade-{role or 'none'}")
    if role is not None:
        service = RuntimeTenureService(
            make_session_factory(engine),
            process_inspector=_AbsentProcessInspector(),
        )
        identity = ProcessIdentity(
            8400 + len(role),
            f"direct-upgrade-{role}",
        )
        if role == "maintenance":
            service.acquire_maintenance(identity, ttl_seconds=30)
        else:
            service.acquire_runtime(role, identity, ttl_seconds=30)
    before = _database_fingerprint(engine)
    artifact_directory = tmp_path / "direct-upgrade-artifacts"
    artifact_directory.mkdir()
    existing_artifact = artifact_directory / "existing.aesgcm"
    existing_artifact.write_bytes(b"unchanged")
    artifacts_before = tuple(
        (path.name, path.read_bytes())
        for path in sorted(artifact_directory.iterdir())
    )

    with pytest.raises(
        RuntimeError,
        match="^schema_migration_authority_required$",
    ):
        _RAW_ALEMBIC_UPGRADE(cfg, "head")

    assert _database_fingerprint(engine) == before
    assert tuple(
        (path.name, path.read_bytes())
        for path in sorted(artifact_directory.iterdir())
    ) == artifacts_before


@pytest.mark.parametrize(
    "role",
    [None, "app", "daemon", "mcp", "validation", "maintenance"],
)
def test_direct_alembic_head_upgrade_requires_opaque_authority(
    tmp_path,
    role,
):
    path = tmp_path / f"direct-head-upgrade-{role or 'none'}.db"
    engine = create_db_engine(_url(path))
    assert upgrade(engine) is None
    _seed_migration_refusal_row(engine, f"head-{role or 'none'}")
    if role is not None:
        service = RuntimeTenureService(
            make_session_factory(engine),
            process_inspector=_AbsentProcessInspector(),
        )
        identity = ProcessIdentity(
            8500 + len(role),
            f"direct-head-upgrade-{role}",
        )
        if role == "maintenance":
            service.acquire_maintenance(identity, ttl_seconds=30)
        else:
            service.acquire_runtime(role, identity, ttl_seconds=30)
    before = _database_fingerprint(engine)
    artifact_directory = tmp_path / "direct-head-upgrade-artifacts"
    assert not artifact_directory.exists()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with pytest.raises(
        RuntimeError,
        match="^schema_migration_authority_required$",
    ):
        _RAW_ALEMBIC_UPGRADE(cfg, "head")

    assert _database_fingerprint(engine) == before
    assert not artifact_directory.exists()


@pytest.mark.parametrize(
    "role",
    [None, "app", "daemon", "mcp", "validation", "maintenance"],
)
def test_direct_alembic_downgrade_requires_opaque_authority(
    tmp_path,
    role,
):
    path = tmp_path / f"direct-downgrade-{role or 'none'}.db"
    engine = create_db_engine(_url(path))
    assert upgrade(engine) is None
    _seed_migration_refusal_row(engine, f"downgrade-{role or 'none'}")
    if role is not None:
        service = RuntimeTenureService(
            make_session_factory(engine),
            process_inspector=_AbsentProcessInspector(),
        )
        identity = ProcessIdentity(
            8600 + len(role),
            f"direct-downgrade-{role}",
        )
        if role == "maintenance":
            service.acquire_maintenance(identity, ttl_seconds=30)
        else:
            service.acquire_runtime(role, identity, ttl_seconds=30)
    before = _database_fingerprint(engine)
    artifact_directory = tmp_path / "direct-downgrade-artifacts"
    assert not artifact_directory.exists()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with pytest.raises(
        RuntimeError,
        match="^schema_migration_authority_required$",
    ):
        _RAW_ALEMBIC_DOWNGRADE(cfg, "20260727_0014")

    assert _database_fingerprint(engine) == before
    assert not artifact_directory.exists()


def test_offline_alembic_migration_is_refused_before_sql_generation(
    tmp_path,
):
    path = tmp_path / "offline-refused.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with pytest.raises(
        RuntimeError,
        match="^schema_migration_offline_refused$",
    ):
        _RAW_ALEMBIC_UPGRADE(cfg, "head", sql=True)

    assert not path.exists()


def test_forged_maintenance_authority_cannot_upgrade_without_held_tenure(
    tmp_path,
):
    path = tmp_path / "forged-maintenance-upgrade.db"
    engine, _cfg = _engine_at_revision(path, "20260727_0014")
    _seed_migration_refusal_row(engine, "forged-upgrade")
    before = _database_fingerprint(engine)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with engine.connect() as connection:
        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            cfg.attributes["connection"] = connection
            cfg.attributes["migration_authority"] = (
                issue_maintenance_authority(connection)
            )
            cfg.attributes["runtime_tenure_fence_schema"] = (
                "attacker-controlled-fence",
                object(),
            )
            cfg.attributes["runtime_tenure_assert_owned"] = (
                lambda _connection: None
            )
            _RAW_ALEMBIC_UPGRADE(cfg, "head")

    assert _database_fingerprint(engine) == before


def test_forged_maintenance_authority_cannot_downgrade_without_held_tenure(
    tmp_path,
):
    path = tmp_path / "forged-maintenance-downgrade.db"
    engine = create_db_engine(_url(path))
    assert upgrade(engine) is None
    _seed_migration_refusal_row(engine, "forged-downgrade")
    before = _database_fingerprint(engine)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with engine.connect() as connection:
        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            cfg.attributes["connection"] = connection
            cfg.attributes["migration_authority"] = (
                issue_maintenance_authority(connection)
            )
            cfg.attributes["runtime_tenure_fence_schema"] = (
                "attacker-controlled-fence",
                object(),
            )
            cfg.attributes["runtime_tenure_assert_owned"] = (
                lambda _connection: None
            )
            _RAW_ALEMBIC_DOWNGRADE(cfg, "20260727_0014")

    assert _database_fingerprint(engine) == before


def test_forged_handle_assertion_cannot_mint_maintenance_authority(
    tmp_path,
):
    path = tmp_path / "forged-handle-authority.db"
    engine, _cfg = _engine_at_revision(path, "20260727_0014")

    class ForgedService:
        _internal_capability = object()

        def _assert_owned(self, *_args, **_kwargs) -> None:
            return None

    handle = RuntimeTenureHandle(
        _service=ForgedService(),
        resource_key="sensitive-migration:global",
        role="maintenance",
        owner_id="00000000-0000-4000-8000-000000000099",
        generation=99,
        identity=ProcessIdentity(8799, "forged-maintenance-handle"),
        expires_at=datetime.max.replace(tzinfo=timezone.utc),
    )
    guard = RuntimeTenureGuard(
        handle,
        ttl_seconds=30,
        renewal_interval_seconds=5,
    )
    barrier = install_runtime_mutation_barrier(engine, guard)
    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                issue_maintenance_authority(
                    connection,
                    guard=guard,
                    barrier=barrier,
                )
    finally:
        barrier.close()

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM runtime_tenures "
                "WHERE resource_key='sensitive-migration:global'"
            )
        ) == 0


def test_bootstrap_authority_revalidates_empty_database_at_use_and_is_spent(
    tmp_path,
):
    path = tmp_path / "bootstrap-issue-then-create.db"
    engine = create_db_engine(_url(path))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with engine.connect() as connection:
        authority = issue_bootstrap_authority(connection)
        connection.execute(
            text(
                "CREATE TABLE injected_before_bootstrap "
                "(id INTEGER PRIMARY KEY, marker TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO injected_before_bootstrap(id,marker) "
                "VALUES (1,'must-survive')"
            )
        )
        connection.commit()
        cfg.attributes["connection"] = connection
        cfg.attributes["migration_authority"] = authority

        with pytest.raises(
            RuntimeError,
            match="^schema_bootstrap_authority_refused$",
        ):
            _RAW_ALEMBIC_UPGRADE(cfg, "head")

        assert connection.execute(
            text(
                "SELECT id,marker FROM injected_before_bootstrap"
            )
        ).one() == (1, "must-survive")
        assert "alembic_version" not in inspect(connection).get_table_names()
        connection.rollback()
        connection.execute(text("DROP TABLE injected_before_bootstrap"))
        connection.commit()

        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            _RAW_ALEMBIC_UPGRADE(cfg, "head")

    assert inspect(engine).get_table_names() == []


def test_bootstrap_authority_rejects_connection_misuse_and_replay(
    tmp_path,
):
    path = tmp_path / "bootstrap-connection-and-replay.db"
    engine = create_db_engine(_url(path))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with engine.connect() as issued_connection:
        authority = issue_bootstrap_authority(issued_connection)
        with engine.connect() as wrong_connection:
            cfg.attributes["connection"] = wrong_connection
            cfg.attributes["migration_authority"] = authority
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                _RAW_ALEMBIC_UPGRADE(cfg, "head")

        cfg.attributes["connection"] = issued_connection
        cfg.attributes["migration_authority"] = authority
        with pytest.raises(
            RuntimeError,
            match="^schema_migration_authority_required$",
        ):
            _RAW_ALEMBIC_UPGRADE(cfg, "head")

    assert inspect(engine).get_table_names() == []


def test_maintenance_authority_rejects_connection_misuse_and_is_spent(
    tmp_path,
):
    path = tmp_path / "maintenance-connection-misuse.db"
    engine, _cfg = _engine_at_revision(path, "20260727_0014")
    before = _database_fingerprint(engine)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with engine.connect() as issued_connection:
        with _held_migration_authority(
            engine,
            issued_connection,
            pid=8701,
        ) as (authority, _guard):
            with engine.connect() as wrong_connection:
                cfg.attributes["connection"] = wrong_connection
                cfg.attributes["migration_authority"] = authority
                with pytest.raises(
                    RuntimeError,
                    match="^schema_migration_authority_required$",
                ):
                    _RAW_ALEMBIC_UPGRADE(cfg, "head")

            cfg.attributes["connection"] = issued_connection
            cfg.attributes["migration_authority"] = authority
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                _RAW_ALEMBIC_UPGRADE(cfg, "head")

    after = _database_fingerprint(engine)
    assert (after[0], after[1], after[3]) == (
        before[0],
        before[1],
        before[3],
    )


def test_maintenance_authority_replay_fails_after_successful_upgrade(
    tmp_path,
):
    path = tmp_path / "maintenance-replay.db"
    engine, _cfg = _engine_at_revision(path, "20260727_0014")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with engine.connect() as connection:
        with _held_migration_authority(
            engine,
            connection,
            pid=8702,
        ) as (authority, _guard):
            cfg.attributes["connection"] = connection
            cfg.attributes["migration_authority"] = authority
            _RAW_ALEMBIC_UPGRADE(cfg, "head")
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                _RAW_ALEMBIC_UPGRADE(cfg, "head")

    require_current_schema(engine)


def test_maintenance_authority_revalidates_lease_after_issuance(
    tmp_path,
):
    path = tmp_path / "maintenance-released-before-ddl.db"
    engine, _cfg = _engine_at_revision(path, "20260727_0014")
    before = _database_fingerprint(engine)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with engine.connect() as connection:
        with _held_migration_authority(
            engine,
            connection,
            pid=8703,
        ) as (authority, guard):
            assert guard.close() is True
            cfg.attributes["connection"] = connection
            cfg.attributes["migration_authority"] = authority
            with pytest.raises(
                RuntimeError,
                match="^schema_migration_authority_required$",
            ):
                _RAW_ALEMBIC_UPGRADE(cfg, "head")

    after = _database_fingerprint(engine)
    assert (after[0], after[1], after[3]) == (
        before[0],
        before[1],
        before[3],
    )


def test_held_maintenance_authority_supports_fenced_downgrade(
    tmp_path,
):
    path = tmp_path / "held-maintenance-downgrade.db"
    engine = create_db_engine(_url(path))
    assert upgrade(engine) is None
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))

    with engine.connect() as connection:
        with _held_migration_authority(
            engine,
            connection,
            pid=8704,
            downgrade_to="20260727_0014",
        ) as (authority, _guard):
            cfg.attributes["connection"] = connection
            cfg.attributes["migration_authority"] = authority
            _RAW_ALEMBIC_DOWNGRADE(cfg, "20260727_0014")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0014"
    assert {
        column["name"]
        for column in inspect(engine).get_columns("trade_plans")
    }.isdisjoint({"authority_version", "authority_digest"})


def test_plan_authority_upgrade_preserves_legacy_rows_under_maintenance(
    tmp_path,
):
    engine, _cfg = _engine_at_revision(
        tmp_path / "plan-authority-upgrade.db",
        "20260727_0014",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(symbol,action,status,paper_only,shadow,plan_json,sized_json,"
                "entry_filled_qty,exit_filled_qty,residual_generation,"
                "created_at) VALUES "
                "('AAPL','hold','proposed',1,0,'legacy-plan','legacy-size',"
                "0,0,0,CURRENT_TIMESTAMP)"
            )
        )

    backup = upgrade(
        engine,
        **_migration_backup_args(tmp_path),
        process_identity=ProcessIdentity(
            8100,
            "plan-authority-schema-upgrade",
        ),
        process_inspector=_AbsentProcessInspector(),
    )

    assert backup is not None and backup.verified is True
    with engine.begin() as connection:
        assert connection.execute(
            text(
                "SELECT plan_json,sized_json,authority_version,"
                "authority_digest FROM trade_plans"
            )
        ).one() == ("legacy-plan", "legacy-size", 0, None)
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE trade_plans SET "
                    "authority_version=1,authority_digest='bad'"
                )
            )

    validation = RuntimeTenureService(
        make_session_factory(engine),
        process_inspector=_AbsentProcessInspector(),
    ).acquire_runtime(
        "validation",
        ProcessIdentity(8101, "validation-after-schema-upgrade"),
        ttl_seconds=30,
    )
    assert validation.role == "validation"


def test_runtime_tenure_schema_rejects_invalid_resource_role(tmp_path):
    engine, _cfg = _engine_at_revision(
        tmp_path / "runtime-tenures-schema.db",
        "head",
    )
    inspector = inspect(engine)

    assert {
        "resource_key",
        "role",
        "state",
        "owner_id",
        "generation",
        "pid",
        "process_start_identity",
        "acquired_at",
        "renewed_at",
        "expires_at",
        "released_at",
    } == {
        column["name"]
        for column in inspector.get_columns("runtime_tenures")
    }

    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO runtime_tenures "
                    "(resource_key,role,state,owner_id,generation,pid,"
                    "process_start_identity,acquired_at,renewed_at,"
                    "expires_at,released_at) VALUES "
                    "('runtime:app','daemon','held',"
                    "'00000000-0000-4000-8000-000000000001',1,42,"
                    "'stable-start','2026-07-27 12:00:00',"
                    "'2026-07-27 12:00:00','2026-07-27 12:01:00',NULL)"
                )
            )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_tenures "
                "(resource_key,role,state,owner_id,generation,pid,"
                "process_start_identity,acquired_at,renewed_at,"
                "expires_at,released_at) VALUES "
                "('runtime:mcp','mcp','held',"
                "'00000000-0000-4000-8000-000000000002',1,43,"
                "'mcp-stable-start','2026-07-27 12:00:00',"
                "'2026-07-27 12:00:00','2026-07-27 12:01:00',NULL)"
            )
        )
        assert connection.scalar(
            text(
                "SELECT count(*) FROM runtime_tenures "
                "WHERE resource_key='runtime:mcp' AND role='mcp'"
            )
        ) == 1
        connection.execute(
            text(
                "INSERT INTO runtime_tenures "
                "(resource_key,role,state,owner_id,generation,pid,"
                "process_start_identity,acquired_at,renewed_at,"
                "expires_at,released_at) VALUES "
                "('runtime:validation','validation','held',"
                "'00000000-0000-4000-8000-000000000003',1,44,"
                "'validation-stable-start','2026-07-27 12:00:00',"
                "'2026-07-27 12:00:00','2026-07-27 12:01:00',NULL)"
            )
        )


def test_runtime_tenure_schema_preserves_fenced_reclaim_distinction(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "runtime-tenures-fenced-state.db",
        "head",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_tenures "
                "(resource_key,role,state,owner_id,generation,pid,"
                "process_start_identity,acquired_at,renewed_at,"
                "expires_at,released_at) VALUES "
                "('runtime:app','app','fenced',"
                "'00000000-0000-4000-8000-000000000004',2,45,"
                "'fenced-app-start','2026-07-27 12:00:00',"
                "'2026-07-27 12:01:00','2026-07-27 12:01:00',"
                "'2026-07-27 12:01:00')"
            )
        )

    engine.dispose()
    command.downgrade(cfg, "20260727_0014")
    downgraded = create_db_engine(
        _url(tmp_path / "runtime-tenures-fenced-state.db")
    )
    with downgraded.begin() as connection:
        assert connection.scalar(
            text(
                "SELECT state FROM runtime_tenures "
                "WHERE resource_key='runtime:app'"
            )
        ) == "released"
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE runtime_tenures SET state='fenced' "
                    "WHERE resource_key='runtime:app'"
                )
            )


def test_sensitive_trust_schema_constraints_indexes_and_no_raw_text(tmp_path):
    engine, _cfg = _engine_at_revision(
        tmp_path / "sensitive-trust-schema.db",
        "head",
    )
    inspector = inspect(engine)
    sensitive_fields = {
        "orders": {"approval_reason"},
        "audit_events": {"reason", "detail_json"},
        "proposals": {"reasoning"},
        "llm_decisions": {"prompt", "tool_calls_json", "reasoning_summary"},
        "risk_events": {"reason"},
        "analysis_reports": {"report_json"},
        "trade_plans": {"plan_json", "sized_json"},
        "circuit_breaker_state": {"reason"},
        "startup_reconciliation_state": {"reason", "evidence_json"},
        "panic_receipts": {"response_json"},
    }
    for table_name, field_names in sensitive_fields.items():
        nullability = {
            column["name"]: column["nullable"]
            for column in inspector.get_columns(table_name)
        }
        assert all(not nullability[field] for field in field_names if not (
            table_name == "panic_receipts" and field == "response_json"
        ))
        if table_name == "panic_receipts":
            assert nullability["response_json"] is True

    ingest_columns = {
        column["name"]
        for column in inspector.get_columns("untrusted_ingest_events")
    }
    assert ingest_columns == {
        "id",
        "source_hash",
        "content_hash",
        "byte_length",
        "flags_json",
        "state",
        "received_at",
        "summary_decision_id",
    }
    assert not ingest_columns & {
        "raw_text",
        "raw_content",
        "content",
        "prompt",
        "response",
    }
    assert {
        index["name"]
        for index in inspector.get_indexes("candidate_nonces")
    } >= {
        "ix_candidate_nonces_expires_at",
        "ix_candidate_nonces_request_id",
        "ix_candidate_nonces_consumed_at",
    }
    receipt_columns = {
        column["name"]
        for column in inspector.get_columns("candidate_queue_receipts")
    }
    assert receipt_columns == {
        "id",
        "session_binding_hash",
        "actor_hash",
        "kind",
        "idempotency_key_hash",
        "candidate_hash",
        "reason_hash",
        "nonce_hash",
        "state",
        "outcome_code",
        "target_id",
        "http_status",
        "request_id",
        "created_at",
        "updated_at",
        "completed_at",
    }
    assert not receipt_columns & {
        "candidate",
        "payload",
        "thesis",
        "narrative",
        "token",
        "idempotency_key",
    }
    assert {
        index["name"]
        for index in inspector.get_indexes(
            "candidate_queue_receipts"
        )
    } >= {
        "ix_candidate_queue_receipts_session_binding_hash",
        "ix_candidate_queue_receipts_actor_hash",
        "ix_candidate_queue_receipts_kind",
        "ix_candidate_queue_receipts_nonce_hash",
        "ix_candidate_queue_receipts_state",
        "ix_candidate_queue_receipts_request_id",
    }
    mutation_operation_check = next(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints(
            "mutation_interlocks"
        )
        if constraint["name"] == "ck_mutation_interlocks_operation"
    )
    assert "candidate_queue" not in mutation_operation_check
    assert {
        index["name"]
        for index in inspector.get_indexes("untrusted_ingest_events")
    } >= {
        "ix_untrusted_ingest_events_state",
        "ix_untrusted_ingest_events_received_at",
        "ix_untrusted_ingest_events_summary_decision_id",
    }

    with engine.connect() as connection:
        state = connection.execute(
            text(
                "SELECT singleton_id,schema_version,state,rows_total,"
                "rows_completed,started_at,completed_at "
                "FROM sensitive_migration_state"
            )
        ).one()
    assert state == (1, 1, "required", 0, 0, None, None)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO sensitive_migration_state "
        "(singleton_id,schema_version,state,active_key_id,rows_total,"
        "rows_completed,updated_at) VALUES "
        "(2,1,'required','valid-key-2026',0,0,CURRENT_TIMESTAMP)",
        "UPDATE sensitive_migration_state SET rows_total=-1",
        "UPDATE sensitive_migration_state SET rows_completed=1",
        "UPDATE sensitive_migration_state SET state='unknown'",
        "UPDATE sensitive_migration_state SET active_key_id='bad key id'",
        "UPDATE sensitive_migration_state SET schema_version=0",
        "INSERT INTO candidate_nonces "
        "(nonce_hash,actor,kind,expires_at,request_id) VALUES "
        "('bad','operator','analysis',CURRENT_TIMESTAMP,'request-1')",
        "INSERT INTO untrusted_ingest_events "
        "(source_hash,content_hash,byte_length,flags_json,state,received_at) "
        "VALUES "
        "('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
        "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',"
        "-1,'[]','received',CURRENT_TIMESTAMP)",
        "INSERT INTO untrusted_ingest_events "
        "(source_hash,content_hash,byte_length,flags_json,state,received_at) "
        "VALUES "
        "('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
        "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',"
        "1,'not-json','received',CURRENT_TIMESTAMP)",
    ],
)
def test_sensitive_trust_constraints_reject_invalid_state(tmp_path, statement):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"invalid-sensitive-{hashlib.sha256(statement.encode()).hexdigest()}.db",
        "head",
    )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(statement))


def test_sensitive_trust_upgrade_and_downgrade_do_not_rewrite_narratives(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "sensitive-no-rewrite.db",
        "20260727_0012",
    )
    original = {
        "reason": "legacy operator reason remains plaintext for Task 6",
        "detail": '{"legacy":"audit detail"}',
        "plan": '{"legacy":"trade plan"}',
        "sized": '{"legacy":"sized plan"}',
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_events "
                "(actor,action,target_type,target_id,request_id,"
                "idempotency_key,reason,result_code,latency_ms,detail_json,"
                "created_at) VALUES "
                "('operator:test','legacy','test','1','request-legacy','',"
                ":reason,'recorded',0,:detail,CURRENT_TIMESTAMP)"
            ),
            original,
        )
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(symbol,action,status,paper_only,shadow,plan_json,sized_json,"
                "entry_filled_qty,exit_filled_qty,residual_generation,created_at) "
                "VALUES "
                "('AAPL','hold','proposed',1,0,:plan,:sized,0,0,0,"
                "CURRENT_TIMESTAMP)"
            ),
            original,
        )

    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT reason,detail_json FROM audit_events")
        ).one() == (original["reason"], original["detail"])
        assert connection.execute(
            text("SELECT plan_json,sized_json FROM trade_plans")
        ).one() == (original["plan"], original["sized"])

    command.downgrade(cfg, "20260727_0012")
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0012"
        assert connection.execute(
            text("SELECT reason,detail_json FROM audit_events")
        ).one() == (original["reason"], original["detail"])
        assert {
            "sensitive_migration_state",
            "candidate_nonces",
            "untrusted_ingest_events",
        }.isdisjoint(inspect(engine).get_table_names())


@pytest.mark.parametrize(
    ("case", "statements"),
    [
        (
            "missing-singleton",
            ["DELETE FROM sensitive_migration_state"],
        ),
        (
            "multiple-singletons",
            [
                "PRAGMA ignore_check_constraints=ON",
                "INSERT INTO sensitive_migration_state "
                "(singleton_id,schema_version,state,active_key_id,"
                "rows_total,rows_completed,updated_at) VALUES "
                "(2,1,'required','migration-required',0,0,CURRENT_TIMESTAMP)",
                "PRAGMA ignore_check_constraints=OFF",
            ],
        ),
        (
            "schema-mismatch",
            ["UPDATE sensitive_migration_state SET schema_version=2"],
        ),
        (
            "active-key-mismatch",
            [
                "UPDATE sensitive_migration_state "
                "SET active_key_id='configured-key-2026'"
            ],
        ),
        (
            "nonzero-total",
            ["UPDATE sensitive_migration_state SET rows_total=1"],
        ),
        (
            "nonzero-completed",
            [
                "UPDATE sensitive_migration_state "
                "SET state='migrating',started_at=CURRENT_TIMESTAMP,"
                "rows_total=1,rows_completed=1"
            ],
        ),
        (
            "backup-evidence",
            [
                "UPDATE sensitive_migration_state SET backup_path_hash='"
                + "a" * 64
                + "'"
            ],
        ),
        (
            "completion-evidence",
            [
                "PRAGMA ignore_check_constraints=ON",
                "UPDATE sensitive_migration_state "
                "SET completed_at=CURRENT_TIMESTAMP",
                "PRAGMA ignore_check_constraints=OFF",
            ],
        ),
        *[
            pytest.param(
                state,
                [
                    "UPDATE sensitive_migration_state "
                    f"SET state='{state}',started_at=CURRENT_TIMESTAMP"
                ],
                id=state,
            )
            for state in ("migrating", "rotating", "failed")
        ],
        (
            "complete",
            [
                "UPDATE sensitive_migration_state "
                "SET state='complete',"
                "active_key_id='configured-key-2026',"
                "backup_path_hash='" + "b" * 64 + "',"
                "started_at=CURRENT_TIMESTAMP,"
                "completed_at=CURRENT_TIMESTAMP"
            ],
        ),
        (
            "candidate-nonce",
            [
                "INSERT INTO candidate_nonces "
                "(nonce_hash,actor,kind,expires_at,request_id) VALUES "
                "('" + "c" * 64 + "','operator:test','analysis',"
                "CURRENT_TIMESTAMP,'downgrade-probe')"
            ],
        ),
        (
            "untrusted-ingest",
            [
                "INSERT INTO untrusted_ingest_events "
                "(source_hash,content_hash,byte_length,flags_json,state,"
                "received_at) VALUES "
                "('" + "d" * 64 + "','" + "e" * 64 + "',1,'[]',"
                "'received',CURRENT_TIMESTAMP)"
            ],
        ),
    ],
)
def test_sensitive_trust_downgrade_refuses_unsafe_state_before_ddl(
    tmp_path,
    case,
    statements,
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"unsafe-sensitive-downgrade-{case}.db",
        "head",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))

    with pytest.raises(
        RuntimeError,
        match="^sensitive_trust_downgrade_blocked$",
    ):
        command.downgrade(cfg, "20260727_0012")

    assert {
        "sensitive_migration_state",
        "candidate_nonces",
        "untrusted_ingest_events",
    } <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0013"


@pytest.mark.parametrize(
    "mutation",
    ["state-update", "dependent-insert"],
)
def test_sensitive_trust_downgrade_lock_closes_checked_after_race(
    tmp_path,
    monkeypatch,
    mutation,
):
    database_path = tmp_path / f"downgrade-race-{mutation}.db"
    engine, cfg = _engine_at_revision(database_path, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA journal_mode=WAL")) == "wal"

    gate_checked = threading.Event()
    allow_ddl = threading.Event()
    mutation_attempted = threading.Event()
    mutation_finished = threading.Event()
    migration_errors: list[BaseException] = []
    mutation_errors: list[BaseException] = []
    mutation_committed: list[bool] = []
    paused_drop_points: list[tuple[str, str | None]] = []
    original_drop_index = alembic_op.drop_index

    def pause_at_sensitive_0013_drop(*args, **kwargs):
        drop_point = (
            args[0] if args else "",
            kwargs.get("table_name"),
        )
        if drop_point == (
            "ux_untrusted_ingest_source_content",
            "untrusted_ingest_events",
        ):
            paused_drop_points.append(
                drop_point
            )
            gate_checked.set()
            if not allow_ddl.wait(timeout=10):
                raise RuntimeError("test_downgrade_pause_timeout")
        return original_drop_index(*args, **kwargs)

    monkeypatch.setattr(
        alembic_op,
        "drop_index",
        pause_at_sensitive_0013_drop,
    )

    def run_downgrade() -> None:
        try:
            command.downgrade(cfg, "20260727_0012")
        except BaseException as error:
            migration_errors.append(error)

    migration_thread = threading.Thread(target=run_downgrade)
    migration_thread.start()
    assert gate_checked.wait(timeout=10)
    assert paused_drop_points == [
        (
            "ux_untrusted_ingest_source_content",
            "untrusted_ingest_events",
        )
    ]

    concurrent_engine = create_db_engine(_url(database_path))

    def mark_attempt(
        _conn,
        _cursor,
        _statement,
        _parameters,
        _context,
        _many,
    ):
        mutation_attempted.set()

    event.listen(
        concurrent_engine,
        "before_cursor_execute",
        mark_attempt,
    )

    def run_mutation() -> None:
        try:
            with concurrent_engine.begin() as connection:
                if mutation == "state-update":
                    connection.execute(
                        text(
                            "UPDATE sensitive_migration_state "
                            "SET state='migrating',"
                            "started_at=CURRENT_TIMESTAMP"
                        )
                    )
                else:
                    connection.execute(
                        text(
                            "INSERT INTO candidate_nonces "
                            "(nonce_hash,actor,kind,expires_at,request_id) "
                            "VALUES "
                            "(:nonce_hash,'operator:test','analysis',"
                            "CURRENT_TIMESTAMP,'checked-after-race')"
                        ),
                        {"nonce_hash": "f" * 64},
                    )
            mutation_committed.append(True)
        except BaseException as error:
            mutation_errors.append(error)
        finally:
            mutation_finished.set()

    mutation_thread = threading.Thread(target=run_mutation)
    mutation_thread.start()
    assert mutation_attempted.wait(timeout=10)
    mutation_was_blocked = not mutation_finished.wait(timeout=0.5)

    allow_ddl.set()
    migration_thread.join(timeout=10)
    mutation_thread.join(timeout=10)
    concurrent_engine.dispose()

    assert not migration_thread.is_alive()
    assert not mutation_thread.is_alive()
    assert migration_errors == []
    assert mutation_was_blocked
    assert mutation_committed == []
    assert len(mutation_errors) == 1
    assert isinstance(mutation_errors[0], OperationalError)
    assert {
        "sensitive_migration_state",
        "candidate_nonces",
        "untrusted_ingest_events",
    }.isdisjoint(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0012"


def test_sensitive_trust_downgrade_lock_failure_refuses_before_ddl(
    tmp_path,
):
    database_path = tmp_path / "downgrade-lock-failure.db"
    engine, cfg = _engine_at_revision(database_path, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA journal_mode=WAL")) == "wal"
    cfg.set_main_option(
        "sqlalchemy.url",
        f"{_url(database_path)}?timeout=0.05",
    )

    with engine.connect() as blocker:
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        blocker.execute(
            text(
                "INSERT INTO candidate_nonces "
                "(nonce_hash,actor,kind,expires_at,request_id) VALUES "
                "(:nonce_hash,'operator:test','analysis',"
                "CURRENT_TIMESTAMP,'lock-failure-evidence')"
            ),
            {"nonce_hash": "9" * 64},
        )
        with pytest.raises(
            RuntimeError,
            match="^runtime_tenure_downgrade_blocked$",
        ):
            command.downgrade(cfg, "20260727_0012")
        blocker.commit()

    assert {
        "sensitive_migration_state",
        "candidate_nonces",
        "untrusted_ingest_events",
    } <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260730_0018"
        assert connection.scalar(
            text("SELECT count(*) FROM candidate_nonces")
        ) == 1


@pytest.mark.parametrize(
    ("counter_name", "statement"),
    [
        (
            "rate window hits",
            "INSERT INTO rate_windows "
            "(bucket_key,policy_name,window_started_at,expires_at,hits) VALUES "
            "('negative-rate-hits','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,-1)",
        ),
        (
            "rate window version",
            "INSERT INTO rate_windows "
            "(bucket_key,policy_name,window_started_at,expires_at,hits,version) VALUES "
            "('negative-rate-version','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,0,-1)",
        ),
        (
            "lease generation",
            "INSERT INTO concurrency_leases "
            "(resource_key,owner,expires_at,generation) VALUES "
            "('negative-lease-generation','',CURRENT_TIMESTAMP,-1)",
        ),
        (
            "provider calls",
            "INSERT INTO provider_budget_days "
            "(provider,budget_day,calls_used,input_tokens_used,output_tokens_used) VALUES "
            "('negative-calls','2026-07-27',-1,0,0)",
        ),
        (
            "provider input tokens",
            "INSERT INTO provider_budget_days "
            "(provider,budget_day,calls_used,input_tokens_used,output_tokens_used) VALUES "
            "('negative-input-tokens','2026-07-27',0,-1,0)",
        ),
        (
            "provider output tokens",
            "INSERT INTO provider_budget_days "
            "(provider,budget_day,calls_used,input_tokens_used,output_tokens_used) VALUES "
            "('negative-output-tokens','2026-07-27',0,0,-1)",
        ),
        (
            "reservation input",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,input_actual,output_actual,created_at,expires_at) "
            "VALUES ('negative-reservation-input','gemini','chat','request',"
            "'2026-07-27','reserved',-1,0,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        ),
        (
            "reservation output",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,input_actual,output_actual,created_at,expires_at) "
            "VALUES ('negative-reservation-output','gemini','chat','request',"
            "'2026-07-27','reserved',0,-1,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        ),
        (
            "reservation actual input",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,input_actual,output_actual,created_at,expires_at) "
            "VALUES ('negative-reservation-actual-input','gemini','chat','request',"
            "'2026-07-27','reserved',0,0,-1,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        ),
        (
            "reservation actual output",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,input_actual,output_actual,created_at,expires_at) "
            "VALUES ('negative-reservation-actual-output','gemini','chat','request',"
            "'2026-07-27','reserved',0,0,NULL,-1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        ),
    ],
)
def test_policy_budget_migration_rejects_negative_counters(
    tmp_path, counter_name, statement
):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"negative-{counter_name}.db", "head"
    )

    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(statement))


@pytest.mark.parametrize(
    "state", ["reserved", "started", "settled", "unknown", "released"]
)
def test_provider_reservation_migration_accepts_authoritative_states(
    tmp_path, state
):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"accepted-reservation-{state}.db", "head"
    )

    reservation_id = f"accepted-reservation-{state}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO provider_reservations "
                "(reservation_id,provider,category,request_id,budget_day,state,"
                "input_reserved,output_reserved,created_at,expires_at) VALUES "
                "(:reservation_id,'gemini','chat','request','2026-07-27',:state,"
                "0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"reservation_id": reservation_id, "state": state},
        )

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT state FROM provider_reservations "
                "WHERE reservation_id = :reservation_id"
            ),
            {"reservation_id": reservation_id},
        ) == state


@pytest.mark.parametrize("state", ["canceled", "expired"])
def test_provider_reservation_migration_rejects_outside_states(tmp_path, state):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"rejected-reservation-{state}.db", "head"
    )

    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO provider_reservations "
                    "(reservation_id,provider,category,request_id,budget_day,state,"
                    "input_reserved,output_reserved,created_at,expires_at) VALUES "
                    "(:reservation_id,'gemini','chat','request','2026-07-27',:state,"
                    "0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {"reservation_id": f"rejected-reservation-{state}", "state": state},
            )


@pytest.mark.parametrize("state", ["started", "completed", "failed"])
def test_panic_receipt_migration_accepts_authoritative_states(tmp_path, state):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"accepted-panic-{state}.db", "head"
    )

    account_scope = f"accepted-panic-{state}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO panic_receipts "
                "(account_scope,request_id,state,started_at,expires_at) VALUES "
                "(:account_scope,'request',:state,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"account_scope": account_scope, "state": state},
        )

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT state FROM panic_receipts "
                "WHERE account_scope = :account_scope"
            ),
            {"account_scope": account_scope},
        ) == state


@pytest.mark.parametrize("state", ["reserved", "unknown"])
def test_panic_receipt_migration_rejects_outside_states(tmp_path, state):
    engine, _cfg = _engine_at_revision(
        tmp_path / f"rejected-panic-{state}.db", "head"
    )

    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO panic_receipts "
                    "(account_scope,request_id,state,started_at,expires_at) VALUES "
                    "(:account_scope,'request',:state,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {"account_scope": f"rejected-panic-{state}", "state": state},
            )


@pytest.mark.parametrize(
    (
        "durable_state",
        "statement",
        "preserved_query",
        "expected_row",
        "expected_revision",
    ),
    [
        (
            "started provider reservation",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,created_at,expires_at) VALUES "
            "('started-reservation','gemini','chat','request','2026-07-27',"
            "'started',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            "SELECT reservation_id, state FROM provider_reservations "
            "WHERE reservation_id = 'started-reservation'",
            ("started-reservation", "started"),
            "20260727_0011",
        ),
        (
            "unknown provider reservation",
            "INSERT INTO provider_reservations "
            "(reservation_id,provider,category,request_id,budget_day,state,"
            "input_reserved,output_reserved,created_at,expires_at) VALUES "
            "('unknown-reservation','gemini','chat','request','2026-07-27',"
            "'unknown',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            "SELECT reservation_id, state FROM provider_reservations "
            "WHERE reservation_id = 'unknown-reservation'",
            ("unknown-reservation", "unknown"),
            "20260727_0011",
        ),
        (
            "started panic receipt",
            "INSERT INTO panic_receipts "
            "(account_scope,request_id,state,started_at,expires_at) VALUES "
            "('started-panic','request','started',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            "SELECT account_scope, state FROM panic_receipts "
            "WHERE account_scope = 'started-panic'",
            ("started-panic", "started"),
            "20260727_0012",
        ),
    ],
)
def test_policy_budget_downgrade_refuses_durable_inflight_state(
    tmp_path,
    durable_state,
    statement,
    preserved_query,
    expected_row,
    expected_revision,
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"{durable_state}.db", "head"
    )
    with engine.begin() as connection:
        connection.execute(text(statement))

    with pytest.raises(
        RuntimeError,
        match="inflight policy state|generation-bound panic receipt",
    ):
        command.downgrade(cfg, "20260726_0010")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == expected_revision
        assert {
            "rate_windows",
            "concurrency_leases",
            "provider_budget_days",
            "provider_reservations",
            "panic_receipts",
        } <= set(inspect(engine).get_table_names())
        assert connection.execute(text(preserved_query)).one() == expected_row


def test_mutation_interlock_upgrade_and_downgrade_preserve_latched_state(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "mutation-interlock.db",
        "20260727_0011",
    )
    assert "mutation_interlocks" not in inspect(engine).get_table_names()

    command.upgrade(cfg, "head")

    assert "mutation_interlocks" in inspect(engine).get_table_names()
    assert "lease_generation" in {
        column["name"]
        for column in inspect(engine).get_columns("panic_receipts")
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mutation_interlocks "
                    "(resource_key,owner,generation,operation,state,outcome_code,"
                    "created_at,updated_at) VALUES "
                    "(:resource_key,'internal-owner',7,"
                    "'order_approve','uncertain','lease_renewal_unproven',"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"resource_key": "route:" + "a" * 64 + ":0"},
        )

    with pytest.raises(
        RuntimeError,
        match="durable mutation interlock",
    ):
        command.downgrade(cfg, "20260727_0011")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0012"
        assert connection.scalar(
            text("SELECT count(*) FROM mutation_interlocks")
        ) == 1

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mutation_interlocks"))
    command.downgrade(cfg, "20260727_0011")

    assert "mutation_interlocks" not in inspect(engine).get_table_names()
    assert "lease_generation" not in {
        column["name"]
        for column in inspect(engine).get_columns("panic_receipts")
    }
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0011"


@pytest.mark.parametrize(
    (
        "operation",
        "generation",
        "state",
        "outcome_code",
        "worker_finished_at",
    ),
    [
        (
            "order_approve",
            1,
            "active",
            "handler_completed",
            None,
        ),
        (
            "order_approve",
            1,
            "active",
            "",
            datetime(2026, 7, 27, tzinfo=timezone.utc),
        ),
        ("order_approve", 1, "settled", "", None),
        ("order_approve", 1, "uncertain", "", None),
        ("order_approve", 1, "invalid", "", None),
        ("user supplied operation", 1, "active", "", None),
        ("order_approve", -1, "active", "", None),
    ],
)
def test_mutation_interlock_constraints_reject_invalid_combinations(
    tmp_path,
    operation,
    generation,
    state,
    outcome_code,
    worker_finished_at,
):
    engine, _cfg = _engine_at_revision(
        tmp_path
        / (
            "invalid-interlock-"
            f"{abs(hash((operation, generation, state, outcome_code)))}.db"
        ),
        "head",
    )

    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO mutation_interlocks "
                    "(resource_key,owner,generation,operation,state,"
                    "outcome_code,worker_finished_at,created_at,updated_at) "
                    "VALUES (:resource_key,'internal-owner',:generation,"
                    ":operation,:state,:outcome_code,:worker_finished_at,"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {
                    "resource_key": "route:" + "b" * 64 + ":0",
                    "generation": generation,
                    "operation": operation,
                    "state": state,
                    "outcome_code": outcome_code,
                    "worker_finished_at": worker_finished_at,
                },
            )


def test_fill_activated_upgrade_refuses_active_legacy_plan(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "active-legacy-plan.db",
        "20260724_0008",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(id,symbol,action,status,paper_only,shadow,plan_json,"
                "sized_json,created_at) VALUES "
                "(1,'AAPL','buy','approved',1,0,'{}','{}',"
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rule_groups "
                "(id,group_key,state,version,reconciliation_required,"
                "created_at,updated_at) VALUES "
                "(1,'legacy-plan-1','active',0,0,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rules "
                "(id,group_id,payload_version,ticker,condition_json,"
                "action_json,state,created_at,plan_id,kind,"
                "pre_approved) VALUES "
                "(1,1,1,'AAPL','{\"type\":\"price\","
                "\"direction\":\"below\",\"price\":\"99\"}',"
                "'{\"side\":\"buy\",\"order_type\":\"market\","
                "\"qty\":\"1\"}','active',CURRENT_TIMESTAMP,1,"
                "'entry',0)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="active legacy plans",
    ):
        command.upgrade(cfg, "head")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0008"
    assert "activation" not in {
        column["name"]
        for column in inspect(engine).get_columns("rules")
    }


def test_fill_activated_upgrade_refuses_terminalized_legacy_plan_order(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "terminalized-legacy-plan-order.db",
        "20260724_0008",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(id,symbol,action,status,paper_only,shadow,plan_json,"
                "sized_json,created_at) VALUES "
                "(1,'AAPL','buy','approved',1,0,'{}','{}',"
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rule_groups "
                "(id,group_key,state,version,reconciliation_required,"
                "created_at,updated_at) VALUES "
                "(1,'legacy-terminalized-plan','triggered',0,0,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rules "
                "(id,group_id,payload_version,ticker,condition_json,"
                "action_json,state,created_at,plan_id,kind,"
                "pre_approved) VALUES "
                "(1,1,1,'AAPL','{\"type\":\"price\","
                "\"direction\":\"below\",\"price\":\"99\"}',"
                "'{\"side\":\"buy\",\"order_type\":\"market\","
                "\"qty\":\"1\"}','triggered',CURRENT_TIMESTAMP,1,"
                "'entry',0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,"
                "submission_attempt,acceptance_state,last_error_code,"
                "version) VALUES "
                "(1,'legacy-terminalized-order','AAPL','buy','market',"
                "1,'submitted','legacy-broker-order',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'','simple','{}',"
                "1,'submitted','',0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO proposals "
                "(order_id,source_rule_group_id,reasoning,ttl_minutes,"
                "created_at,expires_at) VALUES "
                "(1,1,'legacy plan proposal',15,CURRENT_TIMESTAMP,"
                "'2026-08-01 12:00:00')"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="legacy plans",
    ):
        command.upgrade(cfg, "head")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0008"


def test_fill_activated_upgrade_refuses_canceled_plan_with_legacy_fill(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "canceled-legacy-plan-fill.db",
        "20260724_0008",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(id,symbol,action,status,paper_only,shadow,plan_json,"
                "sized_json,created_at) VALUES "
                "(1,'AAPL','buy','canceled',1,0,'{}','{}',"
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rule_groups "
                "(id,group_key,state,version,reconciliation_required,"
                "created_at,updated_at) VALUES "
                "(1,'legacy-canceled-plan','canceled',0,0,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rules "
                "(id,group_id,payload_version,ticker,condition_json,"
                "action_json,state,created_at,plan_id,kind,"
                "pre_approved) VALUES "
                "(1,1,1,'AAPL','{\"type\":\"price\","
                "\"direction\":\"below\",\"price\":\"99\"}',"
                "'{\"side\":\"buy\",\"order_type\":\"market\","
                "\"qty\":\"1\"}','canceled',CURRENT_TIMESTAMP,1,"
                "'entry',0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,"
                "submission_attempt,acceptance_state,last_error_code,"
                "version) VALUES "
                "(1,'legacy-canceled-filled-entry','AAPL','buy',"
                "'market',1,'filled','legacy-canceled-broker-order',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'','simple','{}',"
                "1,'accepted','',0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO proposals "
                "(order_id,source_rule_group_id,reasoning,ttl_minutes,"
                "created_at,expires_at) VALUES "
                "(1,1,'legacy canceled plan fill',15,"
                "CURRENT_TIMESTAMP,'2026-08-01 12:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fills "
                "(order_id,ticker,side,qty,price,broker_fill_id,"
                "filled_at) VALUES "
                "(1,'AAPL','buy',1,98,'legacy-canceled-fill',"
                "CURRENT_TIMESTAMP)"
            )
        )

    with pytest.raises(RuntimeError, match="legacy plans"):
        command.upgrade(cfg, "head")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0008"


def test_fill_activated_rule_downgrade_refuses_to_remove_protection(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "fill-activated-rules.db",
        "head",
    )
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        group = RuleGroup(
            group_key="pending-protective-exits",
            state="pending",
        )
        session.add(group)
        session.flush()
        session.add(
            Rule(
                group_id=group.id,
                ticker="AAPL",
                condition_json=json.dumps(
                    {
                        "type": "price",
                        "direction": "below",
                        "price": "90",
                    }
                ),
                action_json=json.dumps(
                    {
                        "side": "sell",
                        "order_type": "market",
                        "qty": "1",
                    }
                ),
                state="pending",
                kind="stop",
                activation="on_entry_fill",
                terminal_on_trigger=True,
            )
        )
        session.commit()

    with pytest.raises(
        RuntimeError,
        match="fill-activated plan protection",
    ):
        command.downgrade(cfg, "20260724_0008")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0009"
    assert {
        "activation",
        "terminal_on_trigger",
    } <= {
        column["name"]
        for column in inspect(engine).get_columns("rules")
    }


def test_plan_cancel_intent_downgrade_refuses_to_erase_retry_state(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "plan-cancel-intent.db",
        "head",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders "
                "(idempotency_key,ticker,side,order_type,status,"
                "plan_cancel_state,created_at,updated_at,"
                "approval_reason,submission_kind,"
                "submission_payload_json,submission_attempt,"
                "acceptance_state,last_error_code,version) VALUES "
                "('durable-plan-cancel','AAPL','buy','market',"
                "'submitted','requested',CURRENT_TIMESTAMP,"
                "CURRENT_TIMESTAMP,'','simple','{}',1,'submitted',"
                "'plan_cancel',0)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="durable plan cancellation intent",
    ):
        command.downgrade(cfg, "20260724_0009")

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260726_0010"
        assert connection.scalar(
            text(
                "SELECT plan_cancel_state FROM orders "
                "WHERE idempotency_key='durable-plan-cancel'"
            )
        ) == "requested"


def test_plan_cancel_intent_upgrade_backfills_only_plan_linked_markers(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "plan-cancel-backfill.db",
        "20260724_0009",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trade_plans "
                "(id,symbol,action,status,paper_only,shadow,plan_json,"
                "sized_json,created_at) VALUES "
                "(1,'AAPL','buy','approved',1,0,'{}','{}',"
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rule_groups "
                "(id,group_key,state,version,reconciliation_required,"
                "created_at,updated_at) VALUES "
                "(1,'cancel-backfill','active',0,1,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rules "
                "(id,group_id,payload_version,ticker,condition_json,"
                "action_json,state,created_at,plan_id,kind,"
                "pre_approved,activation,terminal_on_trigger) VALUES "
                "(1,1,1,'AAPL','{\"type\":\"price\","
                "\"direction\":\"below\",\"price\":\"99\"}',"
                "'{\"side\":\"buy\",\"order_type\":\"market\","
                "\"qty\":\"1\"}','processing',CURRENT_TIMESTAMP,1,"
                "'entry',0,'immediate',1)"
            )
        )
        for order_id, marker in enumerate(
            (
                "plan_cancel",
                "plan_exit_entry_cancel",
                "indeterminate_cancel",
            ),
            start=1,
        ):
            connection.execute(
                text(
                    "INSERT INTO orders "
                    "(id,idempotency_key,ticker,side,order_type,qty,"
                    "status,broker_order_id,created_at,updated_at,"
                    "approval_reason,submission_kind,"
                    "submission_payload_json,submission_attempt,"
                    "acceptance_state,last_error_code,version) VALUES "
                    "(:id,:key,'AAPL','buy','market',1,'submitted',"
                    ":broker_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'',"
                    "'simple','{}',1,'submitted',:marker,0)"
                ),
                {
                    "id": order_id,
                    "key": f"plan-cancel-backfill-{order_id}",
                    "broker_id": f"plan-cancel-broker-{order_id}",
                    "marker": marker,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO proposals "
                    "(order_id,source_rule_group_id,source_rule_id,"
                    "reasoning,ttl_minutes,plan_generation,created_at,"
                    "expires_at) VALUES "
                    "(:id,1,1,'legacy cancellation intent',15,0,"
                    "CURRENT_TIMESTAMP,'2026-08-01 12:00:00')"
                ),
                {"id": order_id},
            )
        connection.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,"
                "submission_attempt,acceptance_state,last_error_code,"
                "version) VALUES "
                "(4,'generic-indeterminate','AAPL','buy','market',1,"
                "'submitted','generic-broker',CURRENT_TIMESTAMP,"
                "CURRENT_TIMESTAMP,'','simple','{}',1,'submitted',"
                "'indeterminate_cancel',0)"
            )
        )

    command.upgrade(cfg, "head")

    with engine.connect() as connection:
        states = connection.execute(
            text(
                "SELECT id, plan_cancel_state FROM orders "
                "ORDER BY id"
            )
        ).all()
        version = connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
    assert states == [
        (1, "requested"),
        (2, "requested"),
        (3, "indeterminate"),
        (4, "none"),
    ]
    assert version == "20260730_0018"


def test_auth_session_upgrade_from_0005_adds_only_hashed_session_storage(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "auth-sessions.db",
        "20260724_0005",
    )
    assert "auth_sessions" not in inspect(engine).get_table_names()

    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    assert {
        column["name"] for column in inspector.get_columns("auth_sessions")
    } == {
        "id",
        "token_hash",
        "csrf_hash",
        "actor",
        "created_at",
        "expires_at",
        "authenticated_at",
        "revoked_at",
    }
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("auth_sessions")
    }
    assert indexes["ix_auth_sessions_token_hash"]["unique"] == 1
    assert indexes["ix_auth_sessions_expires_at"]["unique"] == 0
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260730_0018"

    command.downgrade(cfg, "20260724_0005")
    assert "auth_sessions" not in inspect(engine).get_table_names()
    assert "circuit_breaker_state" in inspect(engine).get_table_names()


def test_runtime_health_upgrade_deduplicates_heartbeats_by_time_then_id(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "runtime-health.db",
        "20260724_0006",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO heartbeats (id, source, at) VALUES "
                "(1, 'daemon', '2026-07-24 10:00:00'),"
                "(2, 'daemon', '2026-07-24 11:00:00'),"
                "(3, 'daemon', '2026-07-24 11:00:00'),"
                "(4, 'app', '2026-07-24 09:00:00')"
            )
        )

    command.upgrade(cfg, "head")

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, source FROM heartbeats "
                "ORDER BY source"
            )
        ).mappings().all()
        version = connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
    assert rows == [
        {"id": 4, "source": "app"},
        {"id": 3, "source": "daemon"},
    ]
    assert version == "20260730_0018"
    heartbeat_indexes = {
        index["name"]: index
        for index in inspect(engine).get_indexes("heartbeats")
    }
    assert heartbeat_indexes["uq_heartbeats_source"]["unique"] == 1


def test_startup_reconciliation_downgrade_refuses_to_drop_durable_state(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "startup-reconciliation.db",
        "head",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO startup_reconciliation_state "
                "(broker,generation,completed_generation,status,actor,"
                "reason,request_id,evidence_json,started_at,completed_at,"
                "updated_at) VALUES "
                "('alpaca',2,1,'required','runtime:test','restart',"
                "'startup-test','{}',CURRENT_TIMESTAMP,NULL,"
                "CURRENT_TIMESTAMP)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="durable startup reconciliation state",
    ):
        command.downgrade(cfg, "20260724_0007")

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT generation FROM startup_reconciliation_state "
                "WHERE broker='alpaca'"
            )
        ) == 2
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0008"


def test_unversioned_database_refuses_unsafe_in_place_bootstrap(tmp_path):
    path = tmp_path / "legacy.db"
    engine = _legacy_engine(path)
    with pytest.raises(
        SchemaOutOfDate,
        match="manual isolated schema bootstrap",
    ):
        require_current_schema(engine)
    destination = tmp_path / "must-not-be-created"

    with pytest.raises(
        RuntimeError,
        match="^schema_maintenance_bootstrap_required$",
    ):
        adopt_existing(
            engine,
            backup_key=MIGRATION_BACKUP_KEY,
            backup_key_id=MIGRATION_BACKUP_KEY_ID,
            backup_directory=destination,
        )

    assert "alembic_version" not in inspect(engine).get_table_names()
    assert not destination.exists()


def test_unversioned_bootstrap_refusal_preserves_committed_rows(tmp_path):
    path = tmp_path / "legacy.db"
    engine = _legacy_engine(path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders "
                "(idempotency_key,ticker,side,order_type,status,"
                "created_at,updated_at) VALUES "
                "('keep-me','AAPL','buy','market','proposed',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="^schema_maintenance_bootstrap_required$",
    ):
        adopt_existing(engine)

    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM orders "
                "WHERE idempotency_key='keep-me'"
            )
        ) == 1
    assert "alembic_version" not in inspect(engine).get_table_names()


def test_versioned_pre_tenure_schema_refuses_upgrade_without_mutation(tmp_path):
    path = tmp_path / "legacy.db"
    engine, _cfg = _engine_at_revision(path, "20260727_0013")
    destination = tmp_path / "must-not-be-created"

    with pytest.raises(
        RuntimeError,
        match="^schema_maintenance_bootstrap_required$",
    ):
        upgrade(
            engine,
            backup_key=MIGRATION_BACKUP_KEY,
            backup_key_id=MIGRATION_BACKUP_KEY_ID,
            backup_directory=destination,
            process_identity=ProcessIdentity(8200, "pre-tenure-schema"),
            process_inspector=_AbsentProcessInspector(),
        )

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260727_0013"
    assert not destination.exists()


def test_unknown_revision_is_rejected_at_startup(tmp_path):
    engine = create_db_engine(_url(tmp_path / "unknown.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text(
            "INSERT INTO alembic_version(version_num) VALUES ('unknown_revision')"
        ))
    with pytest.raises(SchemaOutOfDate, match="current='unknown_revision'"):
        require_current_schema(engine)


def test_order_outbox_upgrade_preserves_and_maps_legacy_order(tmp_path):
    path = tmp_path / "legacy-order.db"
    engine = _legacy_engine(path)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO orders "
            "(idempotency_key,ticker,side,order_type,status,created_at,updated_at) "
            "VALUES ('legacy-approved','AAPL','buy','market','approved',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        ))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _url(path))
    command.stamp(cfg, "20260724_0001")
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status, approval_actor FROM orders "
            "WHERE idempotency_key='legacy-approved'"
        )).one()
    assert row.status == "approval_recorded"
    assert row.approval_actor is None


def test_rule_lease_upgrade_from_0003_maps_every_repository_legacy_shape(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "legacy-rules.db", "20260724_0003"
    )
    rows = [
        (
            1,
            "AAPL",
            {"price_below": 175},
            {"side": "buy", "notional": "50"},
            "active",
            7,
            "entry",
            None,
            None,
            None,
            True,
        ),
        (
            2,
            "AAPL",
            {"price_above": 200},
            {"side": "sell", "qty": "2"},
            "processing",
            7,
            "target",
            "0.5",
            None,
            None,
            True,
        ),
        (
            3,
            "AAPL",
            {"trailing_stop_pct": 8},
            {"side": "sell", "qty": "4"},
            "canceled",
            None,
            "trailing",
            None,
            "212.50",
            None,
            True,
        ),
        (
            4,
            "AAPL",
            {},
            {"side": "sell", "qty": "4"},
            "active",
            None,
            "time",
            None,
            None,
            "2026-08-01 12:00:00",
            False,
        ),
        (
            5,
            "MSFT",
            {"retired_custom_shape": "preserve-me"},
            {"historical_action": "preserve-me"},
            "canceled",
            None,
            "price",
            None,
            None,
            None,
            False,
        ),
    ]
    with engine.begin() as conn:
        for row in rows:
            (
                rule_id,
                ticker,
                condition,
                action,
                state,
                plan_id,
                kind,
                fraction,
                hwm,
                deadline,
                pre_approved,
            ) = row
            conn.execute(
                text(
                    "INSERT INTO rules "
                    "(id,ticker,condition_json,action_json,state,created_at,plan_id,"
                    "kind,fraction,hwm,deadline,pre_approved) "
                    "VALUES (:id,:ticker,:condition,:action,:state,CURRENT_TIMESTAMP,"
                    ":plan_id,:kind,:fraction,:hwm,:deadline,:pre_approved)"
                ),
                {
                    "id": rule_id,
                    "ticker": ticker,
                    "condition": json.dumps(condition),
                    "action": json.dumps(action),
                    "state": state,
                    "plan_id": plan_id,
                    "kind": kind,
                    "fraction": fraction,
                    "hwm": hwm,
                    "deadline": deadline,
                    "pre_approved": pre_approved,
                },
            )
        conn.execute(
            text(
                "INSERT INTO orders "
                    "(id,idempotency_key,ticker,side,order_type,status,created_at,"
                "updated_at,approval_reason,submission_kind,"
                "submission_payload_json,submission_attempt,acceptance_state,"
                "last_error_code,version) "
                    "VALUES (1,'rule-1','AAPL','buy','market','acceptance_unknown',"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'','simple','{}',0,"
                    "'acceptance_unknown','',0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO proposals "
                "(order_id,reasoning,ttl_minutes,created_at,expires_at) "
                "VALUES (1,'legacy rule trace',15,CURRENT_TIMESTAMP,"
                "'2026-08-01 12:00:00')"
            )
        )

    command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        migrated = conn.execute(
            text(
                "SELECT id, condition_json, action_json, group_id, payload_version, "
                "plan_id, fraction, hwm, deadline, pre_approved "
                "FROM rules ORDER BY id"
            )
        ).mappings().all()
        groups = conn.execute(
            text("SELECT id, group_key FROM rule_groups ORDER BY group_key")
        ).mappings().all()
        proposal_columns = {
            column["name"] for column in inspect(engine).get_columns("proposals")
        }
        source_rule_group_id = conn.scalar(
            text(
                "SELECT source_rule_group_id FROM proposals WHERE order_id=1"
            )
        )
        source_group_reconciliation_required = conn.scalar(
            text(
                "SELECT reconciliation_required FROM rule_groups "
                "WHERE id=:group_id"
            ),
            {"group_id": source_rule_group_id},
        )

    assert json.loads(migrated[0]["condition_json"]) == {
        "type": "price",
        "direction": "below",
        "price": 175,
    }
    assert json.loads(migrated[1]["condition_json"]) == {
        "type": "price",
        "direction": "above",
        "price": 200,
    }
    assert json.loads(migrated[2]["condition_json"]) == {
        "type": "trailing",
        "percent": 8,
    }
    assert json.loads(migrated[3]["condition_json"]) == {
        "type": "time",
        "deadline": "2026-08-01 12:00:00",
    }
    assert all(
        json.loads(row["action_json"])["order_type"] == "market"
        for row in migrated[:4]
    )
    assert migrated[0]["group_id"] == migrated[1]["group_id"]
    assert migrated[2]["group_id"] != migrated[3]["group_id"]
    assert {row["group_key"] for row in groups} == {
        "legacy-plan-7",
        "legacy-rule-3",
        "legacy-rule-4",
        "legacy-rule-5",
    }
    assert all(row["payload_version"] == 1 for row in migrated[:4])
    assert migrated[4]["payload_version"] == 0
    assert json.loads(migrated[4]["condition_json"]) == {
        "retired_custom_shape": "preserve-me"
    }
    assert json.loads(migrated[4]["action_json"]) == {
        "historical_action": "preserve-me"
    }
    assert migrated[0]["plan_id"] == 7
    assert Decimal(str(migrated[1]["fraction"])) == Decimal("0.5")
    assert Decimal(str(migrated[2]["hwm"])) == Decimal("212.50")
    assert migrated[0]["pre_approved"] == 0
    assert migrated[1]["pre_approved"] == 0
    assert migrated[2]["pre_approved"] == 1
    assert "source_rule_group_id" in proposal_columns
    assert source_rule_group_id == migrated[0]["group_id"]
    assert source_group_reconciliation_required == 1


def test_rule_lease_upgrade_aborts_on_unknown_active_shape(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "unknown-active-rule.db", "20260724_0003"
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rules "
                "(ticker,condition_json,action_json,state,created_at,plan_id,kind,"
                "fraction,hwm,deadline,pre_approved) "
                "VALUES ('AAPL',:condition,:action,'active',CURRENT_TIMESTAMP,"
                "NULL,'price',NULL,NULL,NULL,0)"
            ),
            {
                "condition": json.dumps({"mystery": 1}),
                "action": json.dumps({"side": "buy", "notional": "50"}),
            },
        )

    with pytest.raises(RuntimeError, match="unknown active rule"):
        command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260724_0003"
        )
        assert "rule_groups" not in inspect(engine).get_table_names()


@pytest.mark.parametrize(
    ("state", "field", "value"),
    [
        ("active", "ticker", "   "),
        ("active", "ticker", "ABCDEFGHIJKLMNOPQ"),
        ("active", "fraction", "0"),
        ("active", "fraction", "1.01"),
        ("active", "fraction", "NaN"),
        ("active", "hwm", "0"),
        ("active", "hwm", "Infinity"),
        ("processing", "hwm", "Infinity"),
        ("active", "fraction", "0.0000001"),
        ("processing", "fraction", "0.1234567"),
        ("active", "hwm", "0.0000001"),
        ("processing", "hwm", "100000000000000"),
    ],
)
def test_rule_lease_upgrade_aborts_invalid_resumable_scalar_before_ddl(
    tmp_path, state, field, value
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"invalid-{field}-rule.db", "20260724_0003"
    )
    values = {"ticker": "AAPL", "fraction": None, "hwm": None}
    values[field] = value
    with engine.begin() as conn:
        _insert_legacy_rule(
            conn,
            rule_id=1,
            plan_id=None,
            state=state,
            **values,
        )

    with pytest.raises(RuntimeError, match="unknown active rule"):
        command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260724_0003"
        )
        assert "rule_groups" not in inspect(engine).get_table_names()


@pytest.mark.parametrize(
    ("state", "field", "value"),
    [
        ("active", "qty", "0.0000001"),
        ("processing", "qty", "0.0000001"),
        ("active", "qty", "100000000000000"),
        ("processing", "qty", "100000000000000"),
        ("active", "notional", "0.0000001"),
        ("processing", "notional", "0.0000001"),
        ("active", "notional", "100000000000000"),
        ("processing", "notional", "100000000000000"),
        ("active", "limit_price", "0.0000001"),
        ("processing", "limit_price", "0.0000001"),
        ("active", "limit_price", "100000000000000"),
        ("processing", "limit_price", "100000000000000"),
    ],
)
def test_rule_lease_upgrade_aborts_unpersistable_action_before_ddl(
    tmp_path, state, field, value
):
    engine, cfg = _engine_at_revision(
        tmp_path / "invalid-action-rule.db", "20260724_0003"
    )
    action = {
        "side": "buy",
        "order_type": "limit" if field == "limit_price" else "market",
        "qty": "1" if field == "limit_price" else value,
    }
    if field == "notional":
        action.pop("qty")
        action["notional"] = value
    elif field == "limit_price":
        action["limit_price"] = value

    with engine.begin() as conn:
        _insert_legacy_rule(
            conn,
            rule_id=1,
            plan_id=None,
            state=state,
            action=action,
        )

    with pytest.raises(RuntimeError, match="unknown active rule"):
        command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260724_0003"
        )
        assert "rule_groups" not in inspect(engine).get_table_names()


def test_rule_lease_upgrade_cancels_resumable_siblings_of_terminal_winner(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "terminal-rule-group.db", "20260724_0003"
    )
    with engine.begin() as conn:
        _insert_legacy_rule(
            conn, rule_id=1, plan_id=9, state="triggered"
        )
        _insert_legacy_rule(conn, rule_id=2, plan_id=9, state="active")
        _insert_legacy_rule(
            conn, rule_id=3, plan_id=9, state="processing"
        )

    command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        group = conn.execute(
            text(
                "SELECT state, terminal_rule_id FROM rule_groups "
                "WHERE group_key='legacy-plan-9'"
            )
        ).mappings().one()
        states = dict(
            conn.execute(
                text("SELECT id, state FROM rules ORDER BY id")
            ).all()
        )

    assert group == {"state": "triggered", "terminal_rule_id": 1}
    assert states == {1: "triggered", 2: "canceled", 3: "canceled"}


def test_rule_lease_upgrade_aborts_plan_with_multiple_terminal_winners(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "ambiguous-terminal-rule-group.db", "20260724_0003"
    )
    with engine.begin() as conn:
        _insert_legacy_rule(
            conn, rule_id=1, plan_id=10, state="triggered"
        )
        _insert_legacy_rule(conn, rule_id=2, plan_id=10, state="failed")

    with pytest.raises(RuntimeError, match="multiple terminal winners"):
        command.upgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260724_0003"
        )
        assert "rule_groups" not in inspect(engine).get_table_names()


def test_breaker_upgrade_preserves_every_legacy_latch_and_adds_account_risk_state(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "legacy-breakers.db", "20260724_0004"
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO killswitch_state "
                "(asset_class,tripped,tripped_at,reason,updated_at) VALUES "
                "('equity',1,'2026-07-24 10:00:00','equity loss',"
                "'2026-07-24 10:00:00'),"
                "('crypto',0,NULL,'','2026-07-24 10:01:00'),"
                "('operator_global',1,'2026-07-24 10:02:00','panic',"
                "'2026-07-24 10:02:00')"
            )
        )

    command.upgrade(cfg, "20260724_0005")

    with engine.connect() as conn:
        tables = set(inspect(engine).get_table_names())
        rows = conn.execute(
            text(
                "SELECT scope_key,kind,target,tripped,reason,generation "
                "FROM circuit_breaker_state ORDER BY scope_key"
            )
        ).mappings().all()

    assert "killswitch_state" not in tables
    assert "account_risk_state" in tables
    assert rows == [
        {
            "scope_key": "loss:crypto",
            "kind": "loss",
            "target": "crypto",
            "tripped": 0,
            "reason": "",
            "generation": 1,
        },
        {
            "scope_key": "loss:equity",
            "kind": "loss",
            "target": "equity",
            "tripped": 1,
            "reason": "equity loss",
            "generation": 1,
        },
        {
            "scope_key": "operator_global",
            "kind": "operator_global",
            "target": "",
            "tripped": 1,
            "reason": "panic",
            "generation": 1,
        },
    ]


def test_breaker_migration_widens_fill_precision_and_empty_downgrade_restores_it(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "fill-precision.db", "20260724_0004"
    )

    before = {
        column["name"]: column["type"]
        for column in inspect(engine).get_columns("fills")
    }
    assert (before["qty"].precision, before["qty"].scale) == (20, 6)
    assert (before["price"].precision, before["price"].scale) == (20, 6)

    command.upgrade(cfg, "20260724_0005")

    widened = {
        column["name"]: column["type"]
        for column in inspect(engine).get_columns("fills")
    }
    assert (widened["qty"].precision, widened["qty"].scale) == (24, 9)
    assert (widened["price"].precision, widened["price"].scale) == (24, 9)

    command.downgrade(cfg, "20260724_0004")

    restored = {
        column["name"]: column["type"]
        for column in inspect(engine).get_columns("fills")
    }
    assert (restored["qty"].precision, restored["qty"].scale) == (20, 6)
    assert (restored["price"].precision, restored["price"].scale) == (20, 6)


@pytest.mark.parametrize(
    ("case_name", "broker_fill_id"),
    [
        ("null-id", None),
        ("caller-supplied-id", "pre-0005-caller-supplied-id"),
    ],
)
def test_breaker_upgrade_quarantines_every_preexisting_fill_and_trips_drift(
    tmp_path,
    case_name,
    broker_fill_id,
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"legacy-{case_name}-fill.db",
        "20260724_0004",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,submission_attempt,"
                "acceptance_state,last_error_code,version) VALUES "
                "(1,'legacy-null-client','AAPL','sell','market',1,'canceled',"
                "'legacy-null-order',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'',"
                "'simple','{}',0,'accepted','',0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO fills "
                "(order_id,ticker,side,qty,price,broker_fill_id,filled_at) "
                "VALUES "
                "(1,'AAPL','sell',1,1,:broker_fill_id,CURRENT_TIMESTAMP)"
            ),
            {"broker_fill_id": broker_fill_id},
        )

    command.upgrade(cfg, "20260724_0005")

    with engine.connect() as conn:
        fill = conn.execute(
            text(
                "SELECT broker_fill_id,reconciliation_state "
                "FROM fills WHERE order_id=1"
            )
        ).mappings().one()
        order = conn.execute(
            text(
                "SELECT acceptance_state,last_error_code "
                "FROM orders WHERE id=1"
            )
        ).mappings().one()
        drift = conn.execute(
            text(
                "SELECT tripped,reason,actor,generation "
                "FROM circuit_breaker_state "
                "WHERE scope_key='broker_drift'"
            )
        ).mappings().one()

    assert fill == {
        "broker_fill_id": broker_fill_id,
        "reconciliation_state": "quarantined",
    }
    assert order == {
        "acceptance_state": "fill_reconcile_required",
        "last_error_code": "legacy_unidentified_fill",
    }
    assert drift["tripped"] == 1
    assert "legacy fill" in drift["reason"]
    assert drift["actor"] == "migration:0005"
    assert drift["generation"] == 1


def test_breaker_upgrade_resets_advanced_fill_cursor_for_full_recovery(
    tmp_path,
):
    engine, cfg = _engine_at_revision(
        tmp_path / "legacy-advanced-fill-cursor.db",
        "20260724_0004",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO orders "
                "(id,idempotency_key,ticker,side,order_type,qty,status,"
                "broker_order_id,created_at,updated_at,approval_reason,"
                "submission_kind,submission_payload_json,submission_attempt,"
                "submission_started_at,acceptance_state,last_error_code,"
                "version) VALUES "
                "(1,'cursor-replay-client','AAPL','sell','market',1,'canceled',"
                "'cursor-replay-order','2026-07-20 16:00:00',"
                "'2026-07-20 18:00:00','','simple','{}',0,"
                "'2026-07-20 16:59:00','accepted','',0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO fills "
                "(order_id,ticker,side,qty,price,broker_fill_id,filled_at) "
                "VALUES "
                "(1,'AAPL','sell',1,90,'cursor-replay-fill',"
                "'2026-07-20 17:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO reconciliation_cursors "
                "(broker,stream,last_activity_id,last_activity_at,version) "
                "VALUES "
                "('migration-replay','fills','cursor-ahead',"
                "'2026-07-25 17:00:00',9)"
            )
        )

    command.upgrade(cfg, "20260724_0005")

    with engine.connect() as conn:
        assert conn.scalar(
            text(
                "SELECT count(*) FROM reconciliation_cursors "
                "WHERE stream='fills'"
            )
        ) == 0
    # Runtime code is only supported at the current schema. Preserve the
    # point-in-time 0005 assertion above, then finish the upgrade before using
    # current ORM models and reconciliation services.
    command.upgrade(cfg, "head")

    exact_time = datetime(2026, 7, 20, 17, 0, tzinfo=timezone.utc)
    exact = BrokerFill(
        broker_fill_id="cursor-replay-fill",
        broker_order_id="cursor-replay-order",
        ticker="AAPL",
        side="sell",
        qty=Decimal("1"),
        price=Decimal("90"),
        filled_at=exact_time,
    )
    remote = OrderResult(
        "cursor-replay-client",
        "cursor-replay-order",
        OrderStatus.CANCELED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("90"),
    )

    class CursorAwareReplayBroker(MockBroker):
        reconciliation_key = "migration-replay"

        def __init__(self):
            super().__init__()
            self.after_calls = []
            self._orders_by_id["cursor-replay-order"] = remote
            self._orders_by_key["cursor-replay-client"] = remote

        def get_fill_activities(self, after=None):
            self.after_calls.append(after)
            return [exact] if after is None else []

    broker = CursorAwareReplayBroker()
    factory = make_session_factory(engine)
    cipher = SensitiveDataCipher(
        {"migration-test-field-key": b"f" * 32},
        active_key_id="migration-test-field-key",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE orders SET approval_reason=:reason WHERE id=1"
            ),
            {
                "reason": cipher.encrypt(
                    "legacy approval state",
                    SensitiveFieldRef(
                        "orders",
                        "1",
                        "approval_reason",
                        1,
                    ),
                )
            },
        )
    bind_sensitive_cipher(factory, cipher)
    reconciliation = ReconciliationService(
        factory,
        broker,
        OrderRepository(factory),
    )

    first = reconciliation.reconcile(
        actor="test:migration",
        reason="migration reconciliation",
        request_id="migration-reconciliation",
    )
    replay = ReconciliationService(
        factory,
        broker,
        OrderRepository(factory),
    ).reconcile(
        actor="test:migration",
        reason="migration reconciliation replay",
        request_id="migration-reconciliation-replay",
    )

    assert first.inserted_fills == 0
    assert replay.inserted_fills == 0
    assert broker.after_calls == [None, exact_time]
    with factory() as session:
        order = session.get(Order, 1)
        recovered = session.scalar(
            select(Fill).where(
                Fill.broker_fill_id == "cursor-replay-fill"
            )
        )
        cursor = session.get(
            ReconciliationCursor,
            ("migration-replay", "fills"),
        )
        assert order is not None
        assert recovered is not None
        assert cursor is not None
        assert order.acceptance_state == "accepted"
        assert recovered.reconciliation_state == "trusted"
        assert recovered.filled_at == exact_time
        assert session.scalar(
            select(func.count()).select_from(Fill)
        ) == 1
        assert cursor.last_activity_id == "cursor-replay-fill"
        assert cursor.last_activity_at == exact_time


@pytest.mark.parametrize(
    ("case_name", "fill_rows"),
    [
        (
            "recovered",
            [
                {
                    "broker_fill_id": "recovered-exact",
                    "reconciliation_state": "trusted",
                },
                {
                    "broker_fill_id": "recovered-legacy",
                    "reconciliation_state": "superseded",
                },
            ],
        ),
        (
            "unresolved",
            [
                {
                    "broker_fill_id": None,
                    "reconciliation_state": "quarantined",
                }
            ],
        ),
    ],
)
def test_breaker_downgrade_refuses_nonempty_fill_trust_ledger(
    tmp_path,
    case_name,
    fill_rows,
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"{case_name}-fill-ledger.db",
        "20260724_0005",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fills "
                "(order_id,ticker,side,qty,price,broker_fill_id,filled_at,"
                "reconciliation_state) VALUES "
                "(NULL,'AAPL','sell',1,90,:broker_fill_id,"
                "'2026-07-24 17:00:00',:reconciliation_state)"
            ),
            fill_rows,
        )

    with pytest.raises(RuntimeError, match="verified pre-upgrade backup"):
        command.downgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0005"
        assert conn.execute(
            text(
                "SELECT broker_fill_id,reconciliation_state "
                "FROM fills ORDER BY id"
            )
        ).mappings().all() == fill_rows
        tables = set(inspect(engine).get_table_names())
        assert "circuit_breaker_state" in tables
        assert "killswitch_state" not in tables


@pytest.mark.parametrize(
    "safety_state",
    [
        "account-risk",
        "fill-latch",
        "data-breaker",
        "drawdown-breaker",
        "liquidity-breaker",
        "broker-drift-breaker",
    ],
)
def test_breaker_downgrade_refuses_nonrepresentable_safety_state_before_ddl(
    tmp_path,
    safety_state,
):
    engine, cfg = _engine_at_revision(
        tmp_path / f"{safety_state}.db",
        "20260724_0005",
    )
    with engine.begin() as conn:
        if safety_state == "account-risk":
            conn.execute(
                text(
                    "INSERT INTO account_risk_state "
                    "(asset_class,high_water_mark,last_equity,updated_at) "
                    "VALUES ('equity',100000,95000,CURRENT_TIMESTAMP)"
                )
            )
        elif safety_state == "fill-latch":
            conn.execute(
                text(
                    "INSERT INTO orders "
                    "(id,idempotency_key,ticker,side,order_type,qty,status,"
                    "created_at,updated_at,approval_reason,submission_kind,"
                    "submission_payload_json,submission_attempt,"
                    "acceptance_state,last_error_code,version) VALUES "
                    "(1,'downgrade-latched-order','AAPL','buy','market',1,"
                    "'canceled',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'',"
                    "'simple','{}',0,'fill_reconcile_required',"
                    "'invalid_fill_activity',0)"
                )
            )
        else:
            kind, target = {
                "data-breaker": ("data", "equity"),
                "drawdown-breaker": ("drawdown", "equity"),
                "liquidity-breaker": ("liquidity", "AAPL"),
                "broker-drift-breaker": ("broker_drift", ""),
            }[safety_state]
            scope_key = f"{kind}:{target}" if target else kind
            conn.execute(
                text(
                    "INSERT INTO circuit_breaker_state "
                    "(scope_key,kind,target,tripped,reason,actor,generation,"
                    "updated_at) VALUES "
                    "(:scope_key,:kind,:target,1,'active safety state',"
                    "'test:review19',3,CURRENT_TIMESTAMP)"
                ),
                {
                    "scope_key": scope_key,
                    "kind": kind,
                    "target": target,
                },
            )

    with pytest.raises(RuntimeError, match="verified pre-upgrade backup"):
        command.downgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0005"
        assert conn.scalar(text("SELECT count(*) FROM fills")) == 0
        tables = set(inspect(engine).get_table_names())
        assert "account_risk_state" in tables
        assert "circuit_breaker_state" in tables
        assert "killswitch_state" not in tables
        assert "reconciliation_state" in {
            column["name"] for column in inspect(engine).get_columns("fills")
        }
        if safety_state == "account-risk":
            assert conn.execute(
                text(
                    "SELECT high_water_mark,last_equity "
                    "FROM account_risk_state WHERE asset_class='equity'"
                )
            ).one() == (100000, 95000)
        elif safety_state == "fill-latch":
            assert conn.execute(
                text(
                    "SELECT acceptance_state,last_error_code "
                    "FROM orders WHERE id=1"
                )
            ).one() == (
                "fill_reconcile_required",
                "invalid_fill_activity",
            )
        else:
            assert conn.execute(
                text(
                    "SELECT tripped,reason,actor,generation "
                    "FROM circuit_breaker_state"
                )
            ).one() == (
                1,
                "active safety state",
                "test:review19",
                3,
            )


def test_breaker_downgrade_allows_representable_empty_safety_state(tmp_path):
    engine, cfg = _engine_at_revision(
        tmp_path / "empty-fill-ledger.db",
        "20260724_0005",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO circuit_breaker_state "
                "(scope_key,kind,target,tripped,reason,actor,generation,"
                "updated_at) VALUES "
                "('loss:equity','loss','equity',1,'loss limit','test',1,"
                "CURRENT_TIMESTAMP),"
                "('operator_global','operator_global','',1,'operator panic',"
                "'test',2,CURRENT_TIMESTAMP)"
            )
        )

    command.downgrade(cfg, "20260724_0004")

    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "20260724_0004"
        assert "reconciliation_state" not in {
            column["name"] for column in inspect(engine).get_columns("fills")
        }
        tables = set(inspect(engine).get_table_names())
        assert "killswitch_state" in tables
        assert "circuit_breaker_state" not in tables
        assert conn.execute(
            text(
                "SELECT asset_class,tripped,reason "
                "FROM killswitch_state ORDER BY asset_class"
            )
        ).all() == [
            ("equity", 1, "loss limit"),
            ("operator_global", 1, "operator panic"),
        ]
