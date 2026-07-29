import base64
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alpaca.trading.client import TradingClient
from pydantic import SecretStr
from sqlalchemy import select, text

from trading_assistant.broker.alpaca import AlpacaBroker
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    Account,
    BrokerFill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderTimeInForce,
    OrderType,
    Position,
    Quote,
)
from trading_assistant.config import BrokerKind, Secrets, TradingMode, load_config
from trading_assistant.db.models import AuditEvent, Fill, Order
from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.orders.submission import OrderSubmissionService
from trading_assistant.ops.safety_drill import (
    SafetyDrillError,
    _CrashAfterAcceptanceOnceBroker,
    _DrillCrash,
    _cancel_validated_tagged_open,
    _compensate_drill_fill,
    _online_copy,
    _validate_credentialed_paper as _production_validate_credentialed_paper,
    main,
    run_safety_drill as _production_run_safety_drill,
)
import trading_assistant.ops.safety_drill as safety_drill_module
from trading_assistant.risk.clock import FakeClock
from trading_assistant.rules.repository import RuleRepository
from trading_assistant.security.crypto import SensitiveDataCipher
from trading_assistant.security.sensitive_fields import (
    bind_sensitive_cipher,
    persist_sensitive,
)
from trading_assistant.security.secrets import RuntimeSecrets
from trading_assistant.service import TradingService
from tests.safety_helpers import bootstrap_database_to_revision


_PAPER_PREEXISTING_FILLED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
_PAPER_PREEXISTING_SUBMITTED_AT = datetime(
    2025,
    12,
    31,
    tzinfo=timezone.utc,
)
_DRILL_CIPHER = SensitiveDataCipher(
    {"local-primary-2026-07": b"d" * 32},
    active_key_id="local-primary-2026-07",
)


def _upgrade_database(path) -> None:
    bootstrap_database_to_revision(f"sqlite:///{path}", "head")


def _safe_config(app_config):
    return app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            ),
        }
    )


def test_drill_copy_secrets_preserve_secretstr_masking(
    app_config,
    monkeypatch,
    tmp_path,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "copy.db"
    _upgrade_database(primary)
    original_token = "safety-drill-secretstr-marker"
    captured = []
    captured_roles = []

    class CapturedDrillSecrets(RuntimeError):
        pass

    def capture_container(_config, secrets, **kwargs):
        captured.append(secrets)
        captured_roles.append(kwargs.get("runtime_role"))
        raise CapturedDrillSecrets

    monkeypatch.setattr(
        safety_drill_module,
        "build_test_container",
        capture_container,
    )

    with pytest.raises(CapturedDrillSecrets):
        _production_run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=MockBroker(prices={"AAPL": Decimal("100")}),
            secrets=RuntimeSecrets(
                database_url=f"sqlite:///{primary}",
                app_api_token=original_token,
            ),
        )

    drill_secrets = captured[0]
    assert isinstance(drill_secrets.database_url, SecretStr)
    assert isinstance(drill_secrets.app_api_token, SecretStr)
    dumped = drill_secrets.model_dump(mode="json")
    assert dumped["database_url"] == "**********"
    assert dumped["app_api_token"] == "**********"
    assert original_token not in repr(drill_secrets)
    assert str(destination) not in repr(drill_secrets)
    assert captured_roles == ["safety-drill"]


def _runtime_secrets_from_test_environment(
    base: RuntimeSecrets | None = None,
) -> RuntimeSecrets:
    current = base or RuntimeSecrets()
    values = {
        "field_encryption_keys": (
            current.field_encryption_keys
            or {
                "local-primary-2026-07": base64.b64encode(
                    b"d" * 32
                ).decode("ascii")
            }
        )
    }
    for env_name, field_name in (
        ("DATABASE_URL", "database_url"),
        ("APP_API_TOKEN", "app_api_token"),
        ("ALPACA_API_KEY", "alpaca_api_key"),
        ("ALPACA_SECRET_KEY", "alpaca_secret_key"),
        ("LIVE_TRADING_CONFIRM", "live_trading_confirm"),
    ):
        value = os.environ.get(env_name)
        if value is not None:
            values[field_name] = value
    return current.model_copy(update=values)


def run_safety_drill(**kwargs):
    kwargs.setdefault(
        "secrets",
        _runtime_secrets_from_test_environment(),
    )
    return _production_run_safety_drill(**kwargs)


def _validate_credentialed_paper(
    broker,
    secrets,
    config=None,
):
    return _production_validate_credentialed_paper(
        broker,
        _runtime_secrets_from_test_environment(secrets),
        config or _safe_config(load_config()),
    )


def _primary_manifest(path: Path) -> tuple[bytes, tuple, tuple]:
    content = path.read_bytes()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        schema = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "ORDER BY type,name"
            )
        )
        state = tuple(connection.execute("SELECT version_num FROM alembic_version"))
    return content, schema, state


def _sqlite_file_snapshot(path: Path) -> dict[str, tuple | None]:
    snapshot: dict[str, tuple | None] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = path.with_name(f"{path.name}{suffix}")
        if not candidate.exists():
            snapshot[suffix or "main"] = None
            continue
        metadata = os.lstat(candidate)
        snapshot[suffix or "main"] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            candidate.read_bytes(),
        )
    return snapshot


def _assert_source_files_unchanged_allowing_shm_read_marks(
    before: dict[str, tuple | None],
    after: dict[str, tuple | None],
) -> None:
    assert set(before) == set(after)
    assert after["main"] == before["main"]
    assert after["-wal"] == before["-wal"]
    assert after["-journal"] == before["-journal"]
    if before["-shm"] is None:
        assert after["-shm"] is None
    else:
        assert after["-shm"] is not None
        assert after["-shm"][:4] == before["-shm"][:4]


def _open_wal_source(
    path: Path,
    *,
    checkpointed: bool = False,
) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "INSERT INTO heartbeats (source, at) "
        "VALUES ('task-10-source-sentinel', CURRENT_TIMESTAMP)"
    )
    connection.commit()
    if checkpointed:
        assert connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone() == (0, 0, 0)
    assert path.with_name(f"{path.name}-wal").exists()
    assert path.with_name(f"{path.name}-shm").exists()
    return connection


def test_safety_drill_requires_every_gate(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    broker = MockBroker(prices={"AAPL": Decimal("100")})

    report = run_safety_drill(
        database_copy=tmp_path / "operator-copy.db",
        config=_safe_config(app_config),
        broker=broker,
    )

    assert report.schema_current
    assert report.auth_fail_closed
    assert report.crash_recovered_without_duplicate
    assert report.oco_single_terminal
    assert report.breakers_persisted
    assert report.reconciliation_clean
    assert report.safe


def test_mock_drill_leaves_primary_bytes_schema_and_state_unchanged(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    engine = create_db_engine(f"sqlite:///{primary}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO heartbeats (source, at) "
                "VALUES ('primary-sentinel', CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    before = _primary_manifest(primary)

    report = run_safety_drill(
        database_copy=tmp_path / "copy.db",
        config=_safe_config(app_config),
        broker=MockBroker(prices={"AAPL": Decimal("100")}),
    )

    assert report.safe
    assert _primary_manifest(primary) == before
    assert oct((tmp_path / "copy.db").stat().st_mode & 0o777) == "0o600"


def test_source_backup_connection_is_read_only_and_preserves_main_wal_shm(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "copy.db"
    _upgrade_database(primary)
    writer = _open_wal_source(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    before = _sqlite_file_snapshot(primary)
    real_connect = sqlite3.connect
    readonly_source_seen = False

    def checked_connect(database, *args, **kwargs):
        nonlocal readonly_source_seen
        connection = real_connect(database, *args, **kwargs)
        if (
            isinstance(database, str)
            and database.startswith("file:")
            and "mode=ro" in database
        ):
            readonly_source_seen = True
            assert kwargs.get("uri") is True
            with pytest.raises(
                sqlite3.OperationalError,
                match="readonly|read-only",
            ):
                connection.execute(
                    "INSERT INTO heartbeats (source, at) "
                    "VALUES ('forbidden-source-write', CURRENT_TIMESTAMP)"
                )
            connection.rollback()
        return connection

    monkeypatch.setattr(safety_drill_module.sqlite3, "connect", checked_connect)
    try:
        report = run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=MockBroker(prices={"AAPL": Decimal("100")}),
        )
        after = _sqlite_file_snapshot(primary)
        source_state = tuple(
            writer.execute(
                "SELECT source FROM heartbeats "
                "WHERE source = 'task-10-source-sentinel'"
            )
        )
    finally:
        writer.close()

    assert report.safe
    assert readonly_source_seen
    _assert_source_files_unchanged_allowing_shm_read_marks(before, after)
    assert source_state == (("task-10-source-sentinel",),)
    with sqlite3.connect(destination) as copied:
        assert copied.execute(
            "SELECT source FROM heartbeats "
            "WHERE source = 'task-10-source-sentinel'"
        ).fetchone() == ("task-10-source-sentinel",)


def test_active_wal_backup_opens_an_inode_bound_private_alias(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "copy.db"
    _upgrade_database(primary)
    writer = _open_wal_source(primary)
    before = _sqlite_file_snapshot(primary)
    real_connect = sqlite3.connect
    alias_seen = False

    def inspect_source_alias(database, *args, **kwargs):
        nonlocal alias_seen
        raw = str(database)
        if raw.startswith("file:") and "mode=ro" in raw:
            from urllib.parse import unquote, urlsplit

            alias = Path(unquote(urlsplit(raw).path))
            alias_seen = True
            assert alias != primary
            assert alias.parent.parent == primary.parent
            assert stat.S_IMODE(alias.parent.stat().st_mode) == 0o700
            assert (alias.stat().st_dev, alias.stat().st_ino) == (
                primary.stat().st_dev,
                primary.stat().st_ino,
            )
            assert (
                alias.with_name(f"{alias.name}-wal").stat().st_ino
                == primary.with_name(f"{primary.name}-wal").stat().st_ino
            )
            assert (
                alias.with_name(f"{alias.name}-shm").stat().st_ino
                == primary.with_name(f"{primary.name}-shm").stat().st_ino
            )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        safety_drill_module.sqlite3,
        "connect",
        inspect_source_alias,
    )
    try:
        assert _online_copy(primary, destination) == destination
        after = _sqlite_file_snapshot(primary)
    finally:
        writer.close()

    assert alias_seen
    _assert_source_files_unchanged_allowing_shm_read_marks(before, after)
    with sqlite3.connect(destination) as copied:
        assert copied.execute(
            "SELECT source FROM heartbeats "
            "WHERE source = 'task-10-source-sentinel'"
        ).fetchone() == ("task-10-source-sentinel",)
    assert not tuple(primary.parent.glob(".safety-drill-db-*"))


def test_binding_open_failure_removes_exact_created_empty_private_directory(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "copy.db"
    _upgrade_database(primary)
    real_open = os.open

    def fail_binding_open(path, *args, **kwargs):
        if (
            isinstance(path, str)
            and path.startswith(".safety-drill-db-")
        ):
            raise OSError("injected binding open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(safety_drill_module.os, "open", fail_binding_open)

    with pytest.raises(SafetyDrillError):
        _online_copy(primary, destination)

    assert not tuple(tmp_path.glob(".safety-drill-db-*"))


def test_binding_post_open_initialization_failure_removes_private_directory(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "copy.db"
    _upgrade_database(primary)
    real_fchmod = os.fchmod

    def fail_binding_fchmod(descriptor, mode):
        if mode == 0o700:
            raise OSError("injected binding fchmod failure")
        return real_fchmod(descriptor, mode)

    monkeypatch.setattr(
        safety_drill_module.os,
        "fchmod",
        fail_binding_fchmod,
    )

    with pytest.raises(SafetyDrillError):
        _online_copy(primary, destination)

    assert not tuple(tmp_path.glob(".safety-drill-db-*"))


def test_binding_open_failure_never_deletes_replacement_directory(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "copy.db"
    _upgrade_database(primary)
    real_open = os.open
    replacement_name = None
    held_name = None

    def replace_binding_then_fail(path, *args, **kwargs):
        nonlocal replacement_name, held_name
        if (
            isinstance(path, str)
            and path.startswith(".safety-drill-db-")
            and replacement_name is None
        ):
            parent_fd = kwargs["dir_fd"]
            replacement_name = path
            held_name = f"{path}.held"
            os.rename(
                path,
                held_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.mkdir(path, 0o700, dir_fd=parent_fd)
            raise OSError("injected replacement before open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(
        safety_drill_module.os,
        "open",
        replace_binding_then_fail,
    )

    with pytest.raises(SafetyDrillError):
        _online_copy(primary, destination)

    assert replacement_name is not None
    assert held_name is not None
    replacement = tmp_path / replacement_name
    held = tmp_path / held_name
    assert replacement.is_dir()
    assert held.is_dir()
    replacement.rmdir()
    held.rmdir()


def test_online_copy_defeats_source_swap_open_restore_race(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    replacement = tmp_path / "replacement.db"
    held_original = tmp_path / "held-original.db"
    destination = tmp_path / "copy.db"
    _upgrade_database(primary)
    _upgrade_database(replacement)
    with sqlite3.connect(primary) as connection:
        connection.execute(
            "INSERT INTO heartbeats (source, at) "
            "VALUES ('original-inode', CURRENT_TIMESTAMP)"
        )
    with sqlite3.connect(replacement) as connection:
        connection.execute(
            "INSERT INTO heartbeats (source, at) "
            "VALUES ('replacement-inode', CURRENT_TIMESTAMP)"
        )
    real_connect = sqlite3.connect
    raced = False

    def swap_open_restore(database, *args, **kwargs):
        nonlocal raced
        raw = str(database)
        if raw.startswith("file:") and "mode=ro" in raw and not raced:
            primary.rename(held_original)
            replacement.rename(primary)
            try:
                connection = real_connect(database, *args, **kwargs)
            finally:
                primary.rename(replacement)
                held_original.rename(primary)
            raced = True
            return connection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        safety_drill_module.sqlite3,
        "connect",
        swap_open_restore,
    )

    assert _online_copy(primary, destination) == destination

    assert raced
    with sqlite3.connect(destination) as copied:
        sources = {
            row[0]
            for row in copied.execute(
                "SELECT source FROM heartbeats "
                "WHERE source IN ('original-inode', 'replacement-inode')"
            )
        }
    assert sources == {"original-inode"}
    assert not tuple(primary.parent.glob(".safety-drill-db-*"))


def test_online_copy_quotes_special_characters_in_read_only_source_uri(
    tmp_path,
    monkeypatch,
):
    seeded = tmp_path / "seed.db"
    primary = tmp_path / "primary with # and ?.db"
    destination = tmp_path / "copy.db"
    _upgrade_database(seeded)
    seeded.rename(primary)
    real_connect = sqlite3.connect
    quoted_source_seen = False

    def checked_connect(database, *args, **kwargs):
        nonlocal quoted_source_seen
        raw = str(database)
        if raw.startswith("file:") and "mode=ro" in raw:
            quoted_source_seen = True
            assert "%23" in raw and "%3F" in raw
            assert kwargs.get("uri") is True
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(safety_drill_module.sqlite3, "connect", checked_connect)

    assert _online_copy(primary, destination) == destination
    assert quoted_source_seen


def test_source_main_wal_shm_identity_is_unchanged_when_a_gate_fails(
    tmp_path,
    app_config,
    monkeypatch,
):
    class QuoteFailureBroker(MockBroker):
        def get_quote(self, ticker):
            raise RuntimeError("offline quote failure")

    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    writer = _open_wal_source(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    before = _sqlite_file_snapshot(primary)
    try:
        report = run_safety_drill(
            database_copy=tmp_path / "failed-gate-copy.db",
            config=_safe_config(app_config),
            broker=QuoteFailureBroker(),
        )
        after = _sqlite_file_snapshot(primary)
    finally:
        writer.close()

    assert report.safe is False
    _assert_source_files_unchanged_allowing_shm_read_marks(before, after)


def test_online_copy_requires_existing_regular_shm_for_wal_without_creating_it(
    tmp_path,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    writer = _open_wal_source(primary)
    shm = primary.with_name(f"{primary.name}-shm")
    shm.unlink()
    before = _sqlite_file_snapshot(primary)
    try:
        with pytest.raises(SafetyDrillError) as caught:
            _online_copy(primary, destination)
        after = _sqlite_file_snapshot(primary)
    finally:
        writer.close()

    assert caught.value.code == "database_copy_failed"
    assert after == before
    assert after["-shm"] is None
    assert not destination.exists()


def test_online_copy_refuses_closed_wal_before_read_connection_creates_sidecars(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    writer = _open_wal_source(primary, checkpointed=True)
    writer.close()
    assert not primary.with_name(f"{primary.name}-wal").exists()
    assert not primary.with_name(f"{primary.name}-shm").exists()
    real_connect = sqlite3.connect

    def forbid_source_connect(database, *args, **kwargs):
        if isinstance(database, str) and "mode=ro" in database:
            pytest.fail("closed WAL source reached a sidecar-creating open")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        safety_drill_module.sqlite3,
        "connect",
        forbid_source_connect,
    )

    with pytest.raises(SafetyDrillError) as caught:
        _online_copy(primary, destination)

    assert caught.value.code == "database_copy_failed"
    assert not primary.with_name(f"{primary.name}-wal").exists()
    assert not primary.with_name(f"{primary.name}-shm").exists()
    assert not destination.exists()


def test_online_copy_fails_closed_on_hot_journal_without_recovery_writes(
    tmp_path,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sqlite3, sys; "
                "c = sqlite3.connect(sys.argv[1]); "
                "c.execute('PRAGMA journal_mode=DELETE'); "
                "c.execute('PRAGMA synchronous=FULL'); "
                "c.execute('PRAGMA cache_size=5'); "
                "c.execute('BEGIN IMMEDIATE'); "
                "[c.execute(\"INSERT INTO heartbeats (source, at) "
                "VALUES (?, CURRENT_TIMESTAMP)\", "
                "(f'hot-{i}-' + 'x' * 1000,)) for i in range(2000)]; "
                "os._exit(0)"
            ),
            str(primary),
        ],
        check=True,
    )
    journal = primary.with_name(f"{primary.name}-journal")
    assert journal.is_file()
    assert journal.stat().st_size > 0
    assert journal.read_bytes()[:8] != b"\x00" * 8
    before = _sqlite_file_snapshot(primary)

    with pytest.raises(SafetyDrillError) as caught:
        _online_copy(primary, destination)

    assert caught.value.code == "database_copy_failed"
    assert _sqlite_file_snapshot(primary) == before
    assert not destination.exists()


def test_online_copy_refuses_source_beneath_symlink_component(tmp_path):
    real_parent = tmp_path / "real-source"
    linked_parent = tmp_path / "linked-source"
    real_parent.mkdir(mode=0o700)
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    primary = real_parent / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)

    with pytest.raises(SafetyDrillError) as caught:
        _online_copy(linked_parent / primary.name, destination)

    assert caught.value.code == "unsafe_primary_database"
    assert not destination.exists()


def test_safety_drill_refuses_final_source_symlink_without_resolving_it(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    linked_primary = tmp_path / "linked-primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    linked_primary.symlink_to(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{linked_primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=MockBroker(prices={"AAPL": Decimal("100")}),
        )

    assert caught.value.code == "unsafe_primary_database"
    assert not destination.exists()


def test_online_copy_refuses_group_or_world_writable_source_parent(tmp_path):
    source_parent = tmp_path / "writable-source"
    source_parent.mkdir(mode=0o700)
    primary = source_parent / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    source_parent.chmod(0o777)
    try:
        with pytest.raises(SafetyDrillError) as caught:
            _online_copy(primary, destination)
    finally:
        source_parent.chmod(0o700)

    assert caught.value.code == "unsafe_primary_database"
    assert not destination.exists()


def test_online_copy_refuses_main_replacement_between_hold_and_connect(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    replacement = tmp_path / "replacement.db"
    held_original = tmp_path / "held-original.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    _upgrade_database(replacement)
    real_connect = sqlite3.connect
    replaced = False

    def replace_before_source_connect(database, *args, **kwargs):
        nonlocal replaced
        raw = str(database)
        if "mode=ro" in raw and not replaced:
            primary.rename(held_original)
            replacement.rename(primary)
            replaced = True
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        safety_drill_module.sqlite3,
        "connect",
        replace_before_source_connect,
    )

    with pytest.raises(SafetyDrillError) as caught:
        _online_copy(primary, destination)

    assert caught.value.code == "database_copy_failed"
    assert replaced
    assert not destination.exists()


def test_online_copy_refuses_wal_sidecar_swap_during_source_connect(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    writer = _open_wal_source(primary)
    wal = primary.with_name(f"{primary.name}-wal")
    held_wal = tmp_path / "held-primary.db-wal"
    replacement_wal = tmp_path / "replacement-wal"
    replacement_wal.write_bytes(wal.read_bytes())
    real_connect = sqlite3.connect
    replaced = False

    def replace_before_source_connect(database, *args, **kwargs):
        nonlocal replaced
        raw = str(database)
        if "mode=ro" in raw and not replaced:
            wal.rename(held_wal)
            replacement_wal.rename(wal)
            replaced = True
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        safety_drill_module.sqlite3,
        "connect",
        replace_before_source_connect,
    )
    try:
        with pytest.raises(SafetyDrillError) as caught:
            _online_copy(primary, destination)
    finally:
        writer.close()

    assert caught.value.code == "database_copy_failed"
    assert replaced
    assert not destination.exists()


def test_online_copy_refuses_held_main_identity_mismatch(tmp_path, monkeypatch):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    real_stat = os.stat
    mismatched = False

    def mismatched_main(path, *args, **kwargs):
        nonlocal mismatched
        result = real_stat(path, *args, **kwargs)
        if (
            path == primary.name
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
            and not mismatched
        ):
            values = list(result)
            values[1] += 1
            mismatched = True
            return os.stat_result(values)
        return result

    monkeypatch.setattr(safety_drill_module.os, "stat", mismatched_main)

    with pytest.raises(SafetyDrillError) as caught:
        _online_copy(primary, destination)

    assert caught.value.code == "database_copy_failed"
    assert mismatched
    assert not destination.exists()


@pytest.mark.parametrize(
    "unsafe_update",
    [
        {"trading": {"mode": TradingMode.LIVE}},
        {"trading": {"broker": BrokerKind.MOCK}},
        {"features": {"auto_execute_preapproved_rules": True}},
        {"execution": {"prefer_bracket_orders": True}},
        {"llm": {"fallback_provider": "groq"}},
    ],
)
def test_unsafe_config_is_refused_before_copy_or_broker_mutation(
    tmp_path,
    app_config,
    monkeypatch,
    unsafe_update,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    config = _safe_config(app_config)
    field, values = next(iter(unsafe_update.items()))
    config = config.model_copy(
        update={
            field: getattr(config, field).model_copy(update=values),
        }
    )
    before = _primary_manifest(primary)

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=config,
            broker=MockBroker(prices={"AAPL": Decimal("100")}),
        )

    assert caught.value.code == "unsafe_configuration"
    assert not destination.exists()
    assert _primary_manifest(primary) == before


def test_refuses_relative_destination_without_creating_it(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.chdir(tmp_path)
    before = _primary_manifest(primary)

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=Path("relative.db"),
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_database_copy"
    assert not (tmp_path / "relative.db").exists()
    assert _primary_manifest(primary) == before


def test_refuses_primary_aliases_and_existing_destination_without_changes(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"operator evidence")
    before = _primary_manifest(primary)

    for destination in (primary, existing):
        prior = destination.read_bytes()
        with pytest.raises(SafetyDrillError) as caught:
            run_safety_drill(
                database_copy=destination,
                config=_safe_config(app_config),
                broker=MockBroker(),
            )
        assert caught.value.code == "unsafe_database_copy"
        assert destination.read_bytes() == prior

    symlink = tmp_path / "primary-symlink.db"
    symlink.symlink_to(primary)
    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=symlink,
            config=_safe_config(app_config),
            broker=MockBroker(),
        )
    assert caught.value.code == "unsafe_database_copy"

    hardlink = tmp_path / "primary-hardlink.db"
    os.link(primary, hardlink)
    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=hardlink,
            config=_safe_config(app_config),
            broker=MockBroker(),
        )
    assert caught.value.code == "unsafe_primary_database"

    assert _primary_manifest(primary) == before


def test_refuses_destination_beneath_symlinked_parent(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    real_parent = tmp_path / "real-parent"
    linked_parent = tmp_path / "linked-parent"
    real_parent.mkdir()
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=linked_parent / "copy.db",
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_database_copy"
    assert not (real_parent / "copy.db").exists()


def test_refuses_nested_existing_path_beneath_symlink_component(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    safe_parent = tmp_path / "safe-parent"
    real_parent = tmp_path / "real-parent"
    nested = real_parent / "already-existing"
    safe_parent.mkdir(mode=0o700)
    nested.mkdir(parents=True, mode=0o700)
    (safe_parent / "linked").symlink_to(
        real_parent,
        target_is_directory=True,
    )
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=(
                safe_parent / "linked" / "already-existing" / "copy.db"
            ),
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_database_copy"
    assert not (nested / "copy.db").exists()


def test_refuses_group_or_world_writable_destination_parent(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    writable_parent = tmp_path / "writable-parent"
    writable_parent.mkdir()
    writable_parent.chmod(0o777)
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=writable_parent / "copy.db",
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_database_copy"
    assert not (writable_parent / "copy.db").exists()


def test_copy_temp_is_private_regular_single_link_before_sqlite_connect(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "copy.db"
    _upgrade_database(primary)
    real_connect = sqlite3.connect
    inspected_temp = False

    def checked_connect(database, *args, **kwargs):
        nonlocal inspected_temp
        raw = str(database)
        if ".tmp" in raw and "mode=rw" in raw:
            from urllib.parse import unquote, urlsplit

            temp_path = Path(unquote(urlsplit(raw).path))
            metadata = os.lstat(temp_path)
            inspected_temp = True
            assert stat.S_ISREG(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o600
            assert metadata.st_nlink == 1
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(safety_drill_module.sqlite3, "connect", checked_connect)

    copied = _online_copy(primary, destination)

    assert copied == destination
    assert inspected_temp


def test_temp_symlink_swap_is_refused_without_touching_victim(
    tmp_path,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "copy.db"
    victim = tmp_path / "victim.db"
    _upgrade_database(primary)
    victim.write_bytes(b"do-not-touch")
    before = victim.read_bytes()
    real_connect = sqlite3.connect
    swapped = False

    def swap_before_connect(database, *args, **kwargs):
        nonlocal swapped
        raw = str(database)
        if ".tmp" in raw and not swapped:
            from urllib.parse import unquote, urlsplit

            temp_path = Path(unquote(urlsplit(raw).path))
            temp_path.unlink()
            temp_path.symlink_to(victim)
            swapped = True
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        safety_drill_module.sqlite3,
        "connect",
        swap_before_connect,
    )

    with pytest.raises(SafetyDrillError):
        _online_copy(primary, destination)

    assert swapped
    assert victim.read_bytes() == before
    assert not destination.exists()


def test_copy_publish_refuses_a_racing_overwrite(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "racing-evidence.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    before = _primary_manifest(primary)
    original_link = os.link

    def race_destination(source, target, *args, **kwargs):
        destination.write_bytes(b"operator evidence created during copy")
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", race_destination)

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_database_copy"
    assert destination.read_bytes() == b"operator evidence created during copy"
    assert _primary_manifest(primary) == before


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite:///:memory:",
        "postgresql://localhost/trading",
    ],
)
def test_refuses_non_file_sqlite_primary_before_destination_creation(
    tmp_path,
    app_config,
    monkeypatch,
    database_url,
):
    destination = tmp_path / "must-not-exist" / "copy.db"
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "unsafe_primary_database"
    assert not destination.parent.exists()


def test_invalid_sqlite_primary_does_not_publish_a_copy(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "not-sqlite.db"
    destination = tmp_path / "copy.db"
    primary.write_text("not a sqlite database", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    before = primary.read_bytes()

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=MockBroker(),
        )

    assert caught.value.code == "invalid_primary_database"
    assert not destination.exists()
    assert primary.read_bytes() == before


def test_gate_failure_is_sanitized_and_never_claimed_safe(
    tmp_path,
    app_config,
    monkeypatch,
):
    secret = "provider-secret-must-not-escape"

    class UnavailableBroker(MockBroker):
        def get_quote(self, ticker):
            raise RuntimeError(secret)

    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")

    report = run_safety_drill(
        database_copy=tmp_path / "evidence.db",
        config=_safe_config(app_config),
        broker=UnavailableBroker(),
    )

    payload = json.dumps(report.as_dict(), sort_keys=True)
    assert report.safe is False
    assert report.crash_recovered_without_duplicate is False
    assert "crash:unconfirmed" in report.details
    assert secret not in payload


def test_mock_cli_emits_machine_readable_safe_json(
    tmp_path,
    monkeypatch,
    capsys,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    monkeypatch.setattr(
        safety_drill_module,
        "load_role_secrets",
        lambda role, *, config: _runtime_secrets_from_test_environment(),
    )

    exit_code = main(
        [
            "--database-copy",
            str(tmp_path / "cli-copy.db"),
            "--mock",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["safe"] is True
    assert payload["details"][0] == "mode:mock"


class PaperStateBroker(AlpacaBroker):
    """Offline Alpaca-shaped broker with observable paper-account manifests."""

    reconciliation_key = "alpaca"

    def __init__(
        self,
        *,
        fill_initial: bool = False,
        quote_ask: Decimal = Decimal("100.05"),
        concurrent_position_delta: Decimal = Decimal("0"),
        omit_initial_fill_record: bool = False,
        fail_fill_read_after_submissions: int | None = None,
        identity_failures: int = 0,
        identity_base_failure: BaseException | None = None,
        delay_compensation_fill: bool = False,
        hold_compensation_open: bool = False,
        partial_initial: bool = False,
        fail_cancel: bool = False,
        unconfirmed_cancel: bool = False,
        fill_after_cancel_failure: bool = False,
    ) -> None:
        class OfflineOfficialPaperTarget:
            _sandbox = True
            _base_url = "https://paper-api.alpaca.markets"

        super().__init__(OfflineOfficialPaperTarget(), object())
        self.fill_initial = fill_initial
        self.quote_ask = quote_ask
        self.concurrent_position_delta = concurrent_position_delta
        self.omit_initial_fill_record = omit_initial_fill_record
        self.fail_fill_read_after_submissions = (
            fail_fill_read_after_submissions
        )
        self.fill_read_failed = False
        self.identity_failures = identity_failures
        self.identity_base_failure = identity_base_failure
        self.delay_compensation_fill = delay_compensation_fill
        self.hold_compensation_open = hold_compensation_open
        self.partial_initial = partial_initial
        self.fail_cancel = fail_cancel
        self.unconfirmed_cancel = unconfirmed_cancel
        self.fill_after_cancel_failure = fill_after_cancel_failure
        self.late_fill_applied = False
        self.on_identity_lookup = None
        self.submit_requests: list[OrderRequest] = []
        self.cancel_ids: list[str] = []
        self.status_ids: list[str] = []
        self._positions: dict[str, Position] = {
            "AAPL": Position(
                "AAPL",
                Decimal("5"),
                Decimal("100"),
                Decimal("100"),
                Decimal("0"),
            )
        }
        self._fills: list[BrokerFill] = [
            BrokerFill(
                broker_fill_id="paper-preexisting-fill",
                broker_order_id="paper-history",
                ticker="AAPL",
                side=OrderSide.BUY.value,
                qty=Decimal("5"),
                price=Decimal("100"),
                filled_at=_PAPER_PREEXISTING_FILLED_AT,
            )
        ]
        self._orders_by_id: dict[str, OrderResult] = {
            "paper-preexisting": OrderResult(
                idempotency_key="paper-preexisting-client",
                broker_order_id="paper-preexisting",
                status=OrderStatus.SUBMITTED,
                ticker="AAPL",
            ),
            "paper-history": OrderResult(
                idempotency_key="paper-history-client",
                broker_order_id="paper-history",
                status=OrderStatus.FILLED,
                filled_qty=Decimal("5"),
                avg_fill_price=Decimal("100"),
                ticker="AAPL",
            ),
        }
        self._orders_by_key = {
            order.idempotency_key: order
            for order in self._orders_by_id.values()
        }

    def get_quote(self, ticker: str) -> Quote:
        now = datetime.now(timezone.utc)
        return Quote(
            ticker=ticker.upper(),
            bid=Decimal("99.95"),
            ask=self.quote_ask,
            last=Decimal("100"),
            prev_close=Decimal("100"),
            as_of=now,
            book_as_of=now,
            trade_as_of=now,
        )

    def get_account(self) -> Account:
        return Account(
            buying_power=Decimal("100000"),
            equity=Decimal("100000"),
            cash=Decimal("100000"),
        )

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_fill_activities(self, after=None) -> list[BrokerFill]:
        if (
            self.fail_fill_read_after_submissions is not None
            and len(self.submit_requests)
            >= self.fail_fill_read_after_submissions
            and not self.fill_read_failed
        ):
            self.fill_read_failed = True
            raise RuntimeError("injected fill reconciliation failure")
        return list(self._fills)

    def _apply_position_delta(self, ticker: str, delta: Decimal) -> None:
        prior = self._positions.get(ticker)
        new_qty = (prior.qty if prior is not None else Decimal("0")) + delta
        if new_qty:
            self._positions[ticker] = Position(
                ticker,
                new_qty,
                Decimal("100"),
                Decimal("100"),
                Decimal("0"),
            )
        else:
            self._positions.pop(ticker, None)

    def submit_order(self, order: OrderRequest) -> OrderResult:
        existing = self._orders_by_key.get(order.idempotency_key)
        if existing is not None:
            return existing
        self.submit_requests.append(order)
        broker_id = f"paper-drill-{len(self.submit_requests)}"
        fully_filled = (
            len(self.submit_requests) == 1
            and self.fill_initial
        ) or (
            len(self.submit_requests) > 1
            and not self.delay_compensation_fill
        )
        partially_filled = (
            len(self.submit_requests) == 1
            and self.partial_initial
        )
        status = (
            OrderStatus.FILLED
            if fully_filled
            else (
                OrderStatus.PARTIALLY_FILLED
                if partially_filled
                else OrderStatus.SUBMITTED
            )
        )
        filled_qty = (
            order.qty
            if fully_filled
            else (
                order.qty / Decimal("2")
                if partially_filled and order.qty is not None
                else Decimal("0")
            )
        )
        result = OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=broker_id,
            status=status,
            filled_qty=filled_qty or Decimal("0"),
            avg_fill_price=(
                Decimal("100")
                if fully_filled or partially_filled
                else None
            ),
            ticker=order.ticker,
        )
        self._orders_by_id[broker_id] = result
        self._orders_by_key[order.idempotency_key] = result
        if fully_filled or partially_filled:
            assert order.qty is not None
            signed = (
                filled_qty
                if order.side is OrderSide.BUY
                else -filled_qty
            )
            self._apply_position_delta(order.ticker, signed)
            if not (
                len(self.submit_requests) == 1
                and self.omit_initial_fill_record
            ):
                self._fills.append(
                    BrokerFill(
                        broker_fill_id=f"paper-fill-{len(self._fills) + 1}",
                        broker_order_id=broker_id,
                        ticker=order.ticker,
                        side=order.side.value,
                        qty=filled_qty,
                        price=Decimal("100"),
                        filled_at=datetime.now(timezone.utc),
                    )
                )
            if (
                len(self.submit_requests) == 1
                and self.concurrent_position_delta
            ):
                concurrent_side = (
                    OrderSide.BUY
                    if self.concurrent_position_delta > 0
                    else OrderSide.SELL
                )
                concurrent_qty = abs(self.concurrent_position_delta)
                self._apply_position_delta(
                    order.ticker,
                    self.concurrent_position_delta,
                )
                self._orders_by_id["paper-concurrent"] = OrderResult(
                    idempotency_key="paper-concurrent-client",
                    broker_order_id="paper-concurrent",
                    status=OrderStatus.FILLED,
                    filled_qty=concurrent_qty,
                    avg_fill_price=Decimal("100"),
                    ticker=order.ticker,
                )
                self._orders_by_key["paper-concurrent-client"] = (
                    self._orders_by_id["paper-concurrent"]
                )
                self._fills.append(
                    BrokerFill(
                        broker_fill_id="paper-concurrent-fill",
                        broker_order_id="paper-concurrent",
                        ticker=order.ticker,
                        side=concurrent_side.value,
                        qty=concurrent_qty,
                        price=Decimal("100"),
                        filled_at=datetime.now(timezone.utc),
                    )
                )
        return result

    def get_order_by_client_id(self, client_order_id: str):
        if client_order_id.startswith("safety-drill-"):
            if self.on_identity_lookup is not None:
                self.on_identity_lookup(client_order_id)
            if self.identity_base_failure is not None:
                failure = self.identity_base_failure
                self.identity_base_failure = None
                raise failure
            if self.identity_failures:
                self.identity_failures -= 1
                raise RuntimeError("injected reconciliation failure")
        return self._orders_by_key.get(client_order_id)

    def get_open_orders(self) -> list[OrderResult]:
        return [
            order
            for order in self._orders_by_id.values()
            if order.status
            in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}
        ]

    def get_order_status(self, order_id: str) -> OrderResult:
        self.status_ids.append(order_id)
        pending = self._orders_by_id[order_id]
        if (
            self.delay_compensation_fill
            and not self.hold_compensation_open
            and pending.idempotency_key.endswith("-compensate")
            and pending.status is OrderStatus.SUBMITTED
        ):
            request = next(
                request
                for request in self.submit_requests
                if request.idempotency_key == pending.idempotency_key
            )
            assert request.qty is not None
            signed = (
                request.qty
                if request.side is OrderSide.BUY
                else -request.qty
            )
            self._apply_position_delta(request.ticker, signed)
            self._fills.append(
                BrokerFill(
                    broker_fill_id=f"paper-fill-{len(self._fills) + 1}",
                    broker_order_id=order_id,
                    ticker=request.ticker,
                    side=request.side.value,
                    qty=request.qty,
                    price=Decimal("100"),
                    filled_at=datetime.now(timezone.utc),
                )
            )
            filled = OrderResult(
                idempotency_key=pending.idempotency_key,
                broker_order_id=order_id,
                status=OrderStatus.FILLED,
                filled_qty=request.qty,
                avg_fill_price=Decimal("100"),
                ticker=request.ticker,
            )
            self._orders_by_id[order_id] = filled
            self._orders_by_key[pending.idempotency_key] = filled
        return self._orders_by_id[order_id]

    def cancel_order(self, order_id: str) -> OrderResult:
        assert order_id not in {"paper-preexisting", "paper-history"}
        prior = self._orders_by_id[order_id]
        if self.unconfirmed_cancel:
            return prior
        if self.fail_cancel:
            if (
                self.fill_after_cancel_failure
                and not self.late_fill_applied
                and prior.status is OrderStatus.PARTIALLY_FILLED
            ):
                request = next(
                    request
                    for request in self.submit_requests
                    if request.idempotency_key == prior.idempotency_key
                )
                assert request.qty is not None
                remaining = request.qty - prior.filled_qty
                self._apply_position_delta(request.ticker, remaining)
                self._fills.append(
                    BrokerFill(
                        broker_fill_id="paper-late-fill",
                        broker_order_id=prior.broker_order_id,
                        ticker=request.ticker,
                        side=request.side.value,
                        qty=remaining,
                        price=Decimal("99"),
                        filled_at=datetime.now(timezone.utc),
                    )
                )
                filled = OrderResult(
                    idempotency_key=prior.idempotency_key,
                    broker_order_id=prior.broker_order_id,
                    status=OrderStatus.FILLED,
                    filled_qty=request.qty,
                    avg_fill_price=Decimal("99.50"),
                    ticker=request.ticker,
                )
                self._orders_by_id[order_id] = filled
                self._orders_by_key[prior.idempotency_key] = filled
                self.late_fill_applied = True
            raise RuntimeError("injected cancellation failure")
        self.cancel_ids.append(order_id)
        canceled = OrderResult(
            idempotency_key=prior.idempotency_key,
            broker_order_id=prior.broker_order_id,
            status=OrderStatus.CANCELED,
            filled_qty=prior.filled_qty,
            avg_fill_price=prior.avg_fill_price,
            ticker=prior.ticker,
        )
        self._orders_by_id[order_id] = canceled
        self._orders_by_key[prior.idempotency_key] = canceled
        return canceled


def _seed_preexisting_paper_order(primary: Path) -> None:
    engine = create_db_engine(f"sqlite:///{primary}")
    factory = make_session_factory(engine)
    bind_sensitive_cipher(factory, _DRILL_CIPHER)
    with factory() as session:
        open_order = Order(
            idempotency_key="paper-preexisting-client",
            ticker="AAPL",
            side="buy",
            order_type="limit",
            qty=Decimal("1"),
            limit_price=Decimal("96"),
            status=OrderStatus.SUBMITTED.value,
            broker_order_id="paper-preexisting",
            acceptance_state="accepted",
            last_error_code="",
        )
        historical = Order(
            idempotency_key="paper-history-client",
            ticker="AAPL",
            side="buy",
            order_type="market",
            qty=Decimal("5"),
            status=OrderStatus.FILLED.value,
            broker_order_id="paper-history",
            acceptance_state="accepted",
            last_error_code="",
            submission_started_at=_PAPER_PREEXISTING_SUBMITTED_AT,
            created_at=_PAPER_PREEXISTING_SUBMITTED_AT,
            updated_at=_PAPER_PREEXISTING_SUBMITTED_AT,
        )
        persist_sensitive(
            session,
            open_order,
            {"approval_reason": "preexisting open paper order"},
        )
        persist_sensitive(
            session,
            historical,
            {"approval_reason": "preexisting historical paper order"},
        )
        session.add(
            Fill(
                order_id=historical.id,
                ticker="AAPL",
                side=OrderSide.BUY.value,
                qty=Decimal("5"),
                price=Decimal("100"),
                broker_fill_id="paper-preexisting-fill",
                filled_at=_PAPER_PREEXISTING_FILLED_AT,
            )
        )
        session.commit()
    engine.dispose()
    with sqlite3.connect(primary) as connection:
        assert connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()[0] == 0
        assert connection.execute(
            "PRAGMA journal_mode=DELETE"
        ).fetchone() == ("delete",)


def _credentialed_environment(monkeypatch, primary: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")


def _local_drill_environment(monkeypatch, primary: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")


@pytest.mark.parametrize(
    ("sandbox", "base_url"),
    [
        (False, "https://api.alpaca.markets"),
        (False, "https://paper-api.alpaca.markets"),
        (True, "https://paper-proxy.invalid"),
    ],
)
def test_credentialed_mode_refuses_unverified_execution_target_before_copy_or_access(
    tmp_path,
    app_config,
    monkeypatch,
    sandbox,
    base_url,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "must-not-exist.db"
    _upgrade_database(primary)
    _credentialed_environment(monkeypatch, primary)
    before = _primary_manifest(primary)
    broker = AlpacaBroker(
        TradingClient(
            "paper-key-present",
            "paper-secret-present",
            paper=True,
        ),
        object(),
    )
    broker._trading._sandbox = sandbox
    broker._trading._base_url = base_url
    accesses = 0

    def forbid_access():
        nonlocal accesses
        accesses += 1
        pytest.fail("unsafe target reached broker access")

    monkeypatch.setattr(broker, "get_open_orders", forbid_access)

    with pytest.raises(SafetyDrillError) as caught:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=broker,
            credentialed_paper=True,
            clock=FakeClock(is_open=True),
        )

    assert caught.value.code == "unsafe_configuration"
    assert accesses == 0
    assert not destination.exists()
    assert _primary_manifest(primary) == before


def test_credentialed_validation_refuses_uninitialized_alpaca_broker(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")
    broker = object.__new__(AlpacaBroker)

    with pytest.raises(SafetyDrillError) as caught:
        _validate_credentialed_paper(
            broker,
            Secrets(),
        )

    assert caught.value.code == "unsafe_configuration"


def test_credentialed_validation_rejects_post_construction_live_target_mutation(
    monkeypatch,
):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")
    broker = AlpacaBroker(
        TradingClient(
            "paper-key-present",
            "paper-secret-present",
            paper=True,
        ),
        object(),
    )
    secrets = Secrets()
    _validate_credentialed_paper(broker, secrets)

    broker._trading._sandbox = False
    broker._trading._base_url = "https://api.alpaca.markets"

    with pytest.raises(SafetyDrillError) as caught:
        _validate_credentialed_paper(broker, secrets)

    assert caught.value.code == "unsafe_configuration"


def test_credentialed_validation_requires_exact_broker_and_trading_client_types(
    monkeypatch,
):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")

    class AlpacaBrokerSubclass(AlpacaBroker):
        pass

    class TradingClientSubclass(TradingClient):
        pass

    candidates = (
        AlpacaBrokerSubclass(
            TradingClient(
                "paper-key-present",
                "paper-secret-present",
                paper=True,
            ),
            object(),
        ),
        AlpacaBroker(
            TradingClientSubclass(
                "paper-key-present",
                "paper-secret-present",
                paper=True,
            ),
            object(),
        ),
    )

    for broker in candidates:
        with pytest.raises(SafetyDrillError) as caught:
            _validate_credentialed_paper(broker, Secrets())
        assert caught.value.code == "unsafe_configuration"


@pytest.mark.parametrize("operation", ["submit", "cancel"])
def test_credentialed_wrapper_revalidates_target_immediately_before_mutation(
    monkeypatch,
    operation,
):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")
    broker = AlpacaBroker(
        TradingClient(
            "paper-key-present",
            "paper-secret-present",
            paper=True,
        ),
        object(),
    )
    secrets = Secrets()
    _validate_credentialed_paper(broker, secrets)
    writes: list[str] = []

    def submit_spy(order):
        writes.append("submit")
        return OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id="paper-order",
            status=OrderStatus.SUBMITTED,
            ticker=order.ticker,
        )

    def cancel_spy(order_id):
        writes.append("cancel")
        return OrderResult(
            idempotency_key="safety-drill-test-crash",
            broker_order_id=order_id,
            status=OrderStatus.CANCELED,
            ticker="AAPL",
        )

    monkeypatch.setattr(broker, "submit_order", submit_spy)
    monkeypatch.setattr(broker, "cancel_order", cancel_spy)
    wrapped = _CrashAfterAcceptanceOnceBroker(
        broker,
        before_broker_mutation=lambda: _validate_credentialed_paper(
            broker,
            secrets,
        ),
    )
    broker._trading._sandbox = False
    broker._trading._base_url = "https://api.alpaca.markets"

    with pytest.raises(SafetyDrillError) as caught:
        if operation == "submit":
            wrapped.submit_order(
                OrderRequest(
                    ticker="AAPL",
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    qty=Decimal("1"),
                    limit_price=Decimal("96"),
                    time_in_force=OrderTimeInForce.GTC,
                    idempotency_key="safety-drill-test-crash",
                )
            )
        else:
            wrapped.cancel_order("paper-order")

    assert caught.value.code == "unsafe_configuration"
    assert writes == []


def test_credentialed_wrapper_revalidates_each_submission(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")
    broker = AlpacaBroker(
        TradingClient(
            "paper-key-present",
            "paper-secret-present",
            paper=True,
        ),
        object(),
    )
    secrets = Secrets()
    writes: list[str] = []

    def submit_spy(order):
        writes.append(order.idempotency_key)
        return OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=f"paper-{len(writes)}",
            status=OrderStatus.SUBMITTED,
            ticker=order.ticker,
        )

    monkeypatch.setattr(broker, "submit_order", submit_spy)
    wrapped = _CrashAfterAcceptanceOnceBroker(
        broker,
        before_broker_mutation=lambda: _validate_credentialed_paper(
            broker,
            secrets,
        ),
    )
    request = OrderRequest(
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("1"),
        limit_price=Decimal("96"),
        time_in_force=OrderTimeInForce.GTC,
        idempotency_key="safety-drill-test-crash",
    )

    with pytest.raises(_DrillCrash):
        wrapped.submit_order(request)
    broker._trading._sandbox = False
    broker._trading._base_url = "https://api.alpaca.markets"

    with pytest.raises(SafetyDrillError):
        wrapped.submit_order(
            OrderRequest(
                ticker="AAPL",
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                qty=Decimal("1"),
                idempotency_key="safety-drill-test-compensate",
            )
        )

    assert writes == ["safety-drill-test-crash"]


def test_armed_paper_guard_blocks_submit_after_idempotency_lookup_redirects_live(
    monkeypatch,
):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")
    trading = TradingClient(
        "paper-key-present",
        "paper-secret-present",
        paper=True,
    )
    broker = AlpacaBroker(trading, object())
    _validate_credentialed_paper(broker, Secrets())
    live_writes: list[tuple[bool, str]] = []

    def redirect_during_lookup(client_order_id):
        assert client_order_id == "safety-drill-inner-submit"
        trading._sandbox = False
        trading._base_url = "https://api.alpaca.markets"
        return None

    def observe_submit(*, order_data):
        live_writes.append((trading._sandbox, str(trading._base_url)))
        raise RuntimeError("SDK submit must not be reached")

    monkeypatch.setattr(
        broker,
        "get_order_by_client_id",
        redirect_during_lookup,
    )
    monkeypatch.setattr(trading, "submit_order", observe_submit)

    with pytest.raises(RuntimeError):
        broker.submit_order(
            OrderRequest(
                ticker="AAPL",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                qty=Decimal("1"),
                limit_price=Decimal("96"),
                time_in_force=OrderTimeInForce.GTC,
                idempotency_key="safety-drill-inner-submit",
            )
        )

    assert live_writes == []


def test_armed_paper_guard_blocks_cancel_after_target_redirects_live(
    monkeypatch,
):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")
    trading = TradingClient(
        "paper-key-present",
        "paper-secret-present",
        paper=True,
    )
    broker = AlpacaBroker(trading, object())
    _validate_credentialed_paper(broker, Secrets())
    live_writes: list[tuple[bool, str]] = []

    def observe_cancel(order_id):
        live_writes.append((trading._sandbox, str(trading._base_url)))
        raise RuntimeError(f"SDK cancel must not be reached: {order_id}")

    monkeypatch.setattr(trading, "cancel_order_by_id", observe_cancel)
    trading._sandbox = False
    trading._base_url = "https://api.alpaca.markets"

    with pytest.raises(RuntimeError):
        broker.cancel_order("paper-order")

    assert live_writes == []


def test_mock_mode_preserves_preexisting_manifest_and_cleans_tagged_order(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker()

    report = run_safety_drill(
        database_copy=tmp_path / "alpaca-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe
    assert "mode:mock" in report.details
    assert not any(
        detail.startswith("alpaca_paper:") for detail in report.details
    )
    assert len(broker.submit_requests) == 1
    submitted = broker.submit_requests[0]
    assert submitted.order_type is OrderType.LIMIT
    assert submitted.time_in_force is OrderTimeInForce.DAY
    assert submitted.qty == Decimal("0.013021")
    assert submitted.limit_price == Decimal("96.00")
    assert submitted.idempotency_key.startswith("safety-drill-")
    copied_engine = create_db_engine(f"sqlite:///{tmp_path / 'alpaca-copy.db'}")
    with make_session_factory(copied_engine)() as session:
        persisted = session.scalar(
            select(Order).where(
                Order.idempotency_key == submitted.idempotency_key
            )
        )
        assert json.loads(persisted.submission_payload_json) == {}
    copied_engine.dispose()
    assert broker.cancel_ids == ["paper-drill-1"]
    assert {
        order.broker_order_id for order in broker.get_open_orders()
    } == {"paper-preexisting"}
    assert [(position.ticker, position.qty) for position in broker.get_positions()] == [
        ("AAPL", Decimal("5"))
    ]


def test_mock_mode_compensates_only_its_adverse_fill(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(fill_initial=True)

    report = run_safety_drill(
        database_copy=tmp_path / "alpaca-fill-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe
    assert not any(
        detail.startswith("alpaca_paper:") for detail in report.details
    )
    assert len(broker.submit_requests) == 2
    initial, compensation = broker.submit_requests
    assert initial.side is OrderSide.BUY
    assert compensation.side is OrderSide.SELL
    assert compensation.qty == initial.qty
    assert compensation.idempotency_key.startswith(
        f"{initial.idempotency_key.rsplit('-', 1)[0]}-compensate"
    )
    assert broker.cancel_ids == []
    assert [(position.ticker, position.qty) for position in broker.get_positions()] == [
        ("AAPL", Decimal("5"))
    ]
    initial_result = broker.get_order_by_client_id(initial.idempotency_key)
    compensation_result = broker.get_order_by_client_id(
        compensation.idempotency_key
    )
    initial_fills = [
        fill
        for fill in broker.get_fill_activities()
        if fill.broker_order_id == initial_result.broker_order_id
    ]
    compensation_fills = [
        fill
        for fill in broker.get_fill_activities()
        if fill.broker_order_id == compensation_result.broker_order_id
    ]
    assert sum((fill.qty for fill in initial_fills), Decimal("0")) == initial.qty
    assert sum(
        (fill.qty for fill in compensation_fills),
        Decimal("0"),
    ) == initial.qty
    assert {fill.side for fill in initial_fills} == {OrderSide.BUY.value}
    assert {fill.side for fill in compensation_fills} == {
        OrderSide.SELL.value
    }
    assert compensation_result.broker_order_id in broker.status_ids


def test_compensation_last_mile_guard_blocks_drift_after_execution_risk(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    copied = tmp_path / "last-mile-compensation-copy.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(fill_initial=True)
    original_risk_check = OrderSubmissionService._risk_check
    drifted = False

    def drift_after_execution_risk(self, request, order_id):
        nonlocal drifted
        result = original_risk_check(self, request, order_id)
        if (
            request.idempotency_key.endswith("-compensate")
            and not drifted
        ):
            broker._apply_position_delta("AAPL", Decimal("1"))
            drifted = True
        return result

    monkeypatch.setattr(
        OrderSubmissionService,
        "_risk_check",
        drift_after_execution_risk,
    )

    report = run_safety_drill(
        database_copy=copied,
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert drifted
    assert report.safe is False
    assert len(broker.submit_requests) == 1
    engine = create_db_engine(f"sqlite:///{copied}")
    with make_session_factory(engine)() as session:
        compensation = session.scalar(
            select(Order).where(
                Order.idempotency_key.like("%-compensate")
            )
        )
        assert compensation is not None
        assert compensation.status == OrderStatus.REJECTED.value
        assert (
            compensation.last_error_code
            == "safety_drill_compensation_invariant_changed"
        )
    engine.dispose()


def test_post_proposal_compensation_failure_uses_rejection_service_and_audit(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    copied = tmp_path / "post-proposal-rejection-copy.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(fill_initial=True)
    original_propose = TradingService.propose_order
    drifted = False

    def drift_after_compensation_proposal(self, *args, **kwargs):
        nonlocal drifted
        result = original_propose(self, *args, **kwargs)
        if (
            kwargs.get("idempotency_key", "").endswith("-compensate")
            and not drifted
        ):
            broker._apply_position_delta("AAPL", Decimal("1"))
            drifted = True
        return result

    monkeypatch.setattr(
        TradingService,
        "propose_order",
        drift_after_compensation_proposal,
    )

    report = run_safety_drill(
        database_copy=copied,
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert drifted
    assert report.safe is False
    assert len(broker.submit_requests) == 1
    engine = create_db_engine(f"sqlite:///{copied}")
    with make_session_factory(engine)() as session:
        compensation = session.scalar(
            select(Order).where(
                Order.idempotency_key.like("%-compensate")
            )
        )
        assert compensation is not None
        assert compensation.status == OrderStatus.REJECTED.value
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "order.reject",
                AuditEvent.target_id == str(compensation.id),
            )
        )
        assert audit is not None
        assert audit.actor == "operator:safety-drill"
    engine.dispose()


def test_compensation_is_boundedly_reconciled_to_terminal_broker_truth(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(
        fill_initial=True,
        delay_compensation_fill=True,
    )

    report = run_safety_drill(
        database_copy=tmp_path / "delayed-compensation-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe
    compensation = broker.submit_requests[1]
    compensation_result = broker.get_order_by_client_id(
        compensation.idempotency_key
    )
    assert compensation_result.status is OrderStatus.FILLED
    assert compensation_result.broker_order_id in broker.status_ids
    assert [(position.ticker, position.qty) for position in broker.get_positions()] == [
        ("AAPL", Decimal("5"))
    ]


def test_nonterminal_compensation_is_canceled_but_never_claimed_safe(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(
        fill_initial=True,
        delay_compensation_fill=True,
        hold_compensation_open=True,
    )

    report = run_safety_drill(
        database_copy=tmp_path / "nonterminal-compensation-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    compensation = broker.submit_requests[1]
    compensation_result = broker.get_order_by_client_id(
        compensation.idempotency_key
    )
    assert report.safe is False
    assert report.crash_recovered_without_duplicate is False
    assert compensation_result.status is OrderStatus.CANCELED
    assert broker.cancel_ids == [compensation_result.broker_order_id]
    assert {
        order.broker_order_id for order in broker.get_open_orders()
    } == {"paper-preexisting"}


@pytest.mark.parametrize(
    "broker_options",
    [
        {"fail_cancel": True},
        {"unconfirmed_cancel": True},
        {"fail_cancel": True, "fill_after_cancel_failure": True},
    ],
)
def test_unconfirmed_partially_filled_original_is_never_compensated(
    tmp_path,
    app_config,
    monkeypatch,
    broker_options,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    broker = PaperStateBroker(
        partial_initial=True,
        **broker_options,
    )

    report = run_safety_drill(
        database_copy=tmp_path / "unconfirmed-partial-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe is False
    assert report.crash_recovered_without_duplicate is False
    assert len(broker.submit_requests) == 1
    assert not any(
        request.idempotency_key.endswith("-compensate")
        for request in broker.submit_requests
    )
    if broker_options.get("fill_after_cancel_failure"):
        assert broker.late_fill_applied
        assert broker.get_order_by_client_id(
            broker.submit_requests[0].idempotency_key
        ).status is OrderStatus.FILLED


@pytest.mark.parametrize(
    "concurrent_delta",
    [Decimal("1"), Decimal("-1")],
)
def test_mock_mode_refuses_unrelated_or_masked_position_drift_before_compensation(
    tmp_path,
    app_config,
    monkeypatch,
    concurrent_delta,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(
        fill_initial=True,
        concurrent_position_delta=concurrent_delta,
    )

    report = run_safety_drill(
        database_copy=tmp_path / "concurrent-drift-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe is False
    assert report.crash_recovered_without_duplicate is False
    assert len(broker.submit_requests) == 1
    assert not any(
        request.idempotency_key.endswith("-compensate")
        for request in broker.submit_requests
    )
    assert broker.cancel_ids == []


@pytest.mark.parametrize("intervening_delta", [Decimal("1"), Decimal("-1")])
def test_compensation_rechecks_position_after_terminal_and_fill_reads(
    intervening_delta,
):
    tag = "safety-drill-terminal-race"
    initial = OrderResult(
        idempotency_key=f"{tag}-crash",
        broker_order_id="initial-order",
        status=OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("100"),
        ticker="AAPL",
    )

    class RacingBroker:
        def __init__(self):
            self.qty = Decimal("6")
            self.changed = False

        def get_positions(self):
            return [
                Position(
                    "AAPL",
                    self.qty,
                    Decimal("100"),
                    Decimal("100"),
                    Decimal("0"),
                )
            ]

        def get_order_by_client_id(self, client_order_id):
            assert client_order_id == initial.idempotency_key
            return initial

        def get_order_status(self, order_id):
            assert order_id == initial.broker_order_id
            if not self.changed:
                self.qty += intervening_delta
                self.changed = True
            return initial

        def get_fill_activities(self):
            return [
                BrokerFill(
                    broker_fill_id="initial-fill",
                    broker_order_id=initial.broker_order_id,
                    ticker="AAPL",
                    side=OrderSide.BUY.value,
                    qty=Decimal("1"),
                    price=Decimal("100"),
                    filled_at=datetime.now(timezone.utc),
                )
            ]

    broker = RacingBroker()
    proposed: list[str] = []

    class Service:
        def sync_open_orders(self, **kwargs):
            return {"failed": 0}

        def propose_order(self, *args, **kwargs):
            proposed.append(kwargs["idempotency_key"])
            return {"status": OrderStatus.REJECTED.value}

    container = type(
        "Container",
        (),
        {"broker": broker, "service": Service()},
    )()

    assert not _compensate_drill_fill(
        container,
        before_positions={"AAPL": Decimal("5")},
        tag=tag,
        symbol="AAPL",
    )
    assert broker.changed
    assert proposed == []


def test_compensation_rechecks_position_after_proposal_before_approval():
    tag = "safety-drill-proposal-race"
    initial = OrderResult(
        idempotency_key=f"{tag}-crash",
        broker_order_id="initial-order",
        status=OrderStatus.FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("100"),
        ticker="AAPL",
    )

    class RacingBroker:
        qty = Decimal("6")

        def get_positions(self):
            return [
                Position(
                    "AAPL",
                    self.qty,
                    Decimal("100"),
                    Decimal("100"),
                    Decimal("0"),
                )
            ]

        def get_order_by_client_id(self, client_order_id):
            assert client_order_id == initial.idempotency_key
            return initial

        def get_order_status(self, order_id):
            assert order_id == initial.broker_order_id
            return initial

        def get_fill_activities(self):
            return [
                BrokerFill(
                    broker_fill_id="initial-fill",
                    broker_order_id=initial.broker_order_id,
                    ticker="AAPL",
                    side=OrderSide.BUY.value,
                    qty=Decimal("1"),
                    price=Decimal("100"),
                    filled_at=datetime.now(timezone.utc),
                )
            ]

    broker = RacingBroker()
    approvals: list[int] = []

    class Service:
        def sync_open_orders(self, **kwargs):
            return {"failed": 0}

        def propose_order(self, *args, **kwargs):
            broker.qty += Decimal("1")
            return {
                "status": OrderStatus.PROPOSED.value,
                "order_id": 91,
            }

        def approve_order(self, order_id, **kwargs):
            approvals.append(order_id)
            return {"status": OrderStatus.REJECTED.value}

    container = type(
        "Container",
        (),
        {"broker": broker, "service": Service()},
    )()

    assert not _compensate_drill_fill(
        container,
        before_positions={"AAPL": Decimal("5")},
        tag=tag,
        symbol="AAPL",
    )
    assert approvals == []


def test_compensation_rechecks_initial_terminal_status_after_proposal():
    tag = "safety-drill-status-race"
    state = {
        "order": OrderResult(
            idempotency_key=f"{tag}-crash",
            broker_order_id="initial-order",
            status=OrderStatus.FILLED,
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("100"),
            ticker="AAPL",
        )
    }

    class RacingBroker:
        def get_positions(self):
            return [
                Position(
                    "AAPL",
                    Decimal("6"),
                    Decimal("100"),
                    Decimal("100"),
                    Decimal("0"),
                )
            ]

        def get_order_by_client_id(self, client_order_id):
            assert client_order_id == f"{tag}-crash"
            return state["order"]

        def get_order_status(self, order_id):
            assert order_id == "initial-order"
            return state["order"]

        def get_fill_activities(self):
            return [
                BrokerFill(
                    broker_fill_id="initial-fill",
                    broker_order_id="initial-order",
                    ticker="AAPL",
                    side=OrderSide.BUY.value,
                    qty=Decimal("1"),
                    price=Decimal("100"),
                    filled_at=datetime.now(timezone.utc),
                )
            ]

    approvals: list[int] = []

    class Service:
        def sync_open_orders(self, **kwargs):
            return {"failed": 0}

        def propose_order(self, *args, **kwargs):
            state["order"] = OrderResult(
                idempotency_key=f"{tag}-crash",
                broker_order_id="initial-order",
                status=OrderStatus.CANCELED,
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("100"),
                ticker="AAPL",
            )
            return {
                "status": OrderStatus.PROPOSED.value,
                "order_id": 92,
            }

        def approve_order(self, order_id, **kwargs):
            approvals.append(order_id)
            return {"status": OrderStatus.REJECTED.value}

    container = type(
        "Container",
        (),
        {"broker": RacingBroker(), "service": Service()},
    )()

    assert not _compensate_drill_fill(
        container,
        before_positions={"AAPL": Decimal("5")},
        tag=tag,
        symbol="AAPL",
    )
    assert approvals == []


def test_mock_mode_requires_exact_initial_fill_activity_before_compensation(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(
        fill_initial=True,
        omit_initial_fill_record=True,
    )

    report = run_safety_drill(
        database_copy=tmp_path / "missing-fill-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe is False
    assert report.crash_recovered_without_duplicate is False
    assert len(broker.submit_requests) == 1


def test_compensation_reconciliation_failure_restores_position_but_never_passes(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(
        fill_initial=True,
        fail_fill_read_after_submissions=2,
    )

    report = run_safety_drill(
        database_copy=tmp_path / "compensation-reconcile-failure.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe is False
    assert report.crash_recovered_without_duplicate is False
    assert len(broker.submit_requests) == 2
    assert all(
        broker.get_order_by_client_id(request.idempotency_key).status
        is OrderStatus.FILLED
        for request in broker.submit_requests
    )
    assert [(position.ticker, position.qty) for position in broker.get_positions()] == [
        ("AAPL", Decimal("5"))
    ]
    assert broker.cancel_ids == []


def test_cleanup_cancels_validated_tagged_remote_while_local_acceptance_is_stale(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(identity_failures=2)

    report = run_safety_drill(
        database_copy=tmp_path / "stale-local-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe is False
    assert broker.cancel_ids == ["paper-drill-1"]
    assert {
        order.broker_order_id for order in broker.get_open_orders()
    } == {"paper-preexisting"}


@pytest.mark.parametrize("remote_ticker", ["MSFT", None])
def test_tagged_cleanup_validates_against_known_drill_symbol(remote_ticker):
    tag = "safety-drill-known-symbol"
    remote = OrderResult(
        idempotency_key=f"{tag}-crash",
        broker_order_id="wrong-symbol-order",
        status=OrderStatus.SUBMITTED,
        ticker=remote_ticker,
    )

    class Broker:
        def get_open_orders(self):
            return [remote]

        def get_order_by_client_id(self, client_order_id):
            assert client_order_id == remote.idempotency_key
            return remote

        def cancel_order(self, order_id):
            pytest.fail(f"mismatched symbol reached cancellation: {order_id}")

    container = type("Container", (), {"broker": Broker()})()

    assert not _cancel_validated_tagged_open(
        container,
        tag=tag,
        symbol="AAPL",
    )


@pytest.mark.parametrize("failure_stage", ["identity", "cancel"])
def test_tagged_cleanup_isolates_provider_failure_per_order(failure_stage):
    tag = "safety-drill-isolated-cleanup"
    orders = [
        OrderResult(
            idempotency_key=f"{tag}-first",
            broker_order_id="drill-first",
            status=OrderStatus.SUBMITTED,
            ticker="AAPL",
        ),
        OrderResult(
            idempotency_key=f"{tag}-second",
            broker_order_id="drill-second",
            status=OrderStatus.SUBMITTED,
            ticker="AAPL",
        ),
    ]

    class Broker:
        canceled: list[str] = []

        def get_open_orders(self):
            return orders

        def get_order_by_client_id(self, client_order_id):
            if (
                failure_stage == "identity"
                and client_order_id == orders[0].idempotency_key
            ):
                raise RuntimeError("injected identity read failure")
            return next(
                order
                for order in orders
                if order.idempotency_key == client_order_id
            )

        def cancel_order(self, order_id):
            if failure_stage == "cancel" and order_id == "drill-first":
                raise RuntimeError("injected cancel failure")
            self.canceled.append(order_id)
            prior = next(
                order
                for order in orders
                if order.broker_order_id == order_id
            )
            return OrderResult(
                idempotency_key=prior.idempotency_key,
                broker_order_id=prior.broker_order_id,
                status=OrderStatus.CANCELED,
                ticker=prior.ticker,
            )

    broker = Broker()
    container = type("Container", (), {"broker": broker})()

    assert not _cancel_validated_tagged_open(
        container,
        tag=tag,
        symbol="AAPL",
    )
    assert "drill-second" in broker.canceled


@pytest.mark.parametrize("cancel_outcome", ["exception", "nonterminal"])
def test_best_effort_cleanup_never_retries_an_uncertain_cancel(cancel_outcome):
    tag = "safety-drill-one-shot-cancel"
    remote = OrderResult(
        idempotency_key=f"{tag}-crash",
        broker_order_id="drill-open",
        status=OrderStatus.SUBMITTED,
        ticker="AAPL",
    )

    class Broker:
        cancel_attempts = 0

        def get_open_orders(self):
            return [remote]

        def get_order_by_client_id(self, client_order_id):
            assert client_order_id == remote.idempotency_key
            return remote

        def cancel_order(self, order_id):
            assert order_id == remote.broker_order_id
            self.cancel_attempts += 1
            if cancel_outcome == "exception":
                raise RuntimeError("injected unknown cancel acceptance")
            return remote

    class Reconciliation:
        def reconcile_unknown(self, **kwargs):
            return (0, ())

    class Service:
        def sync_open_orders(self, **kwargs):
            return {"failed": 0}

    broker = Broker()
    container = type(
        "Container",
        (),
        {
            "broker": broker,
            "reconciliation": Reconciliation(),
            "service": Service(),
        },
    )()

    assert not safety_drill_module._best_effort_cleanup(
        container,
        before_positions={},
        tag=tag,
        symbol="AAPL",
    )
    assert broker.cancel_attempts == 1


def test_final_local_scan_treats_unapproved_tagged_orders_as_unsafe(
    tmp_path,
):
    database = tmp_path / "unsafe-local-orders.db"
    _upgrade_database(database)
    engine = create_db_engine(f"sqlite:///{database}")
    session_factory = make_session_factory(engine)
    tag = "safety-drill-final-scan"
    with session_factory() as session:
        proposed = Order(
            idempotency_key=f"{tag}-proposed",
            ticker="AAPL",
            side=OrderSide.BUY.value,
            order_type=OrderType.LIMIT.value,
            qty=Decimal("1"),
            limit_price=Decimal("90"),
            status=OrderStatus.PROPOSED.value,
        )
        approval_recorded = Order(
            idempotency_key=f"{tag}-approval-recorded",
            ticker="AAPL",
            side=OrderSide.BUY.value,
            order_type=OrderType.LIMIT.value,
            qty=Decimal("1"),
            limit_price=Decimal("90"),
            status=OrderStatus.APPROVAL_RECORDED.value,
        )
        terminal = Order(
            idempotency_key=f"{tag}-rejected",
            ticker="AAPL",
            side=OrderSide.BUY.value,
            order_type=OrderType.LIMIT.value,
            qty=Decimal("1"),
            limit_price=Decimal("90"),
            status=OrderStatus.REJECTED.value,
        )
        session.add_all((proposed, approval_recorded, terminal))
        session.commit()
        expected_ids = {proposed.id, approval_recorded.id}
        finder = getattr(
            safety_drill_module,
            "_unsafe_tagged_local_order_ids",
            lambda *_args, **_kwargs: (),
        )

        assert set(finder(session, tag)) == expected_ids
    engine.dispose()


def test_outer_cleanup_runs_for_base_exception_after_broker_mutation(
    tmp_path,
    app_config,
    monkeypatch,
):
    class InjectedAbort(BaseException):
        pass

    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(identity_base_failure=InjectedAbort())

    with pytest.raises(InjectedAbort):
        run_safety_drill(
            database_copy=tmp_path / "base-exception-copy.db",
            config=_safe_config(app_config),
            broker=broker,
            credentialed_paper=False,
            clock=FakeClock(is_open=True),
        )

    assert broker.cancel_ids == ["paper-drill-1"]
    assert {
        order.broker_order_id for order in broker.get_open_orders()
    } == {"paper-preexisting"}


def test_mock_mode_refuses_quote_without_nonmarketable_sane_limit(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker(quote_ask=Decimal("94"))

    report = run_safety_drill(
        database_copy=tmp_path / "divergent-book-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe is False
    assert report.crash_recovered_without_duplicate is False
    assert broker.submit_requests == []


def test_crash_gate_disposes_and_reconstructs_before_identity_reconciliation(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    destination = tmp_path / "restart-copy.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _local_drill_environment(monkeypatch, primary)
    broker = PaperStateBroker()
    real_build = safety_drill_module.build_test_container
    containers = []
    first_disposed = False
    observations = []

    def observed_build(*args, **kwargs):
        nonlocal first_disposed
        built = real_build(*args, **kwargs)
        containers.append(built)
        if len(containers) == 1:
            real_dispose = built.engine.dispose

            def tracked_dispose(*dispose_args, **dispose_kwargs):
                nonlocal first_disposed
                first_disposed = True
                return real_dispose(*dispose_args, **dispose_kwargs)

            built.engine.dispose = tracked_dispose
        return built

    def observe_identity(client_order_id):
        if not client_order_id.endswith("-crash"):
            return
        with sqlite3.connect(destination) as connection:
            status = connection.execute(
                "SELECT status FROM orders WHERE idempotency_key = ?",
                (client_order_id,),
            ).fetchone()[0]
        observations.append((len(containers), first_disposed, status))

    monkeypatch.setattr(
        safety_drill_module,
        "build_test_container",
        observed_build,
    )
    broker.on_identity_lookup = observe_identity

    report = run_safety_drill(
        database_copy=destination,
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=False,
        clock=FakeClock(is_open=True),
    )

    assert report.safe
    assert observations
    assert observations[0] == (2, True, OrderStatus.SUBMITTING.value)
    assert len(broker.submit_requests) == 1


def test_oco_gate_competes_two_independent_repositories_in_bounded_threads(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    original_claim = RuleRepository.claim_terminal
    calls = []
    calls_lock = threading.Lock()

    def observed_claim(self, lease, winning_rule_id, **kwargs):
        with calls_lock:
            calls.append(
                (id(self), threading.get_ident(), winning_rule_id)
            )
        return original_claim(
            self,
            lease,
            winning_rule_id,
            **kwargs,
        )

    monkeypatch.setattr(
        RuleRepository,
        "claim_terminal",
        observed_claim,
    )

    report = run_safety_drill(
        database_copy=tmp_path / "oco-copy.db",
        config=_safe_config(app_config),
        broker=MockBroker(prices={"AAPL": Decimal("100")}),
    )

    assert report.oco_single_terminal
    assert len(calls) == 2
    assert len({repository_id for repository_id, _, _ in calls}) == 2
    assert len({thread_id for _, thread_id, _ in calls}) == 2
    assert len({rule_id for _, _, rule_id in calls}) == 2


def test_oco_workers_finish_before_later_gates_and_are_non_daemon(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    real_thread = threading.Thread
    real_claim = RuleRepository.claim_terminal
    workers = []
    daemon_values = []
    active_claims = 0
    claims_lock = threading.Lock()
    later_gate_while_active = False

    class ShortFirstJoinThread(real_thread):
        def __init__(self, *args, **kwargs):
            daemon_values.append(kwargs.get("daemon", False))
            super().__init__(*args, **kwargs)
            self.join_calls = 0
            workers.append(self)

        def join(self, timeout=None):
            self.join_calls += 1
            return super().join(
                0.01 if self.join_calls == 1 else timeout
            )

    def delayed_claim(self, lease, winning_rule_id, **kwargs):
        nonlocal active_claims
        with claims_lock:
            active_claims += 1
        try:
            threading.Event().wait(0.2)
            return real_claim(
                self,
                lease,
                winning_rule_id,
                **kwargs,
            )
        finally:
            with claims_lock:
                active_claims -= 1

    class ObservedBroker(MockBroker):
        def get_positions(self):
            nonlocal later_gate_while_active
            with claims_lock:
                if active_claims:
                    later_gate_while_active = True
            return super().get_positions()

    monkeypatch.setattr(
        safety_drill_module.threading,
        "Thread",
        ShortFirstJoinThread,
    )
    monkeypatch.setattr(
        RuleRepository,
        "claim_terminal",
        delayed_claim,
    )

    run_safety_drill(
        database_copy=tmp_path / "bounded-oco-copy.db",
        config=_safe_config(app_config),
        broker=ObservedBroker(prices={"AAPL": Decimal("100")}),
    )
    for worker in workers:
        worker.join(timeout=1)

    assert daemon_values and not any(daemon_values)
    assert not later_gate_while_active
    assert all(not worker.is_alive() for worker in workers)


def test_credentialed_mode_refuses_missing_keys_or_nonpaper_endpoint_before_copy(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary}")
    monkeypatch.setenv("APP_API_TOKEN", "task-10-test-operator-secret")
    destination = tmp_path / "must-not-exist.db"
    broker = AlpacaBroker(
        TradingClient(
            "paper-key-present",
            "paper-secret-present",
            paper=True,
        ),
        object(),
    )
    accesses = 0

    def forbid_access():
        nonlocal accesses
        accesses += 1
        pytest.fail("failed validation reached broker access")

    monkeypatch.setattr(broker, "get_open_orders", forbid_access)

    with pytest.raises(SafetyDrillError) as missing:
        run_safety_drill(
            database_copy=destination,
            config=_safe_config(app_config),
            broker=broker,
            credentialed_paper=True,
            clock=FakeClock(is_open=True),
        )
    assert missing.value.code == "credentials_unavailable"
    assert not destination.exists()
    assert accesses == 0

    monkeypatch.setenv("ALPACA_API_KEY", "paper-key-present")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret-present")
    unsafe_config = _safe_config(app_config).model_copy(
        update={
            "provider_origins": app_config.provider_origins.model_copy(
                update={"alpaca_trading": "https://api.alpaca.markets"}
            )
        }
    )
    with pytest.raises(SafetyDrillError) as endpoint:
        run_safety_drill(
            database_copy=destination,
            config=unsafe_config,
            broker=broker,
            credentialed_paper=True,
            clock=FakeClock(is_open=True),
        )
    assert endpoint.value.code == "unsafe_configuration"
    assert not destination.exists()
    assert accesses == 0


def test_credentialed_label_never_passes_when_crash_gate_is_unconfirmed(
    tmp_path,
    app_config,
    monkeypatch,
):
    primary = tmp_path / "primary.db"
    _upgrade_database(primary)
    _seed_preexisting_paper_order(primary)
    _credentialed_environment(monkeypatch, primary)
    broker = PaperStateBroker(identity_failures=2)
    monkeypatch.setattr(
        safety_drill_module,
        "_validate_credentialed_paper",
        lambda broker, secrets, config: None,
    )

    report = run_safety_drill(
        database_copy=tmp_path / "label-copy.db",
        config=_safe_config(app_config),
        broker=broker,
        credentialed_paper=True,
        clock=FakeClock(is_open=True),
    )

    assert report.crash_recovered_without_duplicate is False
    assert report.reconciliation_clean
    assert report.safe is False
    assert "alpaca_paper:passed" not in report.details
    assert "alpaca_paper:unconfirmed" in report.details
