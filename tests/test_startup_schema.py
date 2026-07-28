"""Production roots must fail closed until the Alembic schema is current."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

import trading_assistant.logging as app_logging
from trading_assistant.db.migrate import upgrade
from trading_assistant.db.schema import SchemaOutOfDate
from trading_assistant.db.session import create_db_engine
from trading_assistant.preflight import SensitiveEncryptionStateInspector
from trading_assistant.security.crypto import SensitiveDataCipher
from trading_assistant.security.secrets import RuntimeSecrets


TEST_FIELD_KEY_ID = "configured-key-2026"
TEST_CIPHER = SensitiveDataCipher(
    {TEST_FIELD_KEY_ID: b"s" * 32},
    active_key_id=TEST_FIELD_KEY_ID,
)


def _revision_0004(tmp_path, name="startup.db"):
    path = tmp_path / name
    url = f"sqlite:///{path}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "20260724_0004")
    return create_db_engine(url), url


def _head_database(tmp_path, name="sensitive-head.db"):
    path = tmp_path / name
    url = f"sqlite:///{path}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return create_db_engine(url), url


def _patch_common_startup(monkeypatch, module, url):
    from trading_assistant.config import BrokerKind, load_config

    config = load_config(
        Path(__file__).resolve().parent.parent / "config.yaml"
    )
    config = config.model_copy(
        update={
            "trading": config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            )
        }
    )
    monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(
        module,
        "load_role_secrets",
        lambda _role, *, config: RuntimeSecrets(
            database_url=url,
            app_api_token="startup-schema-test-secret",
        ),
    )
    monkeypatch.setattr(app_logging, "register_all_secrets", lambda _secrets: None)


@pytest.mark.parametrize(
    "source",
    [
        "src/trading_assistant/app/main.py",
        "src/trading_assistant/daemon/main.py",
        "src/trading_assistant/mcp_server/server.py",
        "src/trading_assistant/preflight.py",
        "src/trading_assistant/ops/paper_drill.py",
    ],
)
def test_production_roots_never_call_create_all(source):
    assert "create_all" not in Path(source).read_text(encoding="utf-8")


def test_runtime_package_never_calls_create_all():
    offenders = [
        str(path)
        for path in Path("src/trading_assistant").rglob("*.py")
        if "Base.metadata.create_all(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_api_startup_and_in_place_upgrade_fail_closed_on_pre_tenure_schema(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.app import main as app_main

    engine, url = _revision_0004(tmp_path, "api-0004.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO killswitch_state "
                "(asset_class,tripped,tripped_at,reason,updated_at) VALUES "
                "('equity',1,CURRENT_TIMESTAMP,'equity loss',CURRENT_TIMESTAMP),"
                "('operator_global',1,CURRENT_TIMESTAMP,'panic',CURRENT_TIMESTAMP)"
            )
        )
    before = set(inspect(engine).get_table_names())
    _patch_common_startup(monkeypatch, app_main, url)

    with pytest.raises(SchemaOutOfDate, match="current='20260724_0004'"):
        app_main.build_default_stack()

    assert set(inspect(engine).get_table_names()) == before
    assert "circuit_breaker_state" not in before

    with pytest.raises(
        RuntimeError,
        match="^schema_maintenance_bootstrap_required$",
    ):
        upgrade(
            engine,
            backup_key=b"u" * 32,
            backup_key_id="startup-schema-backup-2026",
            backup_directory=tmp_path / "encrypted-backups",
        )
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT asset_class,tripped,reason FROM killswitch_state "
                "WHERE asset_class IN ('equity','operator_global') "
                "ORDER BY asset_class"
            )
        ).mappings().all()
    assert rows == [
        {
            "asset_class": "equity",
            "tripped": 1,
            "reason": "equity loss",
        },
        {
            "asset_class": "operator_global",
            "tripped": 1,
            "reason": "panic",
        },
    ]


def test_daemon_startup_fails_on_0004_before_constructing_monitor(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.daemon import main as daemon_main

    _engine, url = _revision_0004(tmp_path, "daemon-0004.db")
    _patch_common_startup(monkeypatch, daemon_main, url)

    with pytest.raises(SchemaOutOfDate, match="run .*upgrade"):
        daemon_main.build_monitor()


def test_mcp_startup_fails_on_0004_before_constructing_service(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.mcp_server import server

    _engine, url = _revision_0004(tmp_path, "mcp-0004.db")
    _patch_common_startup(monkeypatch, server, url)

    with pytest.raises(SchemaOutOfDate, match="run .*upgrade"):
        server.build_default_container()


def test_preflight_reports_outdated_schema_without_mutating_it(tmp_path):
    from trading_assistant.preflight import FAIL, _db

    engine, url = _revision_0004(tmp_path, "preflight-0004.db")
    before = set(inspect(engine).get_table_names())

    schema, wal, breakers = _db(SimpleNamespace(database_url=url))

    assert schema.status == FAIL
    assert wal.status == FAIL
    assert breakers.status == FAIL
    assert schema.detail == "schema_out_of_date"
    assert wal.detail == "schema_out_of_date"
    assert set(inspect(engine).get_table_names()) == before
    assert "circuit_breaker_state" not in before


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        ("required", "sensitive_migration_required"),
        ("migrating", "sensitive_migration_migrating"),
        ("rotating", "sensitive_migration_rotating"),
        ("failed", "sensitive_migration_failed"),
    ],
)
def test_sensitive_encryption_inspector_blocks_all_noncomplete_states(
    tmp_path,
    state,
    expected_code,
):
    engine, _url = _head_database(
        tmp_path,
        f"sensitive-{state}.db",
    )
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sensitive_migration_state SET "
                "state=:state, active_key_id='configured-key-2026', "
                "started_at=:started_at, completed_at=NULL, updated_at=:updated_at"
            ),
            {
                "state": state,
                "started_at": None if state == "required" else now,
                "updated_at": now,
            },
        )

    check = SensitiveEncryptionStateInspector(
        engine,
        schema_version=1,
        active_key_id="configured-key-2026",
    ).inspect()

    assert check.status == "blocked"
    assert check.code == expected_code


def test_sensitive_encryption_inspector_passes_only_consistent_complete_state(
    tmp_path,
):
    engine, _url = _head_database(tmp_path, "sensitive-complete.db")
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sensitive_migration_state SET "
                "schema_version=1,state='complete',"
                "active_key_id='configured-key-2026',"
                "rows_total=0,rows_completed=0,"
                "backup_path_hash=:backup_hash,"
                "started_at=:started_at,completed_at=:completed_at,"
                "updated_at=:updated_at"
            ),
            {
                "backup_hash": "a" * 64,
                "started_at": now - timedelta(minutes=2),
                "completed_at": now - timedelta(minutes=1),
                "updated_at": now,
            },
        )

    check = SensitiveEncryptionStateInspector(
        engine,
        schema_version=1,
        active_key_id="configured-key-2026",
        cipher=TEST_CIPHER,
    ).inspect()

    assert check.passed
    assert check.code == "ok"


@pytest.mark.parametrize(
    ("mutation", "schema_version", "active_key_id", "expected_code"),
    [
        (
            "UPDATE sensitive_migration_state SET schema_version=2",
            1,
            "configured-key-2026",
            "sensitive_schema_mismatch",
        ),
        (
            "UPDATE sensitive_migration_state "
            "SET active_key_id='different-key-2026'",
            1,
            "configured-key-2026",
            "sensitive_active_key_mismatch",
        ),
        (
            "UPDATE sensitive_migration_state SET rows_completed=6",
            1,
            "configured-key-2026",
            "sensitive_migration_state_invalid",
        ),
        (
            "UPDATE sensitive_migration_state SET completed_at=NULL",
            1,
            "configured-key-2026",
            "sensitive_migration_state_invalid",
        ),
        (
            "UPDATE sensitive_migration_state SET started_at=updated_at",
            1,
            "configured-key-2026",
            "sensitive_migration_state_invalid",
        ),
        (
            "UPDATE sensitive_migration_state SET backup_path_hash=NULL",
            1,
            "configured-key-2026",
            "sensitive_migration_state_invalid",
        ),
    ],
)
def test_sensitive_encryption_inspector_fails_closed_on_inconsistent_complete(
    tmp_path,
    mutation,
    schema_version,
    active_key_id,
    expected_code,
):
    engine, _url = _head_database(
        tmp_path,
        hashlib.sha256(mutation.encode()).hexdigest() + ".db",
    )
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sensitive_migration_state SET "
                "schema_version=1,state='complete',"
                "active_key_id='configured-key-2026',"
                "rows_total=0,rows_completed=0,"
                "backup_path_hash=:backup_hash,"
                "started_at=:started_at,completed_at=:completed_at,"
                "updated_at=:updated_at"
            ),
            {
                "backup_hash": "a" * 64,
                "started_at": now - timedelta(minutes=2),
                "completed_at": now - timedelta(minutes=1),
                "updated_at": now,
            },
        )
        connection.execute(text("PRAGMA ignore_check_constraints=ON"))
        connection.execute(text(mutation))

    check = SensitiveEncryptionStateInspector(
        engine,
        schema_version=schema_version,
        active_key_id=active_key_id,
        cipher=TEST_CIPHER,
    ).inspect()

    assert check.status == "blocked"
    assert check.code == expected_code


def test_sensitive_encryption_inspector_rejects_missing_or_multiple_singleton(
    tmp_path,
):
    engine, _url = _head_database(tmp_path, "sensitive-cardinality.db")
    inspector = SensitiveEncryptionStateInspector(
        engine,
        schema_version=1,
        active_key_id="configured-key-2026",
    )
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM sensitive_migration_state"))
    assert inspector.inspect().code == "sensitive_migration_state_invalid"

    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA ignore_check_constraints=ON"))
        for singleton_id in (1, 2):
            connection.execute(
                text(
                    "INSERT INTO sensitive_migration_state "
                    "(singleton_id,schema_version,state,active_key_id,"
                    "rows_total,rows_completed,updated_at) VALUES "
                    "(:singleton_id,1,'required','configured-key-2026',"
                    "0,0,:updated_at)"
                ),
                {"singleton_id": singleton_id, "updated_at": now},
            )
    assert inspector.inspect().code == "sensitive_migration_state_invalid"
