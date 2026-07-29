"""Morning preflight: verify every subsystem before starting the daemon.

    python -m trading_assistant.preflight

Prints a PASS/FAIL/SKIP/NEEDS-ME table and exits non-zero on any FAIL or
NEEDS-ME item. Run this before starting the app + daemon each day.
"""

from __future__ import annotations

from contextlib import contextmanager
import sys
from dataclasses import dataclass
from datetime import datetime
import re
from uuid import uuid4

from .config import BrokerKind, TradingMode, load_config
from .db.schema import SchemaOutOfDate
from .security.secrets import (
    MacOSKeychainSecretProvider,
    RuntimeSecrets,
    SecretProvider,
    app_secret_quality_ok,
    load_role_secrets,
    secret_is_set,
    secret_value,
)

PASS, FAIL, SKIP, NEEDS = "PASS", "FAIL", "SKIP", "NEEDS-ME"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class StructuralCheck:
    """One local startup prerequisite, deliberately separate from broker preflight."""

    name: str
    status: str
    code: str

    @property
    def passed(self) -> bool:
        return self.status == "passed"


class SensitiveEncryptionStateInspector:
    """Read the singleton encryption state and fail closed on ambiguity."""

    _KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}")
    _HASH = re.compile(r"[0-9a-f]{64}")
    _BLOCKED_CODES = {
        "required": "sensitive_migration_required",
        "migrating": "sensitive_migration_migrating",
        "rotating": "sensitive_migration_rotating",
        "failed": "sensitive_migration_failed",
    }

    def __init__(
        self,
        engine,
        *,
        schema_version: int,
        active_key_id: str,
        cipher=None,
    ) -> None:
        self._engine = engine
        self._schema_version = schema_version
        self._active_key_id = active_key_id
        self._cipher = cipher

    @staticmethod
    def _blocked(code: str) -> StructuralCheck:
        return StructuralCheck("encryption", "blocked", code)

    @staticmethod
    def _timestamp(value: object) -> bool:
        return isinstance(value, datetime) and value.tzinfo is not None

    def inspect(self, *, metadata_only: bool = False) -> StructuralCheck:
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import Session

            from .db.models import SensitiveMigrationState

            with Session(self._engine) as session:
                rows = session.scalars(
                    select(SensitiveMigrationState)
                ).all()
        except Exception:
            return self._blocked("sensitive_migration_state_invalid")

        if len(rows) != 1 or rows[0].singleton_id != 1:
            return self._blocked("sensitive_migration_state_invalid")
        row = rows[0]
        if (
            isinstance(row.schema_version, bool)
            or not isinstance(row.schema_version, int)
            or row.schema_version <= 0
        ):
            return self._blocked("sensitive_migration_state_invalid")
        if row.schema_version != self._schema_version:
            return self._blocked("sensitive_schema_mismatch")
        if (
            not isinstance(row.active_key_id, str)
            or self._KEY_ID.fullmatch(row.active_key_id) is None
        ):
            return self._blocked("sensitive_migration_state_invalid")
        if row.active_key_id != self._active_key_id:
            return self._blocked("sensitive_active_key_mismatch")
        if (
            row.state not in {
                "required",
                "migrating",
                "complete",
                "rotating",
                "failed",
            }
            or isinstance(row.rows_total, bool)
            or not isinstance(row.rows_total, int)
            or isinstance(row.rows_completed, bool)
            or not isinstance(row.rows_completed, int)
            or row.rows_total < 0
            or row.rows_completed < 0
            or row.rows_completed > row.rows_total
            or not self._timestamp(row.updated_at)
        ):
            return self._blocked("sensitive_migration_state_invalid")

        if row.state == "required":
            if (
                row.started_at is not None
                or row.completed_at is not None
                or row.rows_completed != 0
            ):
                return self._blocked("sensitive_migration_state_invalid")
            return self._blocked(self._BLOCKED_CODES[row.state])

        if row.state in {"migrating", "rotating", "failed"}:
            if (
                not self._timestamp(row.started_at)
                or row.completed_at is not None
                or row.started_at > row.updated_at
            ):
                return self._blocked("sensitive_migration_state_invalid")
            return self._blocked(self._BLOCKED_CODES[row.state])

        if (
            row.rows_completed != row.rows_total
            or not self._timestamp(row.started_at)
            or not self._timestamp(row.completed_at)
            or not row.started_at <= row.completed_at <= row.updated_at
            or not isinstance(row.backup_path_hash, str)
            or self._HASH.fullmatch(row.backup_path_hash) is None
        ):
            return self._blocked("sensitive_migration_state_invalid")
        if metadata_only:
            return StructuralCheck("encryption", "passed", "ok")
        if self._cipher is None:
            return self._blocked("sensitive_key_unavailable")
        try:
            from .ops.encrypt_sensitive import (
                SensitiveMigrationError,
                inspect_sensitive_envelopes,
            )

            inspect_sensitive_envelopes(
                self._engine,
                self._cipher,
                active_key_id=self._active_key_id,
                schema_version=self._schema_version,
            )
        except SensitiveMigrationError as exc:
            return self._blocked(exc.stable_code)
        except Exception:
            return self._blocked("sensitive_envelope_scan_invalid")
        return StructuralCheck("encryption", "passed", "ok")


def structural_runtime_check(config, secrets: RuntimeSecrets) -> StructuralCheck:
    """Validate only local config/secret invariants; never call a provider."""
    if not app_secret_quality_ok(secrets.app_api_token):
        return StructuralCheck("keychain", "blocked", "app_secret_quality_invalid")
    if not (
        config.trading.mode is TradingMode.PAPER
        and config.trading.broker is BrokerKind.ALPACA
    ):
        return StructuralCheck("paper_configuration", "blocked", "paper_configuration_invalid")
    if (
        config.features.auto_execute_preapproved_rules
        or config.execution.prefer_bracket_orders
        or config.llm.fallback_provider is not None
    ):
        return StructuralCheck("disabled_integrations", "blocked", "unsafe_feature_enabled")
    if config.integrations.webhooks_enabled or config.integrations.composio_enabled:
        return StructuralCheck("disabled_integrations", "blocked", "integration_enabled")
    return StructuralCheck("runtime_configuration", "passed", "ok")


def _safe_exception_code(_exc: Exception) -> str:
    """Return a fixed code without exposing provider type or text."""
    return "dependency_failed"


def _default_encryption_check(
    config,
    secrets: RuntimeSecrets | None,
) -> StructuralCheck:
    """Inspect key metadata and migration state without constructing a cipher."""

    try:
        from .db.session import create_db_engine

        if secrets is None:
            return StructuralCheck(
                "encryption",
                "blocked",
                "sensitive_key_unavailable",
            )
        expected_key_ids = (
            config.encryption.active_key_id,
            *config.encryption.retained_key_ids,
        )
        if tuple(secrets.field_encryption_keys) != expected_key_ids or not all(
            secret_is_set(secrets.field_encryption_keys[key_id])
            for key_id in expected_key_ids
        ):
            return StructuralCheck(
                "encryption",
                "blocked",
                "sensitive_key_unavailable",
            )
        return SensitiveEncryptionStateInspector(
            create_db_engine(secrets.database_url),
            schema_version=config.encryption.schema_version,
            active_key_id=config.encryption.active_key_id,
            cipher=None,
        ).inspect(metadata_only=True)
    except Exception:
        return StructuralCheck(
            "encryption",
            "blocked",
            "sensitive_migration_state_invalid",
        )


def _structural_preflight_checks(
    config,
    secrets: RuntimeSecrets | None,
    *,
    provider: SecretProvider | None,
    tls_validator=None,
    encryption_checker=None,
) -> list[Result]:
    """Run all five local release prerequisites without constructing clients."""

    from .ops.tls import validate_tls_material
    from .security.outbound import configured_origins_match_manifest
    from .security.secrets import _required_fields
    from .security.transport import TransportPolicy

    tls_validator = tls_validator or validate_tls_material
    encryption_checker = encryption_checker or _default_encryption_check

    provider_proven = (
        isinstance(provider, MacOSKeychainSecretProvider)
        and provider.provider_name == "macos-keychain"
    )
    if not provider_proven:
        keychain = Result("KEYCHAIN", FAIL, "provider_unproven")
    else:
        try:
            required = _required_fields("preflight", config)
            fields_present = secrets is not None and all(
                secret_is_set(getattr(secrets, field_name))
                for field_name in required
            )
            expected_key_ids = (
                config.encryption.active_key_id,
                *config.encryption.retained_key_ids,
            )
            field_keys_present = (
                secrets is not None
                and tuple(secrets.field_encryption_keys)
                == expected_key_ids
                and all(
                    secret_is_set(secrets.field_encryption_keys[key_id])
                    for key_id in expected_key_ids
                )
            )
        except Exception:
            fields_present = False
            field_keys_present = False
        keychain_ok = fields_present and field_keys_present
        keychain = Result(
            "KEYCHAIN",
            PASS if keychain_ok else FAIL,
            "ok" if keychain_ok else "required_fields_missing",
        )

    try:
        if (
            config.server.bind_host != "127.0.0.1"
            or config.server.port != 8020
            or str(config.server.origin).rstrip("/")
            != "https://localhost:8020"
            or set(config.server.allowed_hosts)
            != {"localhost", "127.0.0.1", "::1"}
            or config.server.secure_cookies is not True
            or str(config.server.tls_cert_path)
            != ".local/tls/localhost.pem"
            or str(config.server.tls_key_path)
            != ".local/tls/localhost-key.pem"
        ):
            raise RuntimeError("local transport configuration invalid")
        TransportPolicy.production(
            config.server,
            request_bounds=config.security.request_bounds,
        )
        tls_validator(config.server)
    except Exception:
        local_tls = Result("LOCAL_TLS", FAIL, "local_tls_invalid")
    else:
        local_tls = Result("LOCAL_TLS", PASS, "ok")

    try:
        encryption = encryption_checker(config, secrets)
        encryption_ok = (
            isinstance(encryption, StructuralCheck)
            and encryption.passed
        )
        encryption_detail = (
            "ok"
            if encryption_ok
            else encryption.code
            if isinstance(encryption, StructuralCheck)
            else "encryption_state_unproven"
        )
    except Exception:
        encryption_ok = False
        encryption_detail = "encryption_state_unproven"
    field_encryption = Result(
        "FIELD_ENCRYPTION",
        PASS if encryption_ok else FAIL,
        encryption_detail,
    )

    try:
        outbound_ok = configured_origins_match_manifest(
            config.provider_origins
        )
    except Exception:
        outbound_ok = False
    outbound = Result(
        "OUTBOUND_ORIGINS",
        PASS if outbound_ok else FAIL,
        "ok" if outbound_ok else "origin_manifest_mismatch",
    )

    try:
        integrations_ok = (
            config.integrations.webhooks_enabled is False
            and config.integrations.composio_enabled is False
        )
    except Exception:
        integrations_ok = False
    integrations = Result(
        "INTEGRATIONS_DISABLED",
        PASS if integrations_ok else FAIL,
        "ok" if integrations_ok else "integration_enabled",
    )
    return [
        keychain,
        local_tls,
        field_encryption,
        outbound,
        integrations,
    ]


def _config_parses() -> Result:
    try:
        load_config("config.yaml")
        return Result("config.yaml parses (extra=forbid)", PASS)
    except Exception as e:
        return Result("config.yaml parses", FAIL, _safe_exception_code(e))


def _paper_only(config) -> Result:
    ok = (
        config.trading.mode is TradingMode.PAPER
        and config.trading.broker is BrokerKind.ALPACA
    )
    return Result(
        "paper-only Alpaca configuration",
        PASS if ok else FAIL,
        f"mode={config.trading.mode.value} broker={config.trading.broker.value}",
    )


def _dangerous_switches_off(
    config,
    secrets: RuntimeSecrets,
) -> Result:
    enabled = []
    if config.features.auto_execute_preapproved_rules:
        enabled.append("autoexecute")
    if config.execution.prefer_bracket_orders:
        enabled.append("brackets")
    if config.llm.fallback_provider is not None:
        enabled.append("llm_fallback")
    if secret_is_set(secrets.live_trading_confirm):
        enabled.append("live_confirmation")
    return Result(
        "dangerous switches OFF",
        FAIL if enabled else PASS,
        "all disabled" if not enabled else "enabled=" + ",".join(enabled),
    )


def _app_secret_quality(secrets: RuntimeSecrets) -> Result:
    ok = app_secret_quality_ok(secrets.app_api_token)
    return Result(
        "operator login secret quality",
        PASS if ok else FAIL,
        (
            "configured (basic format/placeholder checks passed; "
            "not an entropy proof)"
            if ok
            else "APP_API_TOKEN must be >=32 characters, non-placeholder, "
            "and non-periodic"
        ),
    )


def _alpaca(
    secrets: RuntimeSecrets,
) -> tuple[Result, Result, Result]:
    if not (
        secret_is_set(secrets.alpaca_api_key)
        and secret_is_set(secrets.alpaca_secret_key)
    ):
        n = Result("Alpaca paper auth", NEEDS, "set ALPACA keys, then re-run")
        return n, Result("market clock reachable", NEEDS), Result("data bars reachable", NEEDS)

    try:
        from .broker.alpaca import AlpacaBroker, AlpacaClock

        broker = AlpacaBroker.from_credentials(
            secret_value(secrets.alpaca_api_key),
            secret_value(secrets.alpaca_secret_key),
            paper=True,
            runtime_role="preflight",
        )
    except Exception as exc:
        detail = _safe_exception_code(exc)
        auth = Result("Alpaca paper auth", FAIL, detail)
        data = Result("data reachable", FAIL, detail)
    else:
        try:
            acct = broker.get_account()
            positions = broker.get_positions()
            if (
                not acct.is_valid
                or any(
                    not position.risk_values_valid
                    for position in positions
                )
            ):
                raise ValueError("invalid broker account snapshot")
            auth = Result("Alpaca paper auth", PASS, f"equity={acct.equity}")
        except Exception as exc:
            auth = Result(
                "Alpaca paper auth",
                FAIL,
                _safe_exception_code(exc),
            )

        try:
            quote = broker.get_quote("AAPL")
            data = Result(
                "data reachable (AAPL quote)",
                PASS,
                f"last={quote.last}",
            )
        except Exception as exc:
            data = Result(
                "data reachable",
                FAIL,
                _safe_exception_code(exc),
            )

    try:
        clock_client = AlpacaClock.from_credentials(
            secret_value(secrets.alpaca_api_key),
            secret_value(secrets.alpaca_secret_key),
            paper=True,
            runtime_role="preflight",
        )
        clock = Result(
            "market clock reachable",
            PASS,
            f"open={clock_client.is_open()}",
        )
    except Exception as exc:
        clock = Result(
            "market clock reachable",
            FAIL,
            _safe_exception_code(exc),
        )

    return auth, clock, data


def _db(
    secrets: RuntimeSecrets,
) -> tuple[Result, Result, Result]:
    try:
        from sqlalchemy import text

        from .db.schema import require_current_schema
        from .db.session import create_db_engine

        engine = create_db_engine(secrets.database_url)
        require_current_schema(engine)
        schema = Result("database schema current", PASS)
        with engine.connect() as c:
            mode = c.execute(text("PRAGMA journal_mode")).scalar()
        wal = Result("DB WAL mode", PASS if str(mode).lower() == "wal" else FAIL, f"journal_mode={mode}")
        # Kill-switch state
        from sqlalchemy.orm import Session

        from .db.models import CircuitBreakerState
        with Session(engine) as s:
            tripped = [
                row.scope_key
                for row in s.query(CircuitBreakerState).filter_by(tripped=True).all()
            ]
        ks = Result("kill switches", PASS if not tripped else FAIL,
                    "all clear" if not tripped else f"TRIPPED: {tripped} (reset before trading)")
        return schema, wal, ks
    except SchemaOutOfDate:
        err = "schema_out_of_date"
        return (
            Result("database schema current", FAIL, err),
            Result("DB WAL mode", FAIL, err),
            Result("kill switches", FAIL, err),
        )
    except Exception as e:
        err = _safe_exception_code(e)
        return (
            Result("database schema current", FAIL, err),
            Result("DB WAL mode", FAIL, err),
            Result("kill switches", FAIL, err),
        )


def _reconciliation(service) -> Result:
    """Repair stale order statuses, then require local positions to match Alpaca."""
    try:
        request_id = uuid4().hex
        order_sync = service.sync_open_orders(
            actor="preflight:startup",
            reason="preflight broker order reconciliation",
            request_id=request_id,
        )
        positions = service.reconcile_positions(
            actor="preflight:startup",
            reason="preflight position reconciliation",
            request_id=request_id,
        )
        if order_sync.get("failed", 0):
            return Result(
                "broker/local reconciliation",
                FAIL,
                f"order status sync failures: {order_sync}",
            )
        if not positions["reconciled"]:
            return Result(
                "broker/local reconciliation",
                FAIL,
                f"drift={positions['drift']} order_sync={order_sync}",
            )
        return Result(
            "broker/local reconciliation",
            PASS,
            f"positions match; order_sync={order_sync}",
        )
    except Exception as e:
        return Result(
            "broker/local reconciliation", FAIL, _safe_exception_code(e)
        )


@contextmanager
def _build_service(config, secrets: RuntimeSecrets):
    from .bootstrap import build_container

    container = build_container(
        config,
        secrets,
        runtime_role="app",
    )
    primary_failure = False
    try:
        yield container.service
    except BaseException:
        primary_failure = True
        raise
    finally:
        guard = getattr(container, "runtime_tenure_guard", None)
        if guard is not None:
            try:
                released = guard.close()
            except BaseException:
                if not primary_failure:
                    raise RuntimeError(
                        "runtime_tenure_cleanup_uncertain"
                    ) from None
            else:
                if not released and not primary_failure:
                    raise RuntimeError(
                        "runtime_tenure_cleanup_uncertain"
                    )


def _llm_provider_configured(
    config,
    secrets: RuntimeSecrets,
) -> Result:
    provider_keys = {
        "anthropic": secrets.anthropic_api_key,
        "gemini": secrets.gemini_api_key,
        "groq": secrets.groq_api_key,
    }
    provider = config.llm.provider
    if provider not in provider_keys:
        return Result("configured LLM provider", FAIL, "unsupported provider")
    if not secret_is_set(provider_keys[provider]):
        return Result(
            "configured LLM provider",
            NEEDS,
            f"set credentials for provider={provider}",
        )
    return Result("configured LLM provider", PASS, f"provider={provider}")


def _notification_configuration(
    config,
    secrets: RuntimeSecrets,
) -> Result:
    if not config.features.telegram_notifications:
        return Result("notification configuration", SKIP, "disabled; no message sent")
    configured = (
        secret_is_set(secrets.telegram_bot_token)
        and secret_is_set(secrets.telegram_chat_id)
    )
    return Result(
        "notification configuration",
        PASS if configured else FAIL,
        (
            "enabled; no message sent"
            if configured
            else "enabled but credentials missing; no message sent"
        ),
    )


def _print_results(results: list[Result]) -> int:
    width = max(len(result.name) for result in results)
    print("\nPREFLIGHT\n" + "-" * (width + 40))
    for result in results:
        print(
            f"  {result.status:8} {result.name:<{width}}  "
            f"{result.detail}"
        )
    failed = [result for result in results if result.status == FAIL]
    needs = [result for result in results if result.status == NEEDS]
    print("-" * (width + 40))
    print(
        f"  {len(failed)} FAIL · {len(needs)} NEEDS-ME · "
        f"{sum(result.status == PASS for result in results)} PASS · "
        f"{sum(result.status == SKIP for result in results)} SKIP"
    )
    not_ready = bool(failed or needs)
    print(
        "  => "
        + (
            "NOT READY — fix FAIL/NEEDS-ME items"
            if not_ready
            else "READY"
        )
        + "\n"
    )
    return 1 if not_ready else 0


def run(
    *,
    provider: SecretProvider | None = None,
    provider_factory=None,
    tls_validator=None,
    encryption_checker=None,
) -> int:
    from .logging import runtime_startup

    config = load_config("config.yaml")
    selected_provider: SecretProvider | None = provider
    if selected_provider is None:
        factory = provider_factory or MacOSKeychainSecretProvider
        try:
            selected_provider = factory()
        except Exception:
            selected_provider = None
    secrets: RuntimeSecrets | None = None
    try:
        if isinstance(
            selected_provider,
            MacOSKeychainSecretProvider,
        ):
            secrets = load_role_secrets(
                "preflight",
                config=config,
                provider=selected_provider,
            )
    except Exception:
        secrets = None
    run_options = {"provider": selected_provider}
    if tls_validator is not None:
        run_options["tls_validator"] = tls_validator
    if encryption_checker is not None:
        run_options["encryption_checker"] = encryption_checker
    if secrets is None:
        return _run(config, None, **run_options)
    with runtime_startup("preflight", secrets):
        return _run(config, secrets, **run_options)


def _run(
    config,
    secrets: RuntimeSecrets | None = None,
    *,
    provider: SecretProvider | None = None,
    tls_validator=None,
    encryption_checker=None,
) -> int:
    structural = _structural_preflight_checks(
        config,
        secrets,
        provider=provider,
        tls_validator=tls_validator,
        encryption_checker=encryption_checker,
    )
    if any(result.status in {FAIL, NEEDS} for result in structural):
        return _print_results(structural)
    if secrets is None or not isinstance(
        provider,
        MacOSKeychainSecretProvider,
    ):
        return _print_results(
            [
                Result(
                    "KEYCHAIN",
                    FAIL,
                    "provider_unproven",
                ),
                *structural[1:],
            ]
        )
    results = [
        *structural,
        _config_parses(),
        _paper_only(config),
        _dangerous_switches_off(config, secrets),
        _app_secret_quality(secrets),
    ]
    results.extend(_alpaca(secrets))
    results.extend(_db(secrets))
    if (
        secret_is_set(secrets.alpaca_api_key)
        and secret_is_set(secrets.alpaca_secret_key)
    ):
        with _build_service(config, secrets) as service:
            results.append(_reconciliation(service))
    else:
        results.append(
            Result(
                "broker/local reconciliation",
                NEEDS,
                "set ALPACA keys, then re-run",
            )
        )
    results.append(_llm_provider_configured(config, secrets))
    results.append(_notification_configuration(config, secrets))

    return _print_results(results)


if __name__ == "__main__":
    sys.exit(run())
