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
    SecretBoundaryError,
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

    def inspect(self) -> StructuralCheck:
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
    secrets: RuntimeSecrets,
) -> StructuralCheck:
    """Inspect only configured key material and the local migration state."""

    try:
        from .db.session import create_db_engine
        from .security.crypto import build_sensitive_data_cipher

        cipher = build_sensitive_data_cipher(
            config.encryption,
            secrets,
        )
        return SensitiveEncryptionStateInspector(
            create_db_engine(secrets.database_url),
            schema_version=config.encryption.schema_version,
            active_key_id=config.encryption.active_key_id,
            cipher=cipher,
        ).inspect()
    except Exception:
        return StructuralCheck(
            "encryption",
            "blocked",
            "sensitive_migration_state_invalid",
        )


def _structural_preflight_checks(
    config,
    secrets: RuntimeSecrets,
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

    keychain_ok = (
        provider is None
        or (
            isinstance(provider, MacOSKeychainSecretProvider)
            and provider.provider_name == "macos-keychain"
        )
    )
    try:
        required = _required_fields("preflight", config)
        fields_present = all(
            secret_is_set(getattr(secrets, field_name))
            for field_name in required
        )
        expected_key_ids = (
            config.encryption.active_key_id,
            *config.encryption.retained_key_ids,
        )
        field_keys_present = (
            tuple(secrets.field_encryption_keys) == expected_key_ids
            and all(
                secret_is_set(secrets.field_encryption_keys[key_id])
                for key_id in expected_key_ids
            )
        )
    except Exception:
        fields_present = False
        field_keys_present = False
    keychain_ok = keychain_ok and fields_present and field_keys_present
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
    except Exception:
        encryption_ok = False
    field_encryption = Result(
        "FIELD_ENCRYPTION",
        PASS if encryption_ok else FAIL,
        "ok" if encryption_ok else "migration_incomplete",
    )

    outbound_ok = configured_origins_match_manifest(
        config.provider_origins
    )
    outbound = Result(
        "OUTBOUND_ORIGINS",
        PASS if outbound_ok else FAIL,
        "ok" if outbound_ok else "origin_manifest_mismatch",
    )

    integrations_ok = (
        config.integrations.webhooks_enabled is False
        and config.integrations.composio_enabled is False
    )
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


def _run_partial_keychain(
    config,
    presence,
) -> int:
    required_accounts = {
        "app_api_token",
        "alpaca_api_key",
        "alpaca_secret_key",
        "database_url",
        "candidate_signing_key",
        "backup_encryption_key",
        {
            "anthropic": "anthropic_api_key",
            "gemini": "gemini_api_key",
            "groq": "groq_api_key",
        }.get(config.llm.provider, "unsupported_llm_provider"),
        *(
            f"field-encryption/{key_id}"
            for key_id in (
                config.encryption.active_key_id,
                *config.encryption.retained_key_ids,
            )
        ),
    }
    missing = sorted(
        account
        for account in required_accounts
        if presence.get(account) is False
    )
    unavailable = sorted(
        account
        for account in required_accounts
        if presence.get(account) is None
    )
    if missing:
        role_detail = "missing=" + ",".join(missing)
    elif unavailable:
        role_detail = "unavailable=" + ",".join(unavailable)
    else:
        role_detail = "present material failed role validation"

    dangerous = []
    if config.features.auto_execute_preapproved_rules:
        dangerous.append("autoexecute")
    if config.execution.prefer_bracket_orders:
        dangerous.append("brackets")
    if config.llm.fallback_provider is not None:
        dangerous.append("llm_fallback")
    if presence.get("live_trading_confirm") is True:
        dangerous.append("live_confirmation")

    origins_ok = False
    integrations_ok = False
    try:
        from .security.outbound import configured_origins_match_manifest

        origins_ok = configured_origins_match_manifest(
            config.provider_origins
        )
        integrations_ok = (
            config.integrations.webhooks_enabled is False
            and config.integrations.composio_enabled is False
        )
    except Exception:
        pass

    results = [
        Result("KEYCHAIN", FAIL, "required_fields_missing"),
        Result("LOCAL_TLS", NEEDS, "keychain_unavailable"),
        Result("FIELD_ENCRYPTION", NEEDS, "keychain_unavailable"),
        Result(
            "OUTBOUND_ORIGINS",
            PASS if origins_ok else FAIL,
            "ok" if origins_ok else "origin_manifest_mismatch",
        ),
        Result(
            "INTEGRATIONS_DISABLED",
            PASS if integrations_ok else FAIL,
            "ok" if integrations_ok else "integration_enabled",
        ),
        _config_parses(),
        _paper_only(config),
        Result("runtime secret role validation", NEEDS, role_detail),
        Result(
            "dangerous switches OFF",
            FAIL if dangerous else PASS,
            (
                "all disabled"
                if not dangerous
                else "enabled=" + ",".join(dangerous)
            ),
        ),
        Result(
            "operator login secret quality",
            NEEDS,
            "role validation incomplete; value not displayed",
        ),
        Result(
            "Alpaca paper auth",
            NEEDS,
            "role validation incomplete; no broker call",
        ),
        Result("market clock reachable", NEEDS, "no broker call"),
        Result("data bars reachable", NEEDS, "no provider call"),
        Result("database schema current", NEEDS, "no database call"),
        Result("DB WAL mode", NEEDS, "no database call"),
        Result("kill switches", NEEDS, "no database call"),
        Result(
            "broker/local reconciliation",
            NEEDS,
            "role validation incomplete; no broker call",
        ),
        Result(
            "configured LLM provider",
            NEEDS,
            f"provider={config.llm.provider}; no provider call",
        ),
        (
            Result(
                "notification configuration",
                NEEDS,
                "enabled; role validation incomplete; no message sent",
            )
            if config.features.telegram_notifications
            else Result(
                "notification configuration",
                SKIP,
                "disabled; no message sent",
            )
        ),
    ]
    return _print_results(results)


def run(*, provider: SecretProvider | None = None) -> int:
    from .logging import runtime_startup

    config = load_config("config.yaml")
    try:
        secrets = (
            load_role_secrets("preflight", config=config)
            if provider is None
            else load_role_secrets(
                "preflight",
                config=config,
                provider=provider,
            )
        )
    except SecretBoundaryError:
        if provider is not None and not isinstance(
            provider,
            MacOSKeychainSecretProvider,
        ):
            return _print_results(
                [
                    Result(
                        "KEYCHAIN",
                        FAIL,
                        "required_fields_missing",
                    ),
                    Result("LOCAL_TLS", NEEDS, "keychain_unavailable"),
                    Result(
                        "FIELD_ENCRYPTION",
                        NEEDS,
                        "keychain_unavailable",
                    ),
                    Result(
                        "OUTBOUND_ORIGINS",
                        FAIL,
                        "origin_manifest_unproven",
                    ),
                    Result(
                        "INTEGRATIONS_DISABLED",
                        FAIL,
                        "integration_state_unproven",
                    ),
                ]
            )
        selected = provider or MacOSKeychainSecretProvider()
        return _run_partial_keychain(
            config,
            selected.read_presence(encryption=config.encryption),
        )
    with runtime_startup("preflight", secrets):
        if provider is None:
            return _run(config, secrets)
        return _run(config, secrets, provider=provider)


def _run(
    config,
    secrets: RuntimeSecrets | None = None,
    *,
    provider: SecretProvider | None = None,
) -> int:
    if secrets is None:
        secrets = config
        config = load_config("config.yaml")
    structural = _structural_preflight_checks(
        config,
        secrets,
        provider=provider,
    )
    if any(result.status in {FAIL, NEEDS} for result in structural):
        return _print_results(structural)
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
