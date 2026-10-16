"""Encrypted-backup restore: isolated, validated, and activated only on request.

Every database here is a disposable fixture in a temporary directory.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3

import pytest

from trading_assistant import installation
from trading_assistant.db.migrate import upgrade
from trading_assistant.db.session import create_db_engine
from trading_assistant.ops.backup import create_encrypted_database_backup, list_committed_backups
from trading_assistant.ops.restore import RestoreError, activate_restored, restore_backup

KEY = b"7" * 32
KEY_ID = "restore-test-key"
NOW = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migrated(path: Path, marker: str) -> Path:
    engine = create_db_engine(f"sqlite:///{path}")
    upgrade(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE restore_marker (value TEXT)")
        connection.exec_driver_sql("INSERT INTO restore_marker VALUES (?)", (marker,))
    engine.dispose()
    plain = sqlite3.connect(path)
    try:
        plain.execute("PRAGMA journal_mode=DELETE")  # checkpoints and drops the WAL sidecars
    finally:
        plain.close()
    path.chmod(0o600)
    return path


def _backup(source: Path, directory: Path) -> Path:
    receipt = create_encrypted_database_backup(
        source, directory, backup_key=KEY, backup_key_id=KEY_ID,
        schema_head="20260730_0018", now=lambda: NOW,
    )
    return receipt.path


def _marker(path: Path) -> str:
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        return connection.execute("SELECT value FROM restore_marker").fetchone()[0]


def test_restore_decrypts_into_a_new_private_directory_and_validates(tmp_path):
    source = _migrated(tmp_path / "source.db", "backed-up")
    before = _sha(source)
    artifact = _backup(source, tmp_path / "backups")

    receipt = restore_backup(artifact, tmp_path / "restore-1", backup_key=KEY, backup_key_id=KEY_ID)

    restored = Path(receipt.restored_path)
    assert receipt.restored_sha256 == _sha(restored)
    assert (receipt.integrity, receipt.foreign_key_violations) == ("ok", 0)
    assert receipt.schema_compatible == "current" and receipt.schema_current == receipt.schema_head
    assert restored.stat().st_mode & 0o777 == 0o600
    assert restored.parent.stat().st_mode & 0o777 == 0o700
    assert _marker(restored) == "backed-up"
    assert _sha(source) == before  # the source database is untouched


def test_restore_never_reuses_a_directory(tmp_path):
    artifact = _backup(_migrated(tmp_path / "source.db", "x"), tmp_path / "backups")
    (tmp_path / "taken").mkdir()

    with pytest.raises(RestoreError) as refused:
        restore_backup(artifact, tmp_path / "taken", backup_key=KEY, backup_key_id=KEY_ID)
    assert refused.value.code == "restore_destination_exists"


def test_a_wrong_key_or_a_modified_artifact_leaves_nothing_behind(tmp_path):
    artifact = _backup(_migrated(tmp_path / "source.db", "x"), tmp_path / "backups")

    with pytest.raises(RestoreError) as wrong_id:
        restore_backup(artifact, tmp_path / "r1", backup_key=KEY, backup_key_id="another-key-id")
    with pytest.raises(RestoreError) as wrong_key:
        restore_backup(artifact, tmp_path / "r2", backup_key=b"8" * 32, backup_key_id=KEY_ID)

    assert wrong_id.value.code == "backup_key_mismatch"
    assert wrong_key.value.code == "backup_authentication_failed"
    assert not (tmp_path / "r1").exists() and not (tmp_path / "r2").exists()

    data = bytearray(artifact.read_bytes())
    data[-40] ^= 0x01
    os.chmod(artifact, 0o600)
    artifact.write_bytes(bytes(data))
    with pytest.raises(RestoreError) as tampered:
        restore_backup(artifact, tmp_path / "r3", backup_key=KEY, backup_key_id=KEY_ID)
    assert tampered.value.code in {"backup_authentication_failed", "encrypted_backup_not_committed"}
    assert not (tmp_path / "r3").exists()


def _checkout(root: Path) -> Path:
    for relative in ("pyproject.toml", "scripts/operator.sh", "src/trading_assistant/__init__.py"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text("", encoding="utf-8")
    return root.resolve()


def test_activation_preserves_the_current_database_and_rechooses_the_restore(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = _checkout(tmp_path / "checkout")
    installation.designate(root, home=home)
    current = _migrated(root / "trading_assistant.db", "current")
    installation.choose_database(current, sha256=_sha(current), root=root, home=home)
    backup_source = _migrated(tmp_path / "older.db", "restored")
    artifact = _backup(backup_source, tmp_path / "backups")
    receipt = restore_backup(artifact, root / ".local-restore", backup_key=KEY, backup_key_id=KEY_ID)
    restored = Path(receipt.restored_path)
    current_hash = _sha(current)

    with pytest.raises(RestoreError) as unacknowledged:
        activate_restored(restored, restored_sha256=receipt.restored_sha256, current_sha256="0" * 64,
                          root=root, home=home)
    assert unacknowledged.value.code == "current_database_hash_mismatch"
    (root / "trading_assistant.db-wal").write_bytes(b"")
    with pytest.raises(RestoreError) as in_use:
        activate_restored(restored, restored_sha256=receipt.restored_sha256, current_sha256=current_hash,
                          root=root, home=home)
    assert in_use.value.code == "database_in_use"
    (root / "trading_assistant.db-wal").unlink()

    result = activate_restored(restored, restored_sha256=receipt.restored_sha256, current_sha256=current_hash,
                               root=root, home=home, now=NOW)

    preserved = Path(result["preserved_previous"])
    assert preserved.name == "trading_assistant.db.before-restore-20261016T180000Z"
    assert _sha(preserved) == current_hash and _marker(preserved) == "current"
    assert _marker(root / "trading_assistant.db") == "restored"
    assert installation.require_chosen_database(
        f"sqlite:///{root / 'trading_assistant.db'}", home=home
    ) == root / "trading_assistant.db"
    assert list_committed_backups(tmp_path / "backups")


# ── backup and migration operate on the chosen database unless told otherwise ──
def _migrate_with(monkeypatch, app_config, database_url, observed):
    from trading_assistant.db import migrate
    from trading_assistant.security.secrets import RuntimeSecrets

    monkeypatch.setattr(migrate, "load_config", lambda: app_config, raising=False)
    monkeypatch.setattr(
        migrate, "load_role_secrets",
        lambda role, *, config, **_kw: RuntimeSecrets(database_url=database_url), raising=False,
    )
    monkeypatch.setattr(migrate, "create_db_engine", lambda url: observed.append(url) or object())
    monkeypatch.setattr(migrate, "_print_result", lambda *_args: None)
    monkeypatch.setattr(migrate, "require_current_schema", lambda _engine: None)
    monkeypatch.setattr("trading_assistant.logging.configure_runtime_logging", lambda *_a: None)
    return migrate


def test_migration_refuses_an_unchosen_database_but_accepts_an_explicit_file(
    app_config, monkeypatch, tmp_path, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    database = tmp_path / "trading_assistant.db"
    database.write_bytes(b"")
    observed: list[str] = []
    migrate = _migrate_with(monkeypatch, app_config, f"sqlite:///{database}", observed)

    assert migrate.main(["status"]) == 1
    assert "migration refused: database_not_chosen" in capsys.readouterr().err
    assert observed == []

    assert migrate.main(["--database", str(database), "status"]) == 0
    assert observed == [f"sqlite:///{database.resolve()}"]


def test_backup_refuses_an_unchosen_database(app_config, monkeypatch, tmp_path, capsys):
    from trading_assistant.ops import backup
    from trading_assistant.security.secrets import RuntimeSecrets
    import base64

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    secrets = RuntimeSecrets(
        database_url=f"sqlite:///{tmp_path / 'trading_assistant.db'}",
        backup_encryption_key=base64.b64encode(KEY).decode("ascii"),
    )

    result = backup.main(
        ["--destination", str(tmp_path / "out")],
        config_loader=lambda: app_config,
        secrets_loader=lambda *_a, **_k: secrets,
    )

    assert result == 1
    assert "backup refused: database_not_chosen" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
