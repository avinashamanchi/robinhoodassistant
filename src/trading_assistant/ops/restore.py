"""Restore an encrypted database backup: isolated first, activated only on request.

Two separate steps:

``verify`` decrypts one committed backup into a **new** private directory
(0700) as ``restored.sqlite3`` (0600). It authenticates the AES-256-GCM
ciphertext, checks the plaintext SHA-256 against the artifact header, runs
SQLite ``integrity_check`` and ``foreign_key_check``, and reports schema
compatibility with this code. It never reads or writes the live database, and
a failed decryption leaves nothing behind.

``activate`` puts a verified restore in place, and only when you name both
files' exact SHA-256 hashes. It requires the designated checkout and no SQLite
sidecars beside the runtime database (no active writer), renames the current
``trading_assistant.db`` to ``trading_assistant.db.before-restore-<UTC time>``
(kept, never deleted), moves the restored file into place, and records it with
``choose-database --replace`` so startup accepts the new file.

    python -m trading_assistant.ops.restore verify --artifact PATH --into NEW_DIR
    python -m trading_assistant.ops.restore activate --restored PATH \\
        --restored-sha256 HASH --current-sha256 HASH

Rollback after activate: stop the runtime, rename the preserved file back to
``trading_assistant.db``, and choose it again with ``--replace``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .backup import (
    _CHUNK_BYTES,
    _TAG_BYTES,
    EncryptedBackupError,
    _open_committed_artifact,
    _parse_header_stream,
)

RESTORED_NAME = "restored.sqlite3"


class RestoreError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class RestoreReceipt:
    restored_path: str
    restored_sha256: str
    backup_created_at: str
    backup_key_id: str
    backup_schema_head: str
    integrity: str
    foreign_key_violations: int
    schema_current: Optional[str]
    schema_head: str
    schema_compatible: str  # "current" | "needs_migration" | "incompatible"
    tables: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_schema_head() -> str:
    from alembic.script import ScriptDirectory

    from ..db.schema import _config

    return str(ScriptDirectory.from_config(_config()).get_current_head())


def _schema_report(path: Path) -> tuple[Optional[str], str, str]:
    head = _code_schema_head()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        try:
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        except sqlite3.DatabaseError:
            return None, head, "incompatible"
    finally:
        connection.close()
    current = rows[0][0] if len(rows) == 1 else None
    if current == head:
        return current, head, "current"
    if current is not None and current < head:
        return current, head, "needs_migration"
    return current, head, "incompatible"


def restore_backup(
    artifact: Path,
    destination: Path,
    *,
    backup_key: bytes,
    backup_key_id: str,
) -> RestoreReceipt:
    """Decrypt and validate ``artifact`` into the new directory ``destination``."""
    if not isinstance(backup_key, bytes) or len(backup_key) != 32:
        raise RestoreError("backup_key_invalid")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise RestoreError("restore_destination_exists", "restore into a new directory")
    parent = destination.parent.resolve(strict=True)
    os.mkdir(destination, 0o700)
    os.chmod(destination, 0o700)
    output = destination / RESTORED_NAME
    written = False
    try:
        with _open_committed_artifact(Path(artifact)) as handle:
            header, nonce, aad = _parse_header_stream(handle)
            if header["key_id"] != backup_key_id:
                raise RestoreError("backup_key_mismatch", "the artifact was encrypted with another key id")
            start = handle.tell()
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            remaining = size - start - _TAG_BYTES
            if remaining <= 0:
                raise RestoreError("backup_format_invalid")
            handle.seek(size - _TAG_BYTES)
            tag = handle.read(_TAG_BYTES)
            handle.seek(start)
            decryptor = Cipher(algorithms.AES(backup_key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(aad)
            digest = hashlib.sha256()
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            written = True
            with os.fdopen(descriptor, "wb") as plaintext:
                while remaining:
                    chunk = handle.read(min(_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise RestoreError("backup_format_invalid")
                    remaining -= len(chunk)
                    data = decryptor.update(chunk)
                    plaintext.write(data)
                    digest.update(data)
                try:
                    final = decryptor.finalize()
                except InvalidTag:
                    raise RestoreError(
                        "backup_authentication_failed", "wrong key or the artifact was modified"
                    ) from None
                plaintext.write(final)
                digest.update(final)
                plaintext.flush()
                os.fsync(plaintext.fileno())
        if digest.hexdigest() != header["source_sha256"]:
            raise RestoreError("restored_hash_mismatch")
        connection = sqlite3.connect(f"{output.as_uri()}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            tables = connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        finally:
            connection.close()
        if integrity != "ok":
            raise RestoreError("restored_integrity_failed")
        current, head, compatible = _schema_report(output)
        info = output.stat()
        directory = destination.stat()
        if (
            stat.S_IMODE(info.st_mode) != 0o600
            or stat.S_IMODE(directory.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise RestoreError("restored_permissions_invalid")
        return RestoreReceipt(
            restored_path=str(parent / destination.name / RESTORED_NAME),
            restored_sha256=digest.hexdigest(),
            backup_created_at=str(header["created_at"]),
            backup_key_id=str(header["key_id"]),
            backup_schema_head=str(header["schema_head"]),
            integrity=integrity,
            foreign_key_violations=foreign,
            schema_current=current,
            schema_head=head,
            schema_compatible=compatible,
            tables=int(tables),
        )
    except EncryptedBackupError as error:
        _discard(output if written else None, destination)
        raise RestoreError(error.stable_code) from None
    except BaseException:
        _discard(output if written else None, destination)
        raise


def _discard(output: Optional[Path], destination: Path) -> None:
    if output is not None:
        output.unlink(missing_ok=True)
    try:
        destination.rmdir()
    except OSError:
        pass


def activate_restored(
    restored: Path,
    *,
    restored_sha256: str,
    current_sha256: Optional[str],
    root: Optional[Path] = None,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict[str, Optional[str]]:
    """Replace the runtime database with a verified restore, preserving the old file."""
    from ..installation import RUNTIME_DATABASE_NAME, choose_database, require_designated

    designated = require_designated(root, home=home)
    target = designated / RUNTIME_DATABASE_NAME
    restored = Path(restored).resolve(strict=True)
    for suffix in ("-wal", "-shm", "-journal"):
        if target.with_name(target.name + suffix).exists() or restored.with_name(restored.name + suffix).exists():
            raise RestoreError("database_in_use", "stop the app and daemon first")
    if _sha256(restored) != restored_sha256:
        raise RestoreError("restored_hash_mismatch", "the restored file is not the one you verified")
    if restored.stat().st_dev != designated.stat().st_dev:
        raise RestoreError(
            "restore_on_other_filesystem", "restore into a directory inside the checkout, e.g. .local/restores/"
        )
    preserved: Optional[Path] = None
    if target.exists():
        if current_sha256 is None or _sha256(target) != current_sha256:
            raise RestoreError(
                "current_database_hash_mismatch", "name the current database's exact sha256 to replace it"
            )
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        preserved = target.with_name(f"{RUNTIME_DATABASE_NAME}.before-restore-{stamp}")
        if preserved.exists():
            raise RestoreError("preserved_name_taken")
        os.rename(target, preserved)
    os.chmod(restored, 0o600)
    try:
        os.rename(restored, target)
    except OSError:
        if preserved is not None:
            os.rename(preserved, target)
        raise
    choose_database(target, sha256=restored_sha256, root=designated, home=home, replace=True)
    return {"activated": str(target), "preserved_previous": None if preserved is None else str(preserved)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="decrypt and validate into a new directory (read-only for live data)")
    verify.add_argument("--artifact", required=True, type=Path)
    verify.add_argument("--into", required=True, type=Path)
    activate = commands.add_parser("activate", help="CHANGES STATE: replace the runtime database with a verified restore")
    activate.add_argument("--restored", required=True, type=Path)
    activate.add_argument("--restored-sha256", required=True)
    activate.add_argument("--current-sha256")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            from ..config import load_config
            from ..security.secrets import load_role_secrets, validate_base64_key

            config = load_config()
            secrets = load_role_secrets("backup", config=config)
            key = validate_base64_key("backup_encryption_key", secrets.backup_encryption_key)
            try:
                receipt = restore_backup(
                    args.artifact, args.into, backup_key=bytes(key), backup_key_id=config.encryption.backup_key_id
                )
            finally:
                for index in range(len(key)):
                    key[index] = 0
            print(json.dumps(asdict(receipt), indent=2, sort_keys=True))
            print(
                "Nothing was activated. To use it: stop the app and daemon, then run "
                f"`python -m trading_assistant.ops.restore activate --restored {receipt.restored_path} "
                f"--restored-sha256 {receipt.restored_sha256} --current-sha256 <shasum -a 256 trading_assistant.db>`",
                file=sys.stderr,
            )
            return 0
        result = activate_restored(
            args.restored, restored_sha256=args.restored_sha256, current_sha256=args.current_sha256
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except RestoreError as error:
        print(f"restore refused: {error.code}", file=sys.stderr)
        return 1
    except Exception as error:
        code = getattr(error, "code", None) or type(error).__name__
        print(f"restore refused: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
