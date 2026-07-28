"""Strict local HTTPS application launcher.

The startup guard intentionally performs *only* structural local checks.  It
does not construct a broker, reconcile, notify, or call any provider.  The app
composition root performs its one bounded reconciliation attempt after these
checks have passed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import uvicorn
from sqlalchemy import text

from ..config import load_config
from ..db.schema import require_current_schema
from ..db.session import create_db_engine
from ..preflight import (
    SensitiveEncryptionStateInspector,
    StructuralCheck,
    structural_runtime_check,
)
from ..security.secrets import RuntimeSecrets, load_role_secrets
from ..security.transport import TransportPolicy
from .control import start_app_control
from .tls import TLSMaterialError, validate_tls_material


class EncryptionStateInspector(Protocol):
    """Injectable encryption-state proof for focused local tests."""

    def inspect(self) -> StructuralCheck: ...


class StartupGuardBlocked(RuntimeError):
    """One or more structural prerequisites prevent application construction."""

    def __init__(self, checks: tuple[StructuralCheck, ...]) -> None:
        self.checks = checks
        codes = ",".join(check.code for check in checks if not check.passed)
        super().__init__(f"startup_guard_blocked:{codes}")


def _database_check(secrets: RuntimeSecrets) -> StructuralCheck:
    """Verify current schema and WAL locally without creating a broker/service."""
    try:
        engine = create_db_engine(secrets.database_url)
        require_current_schema(engine)
        if engine.url.get_backend_name() == "sqlite":
            with engine.connect() as connection:
                journal_mode = connection.execute(
                    text("PRAGMA journal_mode")
                ).scalar_one()
            if str(journal_mode).lower() != "wal":
                return StructuralCheck("database", "blocked", "sqlite_wal_required")
    except Exception:
        return StructuralCheck("database", "blocked", "schema_or_database_invalid")
    return StructuralCheck("database", "passed", "ok")


def _transport_check(config) -> StructuralCheck:
    try:
        policy = TransportPolicy.production(config.server)
    except Exception:
        return StructuralCheck("loopback_https", "blocked", "unsafe_bind_or_origin")
    bind_host = config.server.bind_host.strip("[]").lower()
    if bind_host not in policy.allowed_hosts:
        return StructuralCheck("loopback_https", "blocked", "unsafe_bind_or_origin")
    return StructuralCheck("loopback_https", "passed", "ok")


def _tls_check(config) -> StructuralCheck:
    try:
        validate_tls_material(config.server)
    except TLSMaterialError as exc:
        return StructuralCheck("tls", "blocked", exc.code)
    return StructuralCheck("tls", "passed", "ok")


def run_startup_guard(
    *,
    config=None,
    secrets: RuntimeSecrets | None = None,
    encryption_inspector: EncryptionStateInspector | None = None,
) -> tuple[StructuralCheck, ...]:
    """Run local structural checks and raise before app construction on failure."""
    config = config or load_config()
    if secrets is None:
        try:
            secrets = load_role_secrets("app", config=config)
        except Exception:
            checks = (
                StructuralCheck("keychain", "blocked", "keychain_unavailable"),
            )
            raise StartupGuardBlocked(checks) from None
    if encryption_inspector is None:
        try:
            encryption_check = SensitiveEncryptionStateInspector(
                create_db_engine(secrets.database_url),
                schema_version=config.encryption.schema_version,
                active_key_id=config.encryption.active_key_id,
            ).inspect()
        except Exception:
            encryption_check = StructuralCheck(
                "encryption",
                "blocked",
                "sensitive_migration_state_invalid",
            )
    else:
        try:
            encryption_check = encryption_inspector.inspect()
        except Exception:
            encryption_check = StructuralCheck(
                "encryption",
                "blocked",
                "sensitive_migration_state_invalid",
            )
    checks = (
        structural_runtime_check(config, secrets),
        _transport_check(config),
        _tls_check(config),
        _database_check(secrets),
        encryption_check,
    )
    if any(not check.passed for check in checks):
        raise StartupGuardBlocked(checks)
    return checks


def main(argv: list[str] | None = None) -> int:
    if argv:
        raise SystemExit("ops.serve accepts no arguments")
    config = load_config()
    run_startup_guard(config=config)
    control = start_app_control(Path.cwd())
    try:
        uvicorn.run(
            "trading_assistant.app.main:create_app",
            factory=True,
            host=config.server.bind_host,
            port=config.server.port,
            ssl_certfile=str(config.server.tls_cert_path),
            ssl_keyfile=str(config.server.tls_key_path),
            proxy_headers=False,
            forwarded_allow_ips="",
            access_log=False,
        )
    finally:
        control.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
