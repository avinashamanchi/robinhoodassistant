"""Typed runtime secrets and verified providers for production roles."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import json
import platform
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
)

if TYPE_CHECKING:
    from ..config import AppConfig, EncryptionConfig


KEYCHAIN_SERVICE = "io.local.trading-assistant"
_SIMPLE_SECRET_FIELDS = (
    "anthropic_api_key",
    "gemini_api_key",
    "groq_api_key",
    "openrouter_api_key",
    "marketstack_api_key",
    "app_api_token",
    "alpaca_api_key",
    "alpaca_secret_key",
    "database_url",
    "telegram_bot_token",
    "telegram_chat_id",
    "candidate_signing_key",
    "backup_encryption_key",
    "live_trading_confirm",
)
_PRODUCTION_ROLES = frozenset(
    {
        "app",
        "backup",
        "daemon",
        "mcp",
        "migration",
        "paper-drill",
        "preflight",
        "safety-drill",
        "validate-analyst",
        "watchdog",
    }
)
_DEVELOPMENT_ENVIRONMENT_ROLES = frozenset(
    {"migration", "safety-drill"}
)
_ROLE_REQUIRED_FIELDS = {
    "app": (
        "app_api_token",
        "alpaca_api_key",
        "alpaca_secret_key",
        "database_url",
    ),
    "backup": ("database_url",),
    "daemon": (
        "app_api_token",
        "alpaca_api_key",
        "alpaca_secret_key",
        "database_url",
    ),
    "mcp": (
        "app_api_token",
        "alpaca_api_key",
        "alpaca_secret_key",
        "database_url",
    ),
    "migration": ("database_url",),
    "paper-drill": (
        "app_api_token",
        "alpaca_api_key",
        "alpaca_secret_key",
        "database_url",
    ),
    "preflight": (
        "app_api_token",
        "alpaca_api_key",
        "alpaca_secret_key",
        "database_url",
    ),
    "safety-drill": ("database_url",),
    "validate-analyst": ("database_url",),
    "watchdog": ("database_url",),
}
_LLM_ROLES = frozenset(
    {"app", "daemon", "preflight", "validate-analyst"}
)
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}")
_KNOWN_EXAMPLE_KEY_MATERIAL = (
    bytes(32),
    b"A" * 32,
    b"0" * 32,
    bytes(range(32)),
)
_KNOWN_EXAMPLE_KEYS = frozenset(
    {
        base64.b64encode(material).decode()
        for material in _KNOWN_EXAMPLE_KEY_MATERIAL
    }
)
_ENVIRONMENT_SECRET_NAMES = frozenset(
    {
        *(field_name.upper() for field_name in _SIMPLE_SECRET_FIELDS),
        "FIELD_ENCRYPTION_KEYS_JSON",
    }
)
_PLACEHOLDER_MARKERS = (
    "changeme",
    "example",
    "placeholder",
    "replace-me",
    "replace_me",
    "test-token",
    "your-token",
    "your_token",
)


class RuntimeSecrets(BaseModel):
    """Secret values after an explicit provider has loaded and validated them."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    anthropic_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    marketstack_api_key: SecretStr = SecretStr("")
    app_api_token: SecretStr = SecretStr("")
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_secret_key: SecretStr = SecretStr("")
    database_url: SecretStr = SecretStr("sqlite:///./trading_assistant.db")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: SecretStr = SecretStr("")
    candidate_signing_key: SecretStr = SecretStr("")
    field_encryption_keys: Mapping[str, SecretStr] = Field(default_factory=dict)
    backup_encryption_key: SecretStr = SecretStr("")
    live_trading_confirm: SecretStr = SecretStr("")

    @field_validator("field_encryption_keys")
    @classmethod
    def _freeze_field_encryption_keys(
        cls, value: Mapping[str, SecretStr]
    ) -> Mapping[str, SecretStr]:
        return MappingProxyType(dict(value))

    @field_serializer("field_encryption_keys")
    def _serialize_field_encryption_keys(
        self, value: Mapping[str, SecretStr]
    ) -> dict[str, SecretStr]:
        return dict(value)


@runtime_checkable
class SecretProvider(Protocol):
    provider_name: str

    def load(self, *, encryption: EncryptionConfig) -> RuntimeSecrets: ...


@runtime_checkable
class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(
        self,
        service: str,
        username: str,
        password: str,
    ) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class SecretBoundaryError(RuntimeError):
    """Base class for stable, secret-free startup failures."""


class UnsafeKeyringBackend(SecretBoundaryError):
    """The selected keyring is not the native macOS Keychain backend."""


class UnsafeSecretProvider(SecretBoundaryError):
    """A production role attempted to use a development provider."""


class SecretUnavailable(SecretBoundaryError):
    """A provider could not retrieve one named secret."""

    def __init__(self, field_name: str, stable_code: str) -> None:
        self.field_name = field_name
        self.stable_code = stable_code
        super().__init__(
            f"secret unavailable field={field_name} code={stable_code}"
        )


class SecretValidationError(SecretBoundaryError):
    """Loaded secret material failed a stable validation rule."""

    def __init__(
        self,
        field_name: str,
        stable_code: str,
        message: str | None = None,
    ) -> None:
        self.field_name = field_name
        self.stable_code = stable_code
        super().__init__(
            message
            or f"secret validation failed field={field_name} code={stable_code}"
        )


def secret_value(value: str | SecretStr) -> str:
    """Reveal a value only at an explicit trusted consumer boundary."""
    return (
        value.get_secret_value()
        if isinstance(value, SecretStr)
        else value
    )


def secret_is_set(value: str | SecretStr) -> bool:
    """Return whether a secret contains non-whitespace text."""
    return bool(secret_value(value).strip())


def secrets_match(
    left: str | SecretStr,
    right: str | SecretStr,
) -> bool:
    """Compare two secret-bearing values without a normal equality shortcut."""
    return hmac.compare_digest(secret_value(left), secret_value(right))


def validate_key_id(key_id: str) -> str:
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise SecretValidationError(
            "encryption_key_id",
            "invalid_key_id",
            "encryption key ID is invalid",
        )
    return key_id


def _configured_key_ids(encryption: EncryptionConfig) -> tuple[str, ...]:
    ids = (
        validate_key_id(encryption.active_key_id),
        *(
            validate_key_id(key_id)
            for key_id in encryption.retained_key_ids
        ),
    )
    if len(ids) != len(set(ids)):
        raise SecretValidationError(
            "field_encryption_keys",
            "duplicate_key_id",
            "field encryption key IDs must be distinct",
        )
    return ids


def validate_base64_key(
    field_name: str,
    value: str | SecretStr,
    *,
    reject_known_examples: bool = True,
) -> bytearray:
    """Decode one exact 32-byte Base64 key into mutable validation storage."""
    encoded = secret_value(value)
    try:
        decoded = bytearray(
            base64.b64decode(
                encoded.encode("ascii"),
                validate=True,
            )
        )
    except (UnicodeEncodeError, ValueError, binascii.Error):
        raise SecretValidationError(
            field_name,
            "invalid_base64",
            f"{field_name} must be a 32-byte Base64 key",
        ) from None
    canonical = base64.b64encode(bytes(decoded)).decode("ascii")
    if not hmac.compare_digest(encoded, canonical):
        for index in range(len(decoded)):
            decoded[index] = 0
        raise SecretValidationError(
            field_name,
            "invalid_base64",
            f"{field_name} must use canonical Base64 encoding",
        )
    if len(decoded) != 32:
        for index in range(len(decoded)):
            decoded[index] = 0
        raise SecretValidationError(
            field_name,
            "invalid_length",
            f"{field_name} must be a 32-byte Base64 key",
        )
    if reject_known_examples and (
        encoded in _KNOWN_EXAMPLE_KEYS
        or any(
            hmac.compare_digest(decoded, known)
            for known in _KNOWN_EXAMPLE_KEY_MATERIAL
        )
    ):
        for index in range(len(decoded)):
            decoded[index] = 0
        raise SecretValidationError(
            field_name,
            "known_example",
            f"{field_name} must not use a known example value",
        )
    return decoded


def _default_keyring_backend() -> KeyringBackend:
    if platform.system() != "Darwin":
        raise UnsafeKeyringBackend(
            "production secrets require the native macOS Keychain backend"
        )
    try:
        import keyring
        from keyring.backends.macOS import Keyring as MacOSKeyring

        backend = keyring.get_keyring()
    except Exception:
        raise UnsafeKeyringBackend(
            "production secrets require the native macOS Keychain backend"
        ) from None
    if type(backend) is not MacOSKeyring:
        raise UnsafeKeyringBackend(
            "production secrets require the native macOS Keychain backend"
        )
    return backend


class MacOSKeychainSecretProvider:
    """Load typed secrets from generic-password accounts in macOS Keychain."""

    provider_name = "macos-keychain"

    def __init__(self, *, backend: KeyringBackend | None = None) -> None:
        self.backend = (
            backend
            if backend is not None
            else _default_keyring_backend()
        )
        self.last_successful_role_load_at: datetime | None = None

    def _get(self, account: str) -> str | None:
        try:
            return self.backend.get_password(KEYCHAIN_SERVICE, account)
        except Exception:
            raise SecretUnavailable(
                account,
                "keyring_read_failed",
            ) from None

    def load(self, *, encryption: EncryptionConfig) -> RuntimeSecrets:
        values = {
            field_name: self._get(field_name) or ""
            for field_name in _SIMPLE_SECRET_FIELDS
        }
        field_keys: dict[str, str] = {}
        for key_id in _configured_key_ids(encryption):
            account = f"field-encryption/{key_id}"
            value = self._get(account)
            if value is None or not value:
                raise SecretUnavailable(account, "missing")
            field_keys[key_id] = value
        loaded = RuntimeSecrets(
            **values,
            field_encryption_keys=field_keys,
        )
        return loaded

    def read_presence(
        self,
        *,
        encryption: EncryptionConfig,
    ) -> Mapping[str, bool | None]:
        presence: dict[str, bool | None] = {}
        for account in (
            *_SIMPLE_SECRET_FIELDS,
            *(
                f"field-encryption/{key_id}"
                for key_id in _configured_key_ids(encryption)
            ),
        ):
            try:
                value = self.backend.get_password(
                    KEYCHAIN_SERVICE,
                    account,
                )
            except Exception:
                presence[account] = None
            else:
                presence[account] = bool(value)
        return MappingProxyType(presence)


def _parse_environment_field_keys(
    raw_field_keys: str,
    *,
    encryption: EncryptionConfig,
    allow_missing: bool = False,
) -> dict[str, str]:
    try:
        parsed = (
            json.loads(raw_field_keys)
            if raw_field_keys
            else {}
        )
    except (TypeError, ValueError):
        raise SecretUnavailable(
            "field_encryption_keys",
            "invalid_json",
        ) from None
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in parsed.items()
    ):
        raise SecretUnavailable(
            "field_encryption_keys",
            "invalid_json",
        )

    expected_ids = _configured_key_ids(encryption)
    try:
        if (
            any(
                key.upper().startswith("COMPOSIO_")
                for key in parsed
            )
            or set(parsed).difference(expected_ids)
        ):
            raise SecretValidationError(
                "field_encryption_keys",
                "unexpected_key_id",
                "field encryption key JSON contains an unexpected key ID",
            )
        field_keys: dict[str, str] = {}
        for key_id in expected_ids:
            value = parsed.get(key_id)
            if not value:
                if allow_missing:
                    continue
                raise SecretUnavailable(
                    f"field-encryption/{key_id}",
                    "missing",
                )
            field_keys[key_id] = value
        return field_keys
    finally:
        parsed.clear()


class EnvironmentSecretProvider:
    """Parse an explicitly injected development/test environment mapping."""

    provider_name = "environment-development"

    def __init__(
        self,
        *,
        environ: Mapping[str, str],
        encryption: EncryptionConfig,
    ) -> None:
        if not isinstance(environ, Mapping):
            raise TypeError("environ must be an explicitly injected mapping")
        self._configured_key_ids = _configured_key_ids(encryption)
        self._loaded = RuntimeSecrets(
            **{
                field_name: environ.get(field_name.upper(), "")
                for field_name in _SIMPLE_SECRET_FIELDS
            },
            field_encryption_keys=_parse_environment_field_keys(
                environ.get("FIELD_ENCRYPTION_KEYS_JSON", ""),
                encryption=encryption,
            ),
        )
        self.last_successful_role_load_at: datetime | None = None

    def load(self, *, encryption: EncryptionConfig) -> RuntimeSecrets:
        if _configured_key_ids(encryption) != self._configured_key_ids:
            raise SecretValidationError(
                "field_encryption_keys",
                "configuration_mismatch",
                "environment provider encryption configuration changed",
            )
        return self._loaded


def app_secret_quality_ok(value: str | SecretStr) -> bool:
    token = secret_value(value).strip()
    lowered = token.lower()
    periodic = any(
        len(token) % period == 0
        and token == token[:period] * (len(token) // period)
        for period in range(1, min(16, len(token) // 2) + 1)
    )
    return (
        len(token) >= 32
        and len(set(token)) >= 8
        and not any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
        and not periodic
    )


def _selected_llm_secret_field(config: AppConfig) -> str:
    field_name = {
        "anthropic": "anthropic_api_key",
        "gemini": "gemini_api_key",
        "groq": "groq_api_key",
    }.get(config.llm.provider)
    if field_name is None:
        raise SecretValidationError(
            "llm.provider",
            "unsupported_provider",
            "configured LLM provider is unsupported",
        )
    return field_name


def _required_fields(role: str, config: AppConfig) -> tuple[str, ...]:
    fields = list(_ROLE_REQUIRED_FIELDS[role])
    if role in _LLM_ROLES:
        fields.append(_selected_llm_secret_field(config))
    if config.features.telegram_notifications and role in {
        "app",
        "daemon",
        "preflight",
    }:
        fields.extend(("telegram_bot_token", "telegram_chat_id"))
    return tuple(fields)


def _validate_required_fields(
    role: str,
    config: AppConfig,
    secrets: RuntimeSecrets,
) -> None:
    for field_name in _required_fields(role, config):
        if not secret_is_set(getattr(secrets, field_name)):
            raise SecretValidationError(
                field_name,
                "required",
                f"{field_name} is required for role={role}",
            )
    if "app_api_token" in _required_fields(role, config):
        if not app_secret_quality_ok(secrets.app_api_token):
            raise SecretValidationError(
                "app_api_token",
                "weak_operator_secret",
                "app_api_token fails operator secret quality requirements",
            )


def _construct_key_service_probes(
    candidate_key: bytearray,
    backup_key: bytearray,
    field_keys: list[bytearray],
) -> None:
    """Construct keyed service primitives before composition is allowed."""
    hmac.new(
        candidate_key,
        b"candidate-signer-startup-probe",
        hashlib.sha256,
    ).digest()
    for purpose, key in (
        (b"backup-cipher-startup-probe", backup_key),
        *(
            (b"field-cipher-startup-probe", field_key)
            for field_key in field_keys
        ),
    ):
        hmac.new(key, purpose, hashlib.sha256).digest()


def _validate_key_material(
    config: AppConfig,
    secrets: RuntimeSecrets,
) -> None:
    buffers: list[bytearray] = []
    try:
        candidate = validate_base64_key(
            "candidate_signing_key",
            secrets.candidate_signing_key,
        )
        buffers.append(candidate)
        backup = validate_base64_key(
            "backup_encryption_key",
            secrets.backup_encryption_key,
        )
        buffers.append(backup)
        field_buffers: list[bytearray] = []
        expected_ids = _configured_key_ids(config.encryption)
        if tuple(secrets.field_encryption_keys) != expected_ids:
            raise SecretValidationError(
                "field_encryption_keys",
                "key_id_mismatch",
                "field encryption keys must match active and retained IDs",
            )
        for key_id in expected_ids:
            field_buffer = validate_base64_key(
                f"field-encryption/{key_id}",
                secrets.field_encryption_keys[key_id],
            )
            buffers.append(field_buffer)
            field_buffers.append(field_buffer)
        for left_index, left in enumerate(buffers):
            for right in buffers[left_index + 1 :]:
                if hmac.compare_digest(left, right):
                    raise SecretValidationError(
                        "key_material",
                        "shared_key_material",
                        "candidate, backup, and field keys must be distinct",
                    )
        _construct_key_service_probes(
            candidate,
            backup,
            field_buffers,
        )
    finally:
        for buffer in buffers:
            for index in range(len(buffer)):
                buffer[index] = 0


def load_role_secrets(
    role: str,
    *,
    config: AppConfig,
    provider: SecretProvider | None = None,
    runtime_secrets: RuntimeSecrets | None = None,
    allow_environment: bool = False,
) -> RuntimeSecrets:
    """Load and validate exactly once for one trusted composition root."""
    if role not in _PRODUCTION_ROLES:
        raise ValueError("runtime role is invalid")
    if runtime_secrets is not None:
        from ..logging import register_all_secrets

        register_all_secrets(runtime_secrets)
        return runtime_secrets

    selected = (
        provider
        if provider is not None
        else MacOSKeychainSecretProvider()
    )
    if not isinstance(selected, MacOSKeychainSecretProvider):
        if not (
            allow_environment
            and role in _DEVELOPMENT_ENVIRONMENT_ROLES
            and isinstance(selected, EnvironmentSecretProvider)
        ):
            raise UnsafeSecretProvider(
                f"production role={role} requires macOS Keychain"
            )
    loaded = selected.load(encryption=config.encryption)

    from ..logging import register_all_secrets

    register_all_secrets(loaded)
    _validate_required_fields(role, config, loaded)
    _validate_key_material(config, loaded)
    if hasattr(selected, "last_successful_role_load_at"):
        selected.last_successful_role_load_at = datetime.now(timezone.utc)
    return loaded
