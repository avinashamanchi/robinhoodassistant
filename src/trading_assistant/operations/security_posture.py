"""Typed, redacted, local-only security posture evidence.

This module reports observations only.  Nothing in the trading authority path
imports or consumes these models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import func, or_, select

from ..app.limits import (
    DurableRateLimiter,
    LimitSpec,
    LimitStoreUnavailable,
)
from ..broker.models import OrderStatus
from ..config import AppConfig, TradingMode
from ..db.models import (
    CircuitBreakerState,
    FILL_RECONCILIATION_QUARANTINED,
    FILL_RECONCILIATION_SUPERSEDED,
    Fill,
    Heartbeat,
    MutationInterlock,
    Order,
    Rule,
    RuleGroup,
    RuntimeTenure,
    SensitiveMigrationState,
    StartupReconciliationState,
    UntrustedIngestEvent,
)
from ..llm.budget import (
    ProviderBudgetService,
    ProviderBudgetUnavailable,
)
from ..preflight import SensitiveEncryptionStateInspector
from ..risk.breakers import BreakerScope


UTC = timezone.utc


class PostureName(str, Enum):
    BROKER_MODE = "broker_mode"
    LOOPBACK_HTTPS = "loopback_https"
    TLS = "tls"
    SECRET_PROVIDER = "secret_provider"
    SENSITIVE_ENCRYPTION = "sensitive_encryption"
    REQUEST_BUDGET = "request_budget"
    PROVIDER_BUDGET = "provider_budget"
    WEBHOOK_RECEIVER = "webhook_receiver"
    COMPOSIO_INTEGRATION = "composio_integration"
    QUARANTINE = "quarantine"
    CIRCUIT_BREAKER = "circuit_breaker"
    DAEMON_HEARTBEAT = "daemon_heartbeat"
    STARTUP_RECONCILIATION = "startup_reconciliation"
    QUOTE_FRESHNESS = "quote_freshness"
    RUNTIME_TENURE = "runtime_tenure"
    UNSAFE_ORDERS = "unsafe_orders"
    UNSAFE_FILLS = "unsafe_fills"
    UNSAFE_RULES = "unsafe_rules"
    UNCERTAIN_INTERLOCKS = "uncertain_interlocks"


class PostureStatus(str, Enum):
    PAPER = "paper"
    PASS = "pass"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    DISABLED = "disabled"
    ENABLED = "enabled"
    CLEAR = "clear"
    PRESENT = "present"
    TRIPPED = "tripped"
    FRESH = "fresh"
    STALE = "stale"
    HELD = "held"
    RELEASED = "released"
    FENCED = "fenced"


class StartupCheckName(str, Enum):
    RUNTIME_CONFIGURATION = "runtime_configuration"
    PAPER_CONFIGURATION = "paper_configuration"
    DISABLED_INTEGRATIONS = "disabled_integrations"
    KEYCHAIN = "keychain"
    LOOPBACK_HTTPS = "loopback_https"
    TLS = "tls"
    DATABASE = "database"
    ENCRYPTION = "encryption"


class StartupDetailCode(str, Enum):
    OK = "ok"
    STARTUP_CHECK_UNKNOWN = "startup_check_unknown"
    APP_SECRET_QUALITY_INVALID = "app_secret_quality_invalid"
    PAPER_CONFIGURATION_INVALID = "paper_configuration_invalid"
    UNSAFE_FEATURE_ENABLED = "unsafe_feature_enabled"
    INTEGRATION_ENABLED = "integration_enabled"
    KEYCHAIN_UNAVAILABLE = "keychain_unavailable"
    UNSAFE_BIND_OR_ORIGIN = "unsafe_bind_or_origin"
    SCHEMA_OR_DATABASE_INVALID = "schema_or_database_invalid"
    SQLITE_WAL_REQUIRED = "sqlite_wal_required"
    TLS_ROOT_SYMLINK_FORBIDDEN = "tls_root_symlink_forbidden"
    TLS_DIRECTORY_PERMISSIONS_INVALID = (
        "tls_directory_permissions_invalid"
    )
    TLS_PATH_OUTSIDE_LOCAL_DIRECTORY = (
        "tls_path_outside_local_directory"
    )
    TLS_CERTIFICATE_PERMISSIONS_INVALID = (
        "tls_certificate_permissions_invalid"
    )
    TLS_PRIVATE_KEY_PERMISSIONS_INVALID = (
        "tls_private_key_permissions_invalid"
    )
    TLS_MATERIAL_PARSE_FAILED = "tls_material_parse_failed"
    TLS_CERTIFICATE_NOT_CURRENT = "tls_certificate_not_current"
    TLS_CERTIFICATE_SAN_INVALID = "tls_certificate_san_invalid"
    TLS_CERTIFICATE_KEY_MISMATCH = "tls_certificate_key_mismatch"
    SENSITIVE_MIGRATION_REQUIRED = "sensitive_migration_required"
    SENSITIVE_MIGRATION_MIGRATING = "sensitive_migration_migrating"
    SENSITIVE_MIGRATION_ROTATING = "sensitive_migration_rotating"
    SENSITIVE_MIGRATION_FAILED = "sensitive_migration_failed"
    SENSITIVE_MIGRATION_STATE_INVALID = (
        "sensitive_migration_state_invalid"
    )
    SENSITIVE_SCHEMA_MISMATCH = "sensitive_schema_mismatch"
    SENSITIVE_ACTIVE_KEY_MISMATCH = "sensitive_active_key_mismatch"
    SENSITIVE_KEY_UNAVAILABLE = "sensitive_key_unavailable"
    SENSITIVE_ENVELOPE_SCAN_INVALID = (
        "sensitive_envelope_scan_invalid"
    )
    SENSITIVE_PLAINTEXT_DETECTED = "sensitive_plaintext_detected"
    SENSITIVE_ENVELOPE_INVALID = "sensitive_envelope_invalid"
    SENSITIVE_MIXED_KEY = "sensitive_mixed_key"
    SENSITIVE_REGISTRY_SCHEMA_INVALID = (
        "sensitive_registry_schema_invalid"
    )
    SENSITIVE_DATABASE_INVALID = "sensitive_database_invalid"
    SENSITIVE_DATABASE_MISMATCH = "sensitive_database_mismatch"
    SENSITIVE_DATABASE_UNSUPPORTED = "sensitive_database_unsupported"
    SENSITIVE_MIGRATION_DATA_INVALID = (
        "sensitive_migration_data_invalid"
    )
    SENSITIVE_MIGRATION_EVIDENCE_INVALID = (
        "sensitive_migration_evidence_invalid"
    )


class PostureDetailCode(str, Enum):
    BROKER_PAPER_MODE = "broker_paper_mode"
    BROKER_MODE_NOT_PAPER = "broker_mode_not_paper"
    STARTUP_EVIDENCE_UNAVAILABLE = "startup_evidence_unavailable"
    MACOS_KEYCHAIN = "macos_keychain"
    ENCRYPTION_EVIDENCE_UNAVAILABLE = (
        "encryption_evidence_unavailable"
    )
    REQUEST_BUDGET_AVAILABLE = "request_budget_available"
    REQUEST_BUDGET_EXHAUSTED = "request_budget_exhausted"
    PROVIDER_BUDGET_AVAILABLE = "provider_budget_available"
    PROVIDER_BUDGET_EXHAUSTED = "provider_budget_exhausted"
    PROVIDER_RECONCILIATION_REQUIRED = (
        "provider_reconciliation_required"
    )
    BUDGET_EVIDENCE_UNAVAILABLE = "budget_evidence_unavailable"
    INTEGRATION_DISABLED = "integration_disabled"
    INTEGRATION_ENABLED = "integration_enabled"
    QUARANTINE_EMPTY = "quarantine_empty"
    QUARANTINE_ITEMS_PRESENT = "quarantine_items_present"
    BREAKER_CLEAR = "breaker_clear"
    BREAKER_TRIPPED = "breaker_tripped"
    DAEMON_HEARTBEAT_MISSING = "daemon_heartbeat_missing"
    DAEMON_HEARTBEAT_FRESH = "daemon_heartbeat_fresh"
    DAEMON_HEARTBEAT_STALE = "daemon_heartbeat_stale"
    RECONCILIATION_EVIDENCE_UNAVAILABLE = (
        "reconciliation_evidence_unavailable"
    )
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RECONCILIATION_FAILED = "reconciliation_failed"
    RECONCILIATION_CURRENT = "reconciliation_current"
    RECONCILIATION_STALE = "reconciliation_stale"
    QUOTE_EVIDENCE_UNAVAILABLE = "quote_evidence_unavailable"
    RUNTIME_TENURE_MISSING = "runtime_tenure_missing"
    RUNTIME_TENURE_HELD = "runtime_tenure_held"
    RUNTIME_TENURE_RELEASED = "runtime_tenure_released"
    RUNTIME_TENURE_FENCED = "runtime_tenure_fenced"
    RUNTIME_TENURE_STALE = "runtime_tenure_stale"
    UNSAFE_STATE_CLEAR = "unsafe_state_clear"
    UNSAFE_STATE_PRESENT = "unsafe_state_present"
    UNCERTAIN_INTERLOCKS_CLEAR = "uncertain_interlocks_clear"
    UNCERTAIN_INTERLOCKS_PRESENT = "uncertain_interlocks_present"
    DATABASE_EVIDENCE_UNAVAILABLE = "database_evidence_unavailable"


class StartupStructuralCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: StartupCheckName
    status: Literal["pass", "blocked", "unknown"]
    detail_code: StartupDetailCode


class StartupPostureEvidence(BaseModel):
    """Immutable output from the one production startup-evidence chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: AwareDatetime
    structural_checks: tuple[StartupStructuralCheck, ...]
    secret_provider: Literal["macos_keychain"]
    secret_load_status: Literal["pass", "blocked", "unknown"]
    secret_loaded_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def successful_load_has_timestamp(self):
        if (
            self.secret_load_status == "pass"
            and self.secret_loaded_at is None
        ):
            raise ValueError(
                "successful secret load requires an observed timestamp"
            )
        return self


class PostureCheck(BaseModel):
    """One value-free, typed observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: PostureName
    status: PostureStatus
    observed_at: AwareDatetime
    detail_code: PostureDetailCode | StartupDetailCode
    scope: str | None = Field(default=None, min_length=1, max_length=64)
    count: int | None = Field(default=None, ge=0)
    generation: int | None = Field(default=None, ge=0)
    completed_generation: int | None = Field(default=None, ge=0)
    budget_used: int | None = Field(default=None, ge=0)
    budget_remaining: int | None = Field(default=None, ge=0)
    budget_limit: int | None = Field(default=None, ge=0)
    input_tokens_used: int | None = Field(default=None, ge=0)
    input_tokens_remaining: int | None = Field(default=None, ge=0)
    input_tokens_limit: int | None = Field(default=None, ge=0)
    output_tokens_used: int | None = Field(default=None, ge=0)
    output_tokens_remaining: int | None = Field(default=None, ge=0)
    output_tokens_limit: int | None = Field(default=None, ge=0)
    reset_at: AwareDatetime | None = None
    evidence_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    max_age_seconds: float | None = Field(default=None, gt=0)
    migration_state: Literal[
        "required",
        "migrating",
        "complete",
        "rotating",
        "failed",
    ] | None = None
    schema_version: int | None = Field(default=None, ge=1)
    rows_total: int | None = Field(default=None, ge=0)
    rows_completed: int | None = Field(default=None, ge=0)

    @field_validator("scope")
    @classmethod
    def scope_is_typed(cls, value: str | None, info):
        if value is None:
            return value
        name = info.data.get("name")
        if name is PostureName.CIRCUIT_BREAKER:
            BreakerScope.parse(value)
            return value
        allowed: dict[PostureName, set[str]] = {
            PostureName.REQUEST_BUDGET: {
                "login",
                "session_read",
                "broker_read",
                "mutation",
                "approval",
                "privileged",
                "chat",
                "analysis",
                "backtest",
                "provider_read",
                "panic",
            },
            PostureName.PROVIDER_BUDGET: {
                "anthropic",
                "gemini",
                "groq",
            },
            PostureName.QUARANTINE: {
                "received",
                "summarized",
                "rejected",
                "failed",
            },
            PostureName.RUNTIME_TENURE: {
                "app",
                "daemon",
                "mcp",
                "validation",
                "maintenance",
            },
            PostureName.UNSAFE_RULES: {"rules", "rule_groups"},
        }
        if value not in allowed.get(name, set()):
            raise ValueError("posture scope is not valid for this check")
        return value


class SecurityPostureReport(BaseModel):
    """Read-only evidence that can never represent trading authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: AwareDatetime
    checks: tuple[PostureCheck, ...]
    can_trade: Literal[False] = False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def startup_posture_evidence(
    *,
    checks,
    observed_at: datetime,
    secret_loaded_at: datetime,
) -> StartupPostureEvidence:
    """Convert startup guard output without carrying paths or secret metadata."""

    return StartupPostureEvidence(
        observed_at=_as_utc(observed_at),
        structural_checks=tuple(
            StartupStructuralCheck(
                name=check.name,
                status="pass" if check.passed else "blocked",
                detail_code=check.code,
            )
            for check in checks
        ),
        secret_provider="macos_keychain",
        secret_load_status="pass",
        secret_loaded_at=_as_utc(secret_loaded_at),
    )


_LIVE_OR_UNKNOWN_ORDER_STATUSES = (
    OrderStatus.APPROVED.value,
    OrderStatus.APPROVAL_RECORDED.value,
    OrderStatus.SUBMITTING.value,
    OrderStatus.ACCEPTANCE_UNKNOWN.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)
_SAFETY_LATCH_ERROR_CODES = (
    "broker_submission_unknown",
    "cumulative_fill_contradiction",
    "fill_quantity_exceeds_order",
    "indeterminate_cancel",
    "invalid_broker_data",
    "invalid_broker_identity",
    "invalid_cumulative_fill",
    "legacy_unidentified_fill",
    "legacy_unverified_fill",
    "remote_fill_ahead",
    "waiting_for_exact_fill",
)


class SecurityPostureService:
    """Build a local snapshot without calling or mutating any dependency."""

    def __init__(
        self,
        *,
        config: AppConfig,
        session_factory,
        reconciliation_key: str,
        reconciliation_enabled: bool,
        startup_evidence: StartupPostureEvidence | None = None,
        rate_limiter: DurableRateLimiter | None = None,
        provider_budget: ProviderBudgetService | None = None,
        engine=None,
        sensitive_cipher=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._reconciliation_key = reconciliation_key
        self._reconciliation_enabled = reconciliation_enabled
        self._startup_evidence = startup_evidence
        self._rate_limiter = rate_limiter
        self._provider_budget = provider_budget
        self._engine = engine
        self._sensitive_cipher = sensitive_cipher
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _check(
        name: PostureName,
        status: PostureStatus,
        observed_at: datetime,
        detail_code: PostureDetailCode | StartupDetailCode,
        **evidence,
    ) -> PostureCheck:
        return PostureCheck(
            name=name,
            status=status,
            observed_at=observed_at,
            detail_code=detail_code,
            **evidence,
        )

    def _startup_check(
        self,
        name: StartupCheckName,
        posture_name: PostureName,
        observed_at: datetime,
    ) -> PostureCheck:
        evidence = self._startup_evidence
        if evidence is None:
            return self._check(
                posture_name,
                PostureStatus.UNKNOWN,
                observed_at,
                PostureDetailCode.STARTUP_EVIDENCE_UNAVAILABLE,
            )
        found = next(
            (
                item
                for item in evidence.structural_checks
                if item.name is name
            ),
            None,
        )
        if found is None:
            return self._check(
                posture_name,
                PostureStatus.UNKNOWN,
                observed_at,
                StartupDetailCode.STARTUP_CHECK_UNKNOWN,
                evidence_at=evidence.observed_at,
            )
        status = {
            "pass": PostureStatus.PASS,
            "blocked": PostureStatus.BLOCKED,
            "unknown": PostureStatus.UNKNOWN,
        }[found.status]
        return self._check(
            posture_name,
            status,
            observed_at,
            found.detail_code,
            evidence_at=evidence.observed_at,
        )

    def _config_checks(self, observed_at: datetime) -> list[PostureCheck]:
        broker_paper = self._config.trading.mode is TradingMode.PAPER
        return [
            self._check(
                PostureName.BROKER_MODE,
                (
                    PostureStatus.PAPER
                    if broker_paper
                    else PostureStatus.BLOCKED
                ),
                observed_at,
                (
                    PostureDetailCode.BROKER_PAPER_MODE
                    if broker_paper
                    else PostureDetailCode.BROKER_MODE_NOT_PAPER
                ),
            ),
            self._startup_check(
                StartupCheckName.LOOPBACK_HTTPS,
                PostureName.LOOPBACK_HTTPS,
                observed_at,
            ),
            self._startup_check(
                StartupCheckName.TLS,
                PostureName.TLS,
                observed_at,
            ),
            self._secret_check(observed_at),
            self._integration_check(
                PostureName.WEBHOOK_RECEIVER,
                self._config.integrations.webhooks_enabled,
                observed_at,
            ),
            self._integration_check(
                PostureName.COMPOSIO_INTEGRATION,
                self._config.integrations.composio_enabled,
                observed_at,
            ),
            self._check(
                PostureName.QUOTE_FRESHNESS,
                PostureStatus.UNKNOWN,
                observed_at,
                PostureDetailCode.QUOTE_EVIDENCE_UNAVAILABLE,
            ),
        ]

    def _secret_check(self, observed_at: datetime) -> PostureCheck:
        evidence = self._startup_evidence
        if evidence is None:
            return self._check(
                PostureName.SECRET_PROVIDER,
                PostureStatus.UNKNOWN,
                observed_at,
                PostureDetailCode.STARTUP_EVIDENCE_UNAVAILABLE,
            )
        status = {
            "pass": PostureStatus.PASS,
            "blocked": PostureStatus.BLOCKED,
            "unknown": PostureStatus.UNKNOWN,
        }[evidence.secret_load_status]
        return self._check(
            PostureName.SECRET_PROVIDER,
            status,
            observed_at,
            PostureDetailCode.MACOS_KEYCHAIN,
            evidence_at=evidence.secret_loaded_at,
        )

    @classmethod
    def _integration_check(
        cls,
        name: PostureName,
        enabled: bool,
        observed_at: datetime,
    ) -> PostureCheck:
        return cls._check(
            name,
            (
                PostureStatus.ENABLED
                if enabled
                else PostureStatus.DISABLED
            ),
            observed_at,
            (
                PostureDetailCode.INTEGRATION_ENABLED
                if enabled
                else PostureDetailCode.INTEGRATION_DISABLED
            ),
        )

    def _encryption_check(
        self,
        observed_at: datetime,
        *,
        database_available: bool,
    ) -> PostureCheck:
        if not database_available:
            return self._unknown(
                PostureName.SENSITIVE_ENCRYPTION,
                observed_at,
            )
        if self._engine is None:
            return self._check(
                PostureName.SENSITIVE_ENCRYPTION,
                PostureStatus.UNKNOWN,
                observed_at,
                PostureDetailCode.ENCRYPTION_EVIDENCE_UNAVAILABLE,
            )
        try:
            result = SensitiveEncryptionStateInspector(
                self._engine,
                schema_version=self._config.encryption.schema_version,
                active_key_id=self._config.encryption.active_key_id,
                cipher=self._sensitive_cipher,
            ).inspect()
        except Exception:
            return self._unknown(
                PostureName.SENSITIVE_ENCRYPTION,
                observed_at,
            )
        safe_state: dict[str, object] = {}
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(
                        SensitiveMigrationState.schema_version,
                        SensitiveMigrationState.state,
                        SensitiveMigrationState.rows_total,
                        SensitiveMigrationState.rows_completed,
                        SensitiveMigrationState.started_at,
                        SensitiveMigrationState.completed_at,
                        SensitiveMigrationState.updated_at,
                    )
                ).one_or_none()
            if (
                row is not None
                and type(row.schema_version) is int
                and row.schema_version > 0
                and row.state
                in {
                    "required",
                    "migrating",
                    "complete",
                    "rotating",
                    "failed",
                }
                and type(row.rows_total) is int
                and row.rows_total >= 0
                and type(row.rows_completed) is int
                and 0 <= row.rows_completed <= row.rows_total
            ):
                safe_state = {
                    "migration_state": row.state,
                    "schema_version": row.schema_version,
                    "rows_total": row.rows_total,
                    "rows_completed": row.rows_completed,
                    "started_at": (
                        _as_utc(row.started_at)
                        if row.started_at is not None
                        else None
                    ),
                    "completed_at": (
                        _as_utc(row.completed_at)
                        if row.completed_at is not None
                        else None
                    ),
                    "updated_at": _as_utc(row.updated_at),
                }
        except Exception:
            return self._unknown(
                PostureName.SENSITIVE_ENCRYPTION,
                observed_at,
            )
        return self._check(
            PostureName.SENSITIVE_ENCRYPTION,
            (
                PostureStatus.PASS
                if result.passed
                else PostureStatus.BLOCKED
            ),
            observed_at,
            result.code,
            **safe_state,
        )

    def _request_budget_checks(
        self,
        observed_at: datetime,
        principal: str,
    ) -> list[PostureCheck]:
        checks: list[PostureCheck] = []
        for scope, configured in self._config.security.rate_limits:
            if self._rate_limiter is None:
                checks.append(
                    self._unknown(
                        PostureName.REQUEST_BUDGET,
                        observed_at,
                        scope=scope,
                        detail=(
                            PostureDetailCode.BUDGET_EVIDENCE_UNAVAILABLE
                        ),
                    )
                )
                continue
            spec = LimitSpec(
                name=scope,
                principal_requests=configured.requests,
                global_requests=configured.global_requests,
                window_seconds=configured.window_seconds,
                principal_daily_requests=configured.daily_requests,
                global_daily_requests=configured.global_daily_requests,
            )
            try:
                inspected = self._rate_limiter.inspect_pair(
                    spec,
                    principal=principal,
                    now=observed_at,
                )
            except (LimitStoreUnavailable, OSError):
                checks.append(
                    self._unknown(
                        PostureName.REQUEST_BUDGET,
                        observed_at,
                        scope=scope,
                        detail=(
                            PostureDetailCode.BUDGET_EVIDENCE_UNAVAILABLE
                        ),
                    )
                )
                continue
            checks.append(
                self._check(
                    PostureName.REQUEST_BUDGET,
                    (
                        PostureStatus.BLOCKED
                        if inspected.exhausted
                        else PostureStatus.PASS
                    ),
                    observed_at,
                    (
                        PostureDetailCode.REQUEST_BUDGET_EXHAUSTED
                        if inspected.exhausted
                        else PostureDetailCode.REQUEST_BUDGET_AVAILABLE
                    ),
                    scope=scope,
                    budget_used=max(
                        0,
                        inspected.limit - inspected.remaining,
                    ),
                    budget_remaining=inspected.remaining,
                    budget_limit=inspected.limit,
                    reset_at=inspected.reset_at,
                )
            )
        return checks

    def _provider_budget_checks(
        self,
        observed_at: datetime,
    ) -> list[PostureCheck]:
        checks: list[PostureCheck] = []
        for provider in ("anthropic", "gemini", "groq"):
            if self._provider_budget is None:
                checks.append(
                    self._unknown(
                        PostureName.PROVIDER_BUDGET,
                        observed_at,
                        scope=provider,
                        detail=(
                            PostureDetailCode.BUDGET_EVIDENCE_UNAVAILABLE
                        ),
                    )
                )
                continue
            try:
                inspected = self._provider_budget.inspect(
                    provider,
                    now=observed_at,
                )
            except (ProviderBudgetUnavailable, OSError):
                checks.append(
                    self._unknown(
                        PostureName.PROVIDER_BUDGET,
                        observed_at,
                        scope=provider,
                        detail=(
                            PostureDetailCode.BUDGET_EVIDENCE_UNAVAILABLE
                        ),
                    )
                )
                continue
            exhausted = (
                inspected.calls_remaining == 0
                or inspected.input_tokens_remaining == 0
                or inspected.output_tokens_remaining == 0
            )
            blocked = inspected.reconciliation_required or exhausted
            detail = (
                PostureDetailCode.PROVIDER_RECONCILIATION_REQUIRED
                if inspected.reconciliation_required
                else (
                    PostureDetailCode.PROVIDER_BUDGET_EXHAUSTED
                    if exhausted
                    else PostureDetailCode.PROVIDER_BUDGET_AVAILABLE
                )
            )
            checks.append(
                self._check(
                    PostureName.PROVIDER_BUDGET,
                    (
                        PostureStatus.BLOCKED
                        if blocked
                        else PostureStatus.PASS
                    ),
                    observed_at,
                    detail,
                    scope=provider,
                    budget_used=inspected.calls_used,
                    budget_remaining=inspected.calls_remaining,
                    budget_limit=inspected.calls_limit,
                    input_tokens_used=inspected.input_tokens_used,
                    input_tokens_remaining=(
                        inspected.input_tokens_remaining
                    ),
                    input_tokens_limit=inspected.input_tokens_limit,
                    output_tokens_used=inspected.output_tokens_used,
                    output_tokens_remaining=(
                        inspected.output_tokens_remaining
                    ),
                    output_tokens_limit=inspected.output_tokens_limit,
                    reset_at=inspected.reset_at,
                    count=(
                        inspected.expired_started_count
                        + inspected.expired_unknown_count
                    ),
                )
            )
        return checks

    def _database_checks(
        self,
        observed_at: datetime,
    ) -> tuple[bool, list[PostureCheck]]:
        try:
            with self._session_factory() as session:
                session.scalar(select(func.count()).select_from(Order))
                checks = [
                    *self._quarantine_checks(session, observed_at),
                    *self._breaker_checks(session, observed_at),
                    self._heartbeat_check(session, observed_at),
                    self._reconciliation_check(session, observed_at),
                    *self._tenure_checks(session, observed_at),
                    *self._unsafe_checks(session, observed_at),
                    self._interlock_check(session, observed_at),
                ]
            return True, checks
        except Exception:
            return False, self._unknown_database_checks(observed_at)

    def _quarantine_checks(
        self,
        session,
        observed_at: datetime,
    ) -> list[PostureCheck]:
        counts = dict(
            session.execute(
                select(
                    UntrustedIngestEvent.state,
                    func.count(UntrustedIngestEvent.id),
                )
                .group_by(UntrustedIngestEvent.state)
                .order_by(UntrustedIngestEvent.state)
            ).all()
        )
        return [
            self._check(
                PostureName.QUARANTINE,
                (
                    PostureStatus.PRESENT
                    if counts.get(state, 0)
                    else PostureStatus.CLEAR
                ),
                observed_at,
                (
                    PostureDetailCode.QUARANTINE_ITEMS_PRESENT
                    if counts.get(state, 0)
                    else PostureDetailCode.QUARANTINE_EMPTY
                ),
                scope=state,
                count=counts.get(state, 0),
            )
            for state in ("received", "summarized", "rejected", "failed")
        ]

    def _breaker_checks(
        self,
        session,
        observed_at: datetime,
    ) -> list[PostureCheck]:
        rows = session.execute(
            select(
                CircuitBreakerState.scope_key,
                CircuitBreakerState.kind,
                CircuitBreakerState.target,
                CircuitBreakerState.tripped,
                CircuitBreakerState.generation,
                CircuitBreakerState.updated_at,
            ).order_by(CircuitBreakerState.scope_key)
        ).all()
        checks: list[PostureCheck] = []
        for row in rows:
            scope = BreakerScope.parse(row.scope_key)
            if (
                scope.kind.value != row.kind
                or scope.target != row.target
                or type(row.tripped) is not bool
                or type(row.generation) is not int
                or row.generation < 0
            ):
                raise ValueError("invalid breaker evidence")
            checks.append(
                self._check(
                    PostureName.CIRCUIT_BREAKER,
                    (
                        PostureStatus.TRIPPED
                        if row.tripped
                        else PostureStatus.CLEAR
                    ),
                    observed_at,
                    (
                        PostureDetailCode.BREAKER_TRIPPED
                        if row.tripped
                        else PostureDetailCode.BREAKER_CLEAR
                    ),
                    scope=row.scope_key,
                    generation=row.generation,
                    updated_at=_as_utc(row.updated_at),
                )
            )
        if not checks:
            checks.append(
                self._check(
                    PostureName.CIRCUIT_BREAKER,
                    PostureStatus.CLEAR,
                    observed_at,
                    PostureDetailCode.BREAKER_CLEAR,
                    count=0,
                )
            )
        return checks

    def _heartbeat_check(
        self,
        session,
        observed_at: datetime,
    ) -> PostureCheck:
        heartbeat_at = session.scalar(
            select(Heartbeat.at)
            .where(Heartbeat.source == "daemon")
            .order_by(Heartbeat.at.desc(), Heartbeat.id.desc())
            .limit(1)
        )
        if heartbeat_at is None:
            return self._check(
                PostureName.DAEMON_HEARTBEAT,
                PostureStatus.UNKNOWN,
                observed_at,
                PostureDetailCode.DAEMON_HEARTBEAT_MISSING,
            )
        heartbeat_at = _as_utc(heartbeat_at)
        if heartbeat_at > observed_at:
            raise ValueError("invalid heartbeat evidence")
        age = (observed_at - heartbeat_at).total_seconds()
        stale_after = self._config.daemon.heartbeat_stale_seconds
        stale = age > stale_after
        return self._check(
            PostureName.DAEMON_HEARTBEAT,
            PostureStatus.STALE if stale else PostureStatus.FRESH,
            observed_at,
            (
                PostureDetailCode.DAEMON_HEARTBEAT_STALE
                if stale
                else PostureDetailCode.DAEMON_HEARTBEAT_FRESH
            ),
            evidence_at=heartbeat_at,
            age_seconds=age,
            max_age_seconds=stale_after,
        )

    def _reconciliation_check(
        self,
        session,
        observed_at: datetime,
    ) -> PostureCheck:
        if not self._reconciliation_enabled:
            return self._check(
                PostureName.STARTUP_RECONCILIATION,
                PostureStatus.UNKNOWN,
                observed_at,
                PostureDetailCode.RECONCILIATION_EVIDENCE_UNAVAILABLE,
            )
        row = session.execute(
            select(
                StartupReconciliationState.generation,
                StartupReconciliationState.completed_generation,
                StartupReconciliationState.status,
                StartupReconciliationState.started_at,
                StartupReconciliationState.completed_at,
                StartupReconciliationState.updated_at,
            ).where(
                StartupReconciliationState.broker
                == self._reconciliation_key
            )
        ).one_or_none()
        if row is None:
            return self._check(
                PostureName.STARTUP_RECONCILIATION,
                PostureStatus.BLOCKED,
                observed_at,
                PostureDetailCode.RECONCILIATION_REQUIRED,
                generation=0,
                completed_generation=0,
            )
        if (
            type(row.generation) is not int
            or row.generation < 0
            or type(row.completed_generation) is not int
            or row.completed_generation < 0
            or row.completed_generation > row.generation
            or row.status not in {"required", "current", "failed"}
        ):
            raise ValueError("invalid reconciliation evidence")
        started_at = _as_utc(row.started_at)
        completed_at = (
            _as_utc(row.completed_at)
            if row.completed_at is not None
            else None
        )
        updated_at = _as_utc(row.updated_at)
        common = {
            "generation": row.generation,
            "completed_generation": row.completed_generation,
            "started_at": started_at,
            "completed_at": completed_at,
            "updated_at": updated_at,
        }
        if row.status == "required":
            return self._check(
                PostureName.STARTUP_RECONCILIATION,
                PostureStatus.BLOCKED,
                observed_at,
                PostureDetailCode.RECONCILIATION_REQUIRED,
                **common,
            )
        if row.status == "failed":
            return self._check(
                PostureName.STARTUP_RECONCILIATION,
                PostureStatus.BLOCKED,
                observed_at,
                PostureDetailCode.RECONCILIATION_FAILED,
                **common,
            )
        if (
            completed_at is None
            or row.completed_generation != row.generation
            or completed_at > observed_at
        ):
            raise ValueError("invalid reconciliation evidence")
        age = (observed_at - completed_at).total_seconds()
        max_age = self._config.trading.reconciliation_max_age_seconds
        stale = age > max_age
        return self._check(
            PostureName.STARTUP_RECONCILIATION,
            PostureStatus.STALE if stale else PostureStatus.PASS,
            observed_at,
            (
                PostureDetailCode.RECONCILIATION_STALE
                if stale
                else PostureDetailCode.RECONCILIATION_CURRENT
            ),
            age_seconds=age,
            max_age_seconds=max_age,
            **common,
        )

    def _tenure_checks(
        self,
        session,
        observed_at: datetime,
    ) -> list[PostureCheck]:
        rows = session.execute(
            select(
                RuntimeTenure.role,
                RuntimeTenure.state,
                RuntimeTenure.generation,
                RuntimeTenure.acquired_at,
                RuntimeTenure.renewed_at,
                RuntimeTenure.expires_at,
                RuntimeTenure.released_at,
            ).order_by(RuntimeTenure.role)
        ).all()
        if not rows:
            return [
                self._check(
                    PostureName.RUNTIME_TENURE,
                    PostureStatus.UNKNOWN,
                    observed_at,
                    PostureDetailCode.RUNTIME_TENURE_MISSING,
                )
            ]
        checks: list[PostureCheck] = []
        for row in rows:
            if (
                row.role
                not in {
                    "app",
                    "daemon",
                    "mcp",
                    "validation",
                    "maintenance",
                }
                or row.state not in {"held", "released", "fenced"}
                or type(row.generation) is not int
                or row.generation <= 0
            ):
                raise ValueError("invalid tenure evidence")
            expires_at = _as_utc(row.expires_at)
            stale = row.state == "held" and expires_at <= observed_at
            if stale:
                status = PostureStatus.STALE
                detail = PostureDetailCode.RUNTIME_TENURE_STALE
            else:
                status = PostureStatus(row.state)
                detail = {
                    "held": PostureDetailCode.RUNTIME_TENURE_HELD,
                    "released": PostureDetailCode.RUNTIME_TENURE_RELEASED,
                    "fenced": PostureDetailCode.RUNTIME_TENURE_FENCED,
                }[row.state]
            checks.append(
                self._check(
                    PostureName.RUNTIME_TENURE,
                    status,
                    observed_at,
                    detail,
                    scope=row.role,
                    generation=row.generation,
                    started_at=_as_utc(row.acquired_at),
                    updated_at=_as_utc(row.renewed_at),
                    expires_at=expires_at,
                    completed_at=(
                        _as_utc(row.released_at)
                        if row.released_at is not None
                        else None
                    ),
                )
            )
        return checks

    def _unsafe_checks(
        self,
        session,
        observed_at: datetime,
    ) -> list[PostureCheck]:
        order_count = session.scalar(
            select(func.count(Order.id)).where(
                or_(
                    Order.status.in_(_LIVE_OR_UNKNOWN_ORDER_STATUSES),
                    Order.acceptance_state
                    == "fill_reconcile_required",
                    Order.last_error_code.in_(
                        _SAFETY_LATCH_ERROR_CODES
                    ),
                )
            )
        )
        fill_count = session.scalar(
            select(func.count(Fill.id)).where(
                or_(
                    Fill.order_id.is_(None),
                    Fill.reconciliation_state
                    == FILL_RECONCILIATION_QUARANTINED,
                    (
                        Fill.reconciliation_state
                        != FILL_RECONCILIATION_SUPERSEDED
                    )
                    & or_(
                        Fill.broker_fill_id.is_(None),
                        func.trim(Fill.broker_fill_id) == "",
                    ),
                )
            )
        )
        rule_count = session.scalar(
            select(func.count(Rule.id)).where(
                Rule.state.in_(("pending", "active", "processing"))
            )
        )
        group_count = session.scalar(
            select(func.count(RuleGroup.id)).where(
                or_(
                    RuleGroup.state.in_(("pending", "active")),
                    RuleGroup.reconciliation_required.is_(True),
                )
            )
        )
        return [
            self._count_check(
                PostureName.UNSAFE_ORDERS,
                observed_at,
                int(order_count or 0),
            ),
            self._count_check(
                PostureName.UNSAFE_FILLS,
                observed_at,
                int(fill_count or 0),
            ),
            self._count_check(
                PostureName.UNSAFE_RULES,
                observed_at,
                int(rule_count or 0),
                scope="rules",
            ),
            self._count_check(
                PostureName.UNSAFE_RULES,
                observed_at,
                int(group_count or 0),
                scope="rule_groups",
            ),
        ]

    def _interlock_check(
        self,
        session,
        observed_at: datetime,
    ) -> PostureCheck:
        count = int(
            session.scalar(
                select(func.count(MutationInterlock.resource_key)).where(
                    MutationInterlock.state == "uncertain"
                )
            )
            or 0
        )
        return self._check(
            PostureName.UNCERTAIN_INTERLOCKS,
            (
                PostureStatus.PRESENT
                if count
                else PostureStatus.CLEAR
            ),
            observed_at,
            (
                PostureDetailCode.UNCERTAIN_INTERLOCKS_PRESENT
                if count
                else PostureDetailCode.UNCERTAIN_INTERLOCKS_CLEAR
            ),
            count=count,
        )

    @classmethod
    def _count_check(
        cls,
        name: PostureName,
        observed_at: datetime,
        count: int,
        *,
        scope: str | None = None,
    ) -> PostureCheck:
        return cls._check(
            name,
            (
                PostureStatus.PRESENT
                if count
                else PostureStatus.CLEAR
            ),
            observed_at,
            (
                PostureDetailCode.UNSAFE_STATE_PRESENT
                if count
                else PostureDetailCode.UNSAFE_STATE_CLEAR
            ),
            scope=scope,
            count=count,
        )

    @classmethod
    def _unknown(
        cls,
        name: PostureName,
        observed_at: datetime,
        *,
        scope: str | None = None,
        detail: PostureDetailCode = (
            PostureDetailCode.DATABASE_EVIDENCE_UNAVAILABLE
        ),
    ) -> PostureCheck:
        return cls._check(
            name,
            PostureStatus.UNKNOWN,
            observed_at,
            detail,
            scope=scope,
        )

    def _unknown_database_checks(
        self,
        observed_at: datetime,
    ) -> list[PostureCheck]:
        checks = [
            self._unknown(
                PostureName.QUARANTINE,
                observed_at,
                scope=state,
            )
            for state in ("received", "summarized", "rejected", "failed")
        ]
        checks.extend(
            [
                self._unknown(
                    PostureName.CIRCUIT_BREAKER,
                    observed_at,
                ),
                self._unknown(
                    PostureName.DAEMON_HEARTBEAT,
                    observed_at,
                ),
                self._unknown(
                    PostureName.STARTUP_RECONCILIATION,
                    observed_at,
                ),
                self._unknown(
                    PostureName.RUNTIME_TENURE,
                    observed_at,
                ),
                self._unknown(
                    PostureName.UNSAFE_ORDERS,
                    observed_at,
                ),
                self._unknown(
                    PostureName.UNSAFE_FILLS,
                    observed_at,
                ),
                self._unknown(
                    PostureName.UNSAFE_RULES,
                    observed_at,
                    scope="rules",
                ),
                self._unknown(
                    PostureName.UNSAFE_RULES,
                    observed_at,
                    scope="rule_groups",
                ),
                self._unknown(
                    PostureName.UNCERTAIN_INTERLOCKS,
                    observed_at,
                ),
            ]
        )
        return checks

    def report(self, *, limit_principal: str) -> SecurityPostureReport:
        observed_at = _as_utc(self._clock())
        checks = self._config_checks(observed_at)
        database_available, database_checks = self._database_checks(
            observed_at
        )
        checks.append(
            self._encryption_check(
                observed_at,
                database_available=database_available,
            )
        )
        checks.extend(
            self._request_budget_checks(observed_at, limit_principal)
        )
        checks.extend(self._provider_budget_checks(observed_at))
        checks.extend(database_checks)
        return SecurityPostureReport(
            observed_at=observed_at,
            checks=tuple(checks),
        )
