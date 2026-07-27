"""Runtime secrets are typed, immutable, and supplied by a provider."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import logging
from pathlib import Path
import subprocess

import pytest
from pydantic import ValidationError

from trading_assistant.config import EncryptionConfig
from trading_assistant.ops import secrets as secret_ops
from trading_assistant.security.secrets import (
    EnvironmentSecretProvider,
    MacOSKeychainSecretProvider,
    RuntimeSecrets,
    SecretProvider,
    SecretUnavailable,
    SecretValidationError,
    UnsafeKeyringBackend,
    UnsafeSecretProvider,
    load_role_secrets,
)


_SIMPLE_ACCOUNTS = (
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
_PRODUCTION_ROLES = (
    "app",
    "daemon",
    "mcp",
    "preflight",
    "migration",
    "watchdog",
    "paper-drill",
    "safety-drill",
)
_SERVICE = "io.local.trading-assistant"


def _key(label: str) -> str:
    return base64.b64encode(hashlib.sha256(label.encode()).digest()).decode()


def _encryption_config(*, retained: tuple[str, ...] = ()) -> EncryptionConfig:
    return EncryptionConfig(
        active_key_id="local-primary-2026-07",
        retained_key_ids=list(retained),
    )


def _account_values(
    encryption: EncryptionConfig | None = None,
) -> dict[str, str]:
    encryption = encryption or _encryption_config()
    values = {
        "anthropic_api_key": "anthropic-test-key",
        "gemini_api_key": "gemini-test-key",
        "groq_api_key": "groq-test-key",
        "openrouter_api_key": "",
        "marketstack_api_key": "marketstack-test-key",
        "app_api_token": "A7v!9qL2#mN4$pR6&tU8*wX0-zB3_cD5",
        "alpaca_api_key": "paper-key",
        "alpaca_secret_key": "paper-secret",
        "database_url": "sqlite:///test-role.db",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "candidate_signing_key": _key("candidate-signing"),
        "backup_encryption_key": _key("backup-encryption"),
        "live_trading_confirm": "",
    }
    for key_id in (
        encryption.active_key_id,
        *encryption.retained_key_ids,
    ):
        values[f"field-encryption/{key_id}"] = _key(
            f"field-encryption:{key_id}"
        )
    return values


def _environment(
    encryption: EncryptionConfig | None = None,
) -> dict[str, str]:
    encryption = encryption or _encryption_config()
    values = _account_values(encryption)
    environ = {
        account.upper(): value
        for account, value in values.items()
        if account in _SIMPLE_ACCOUNTS
    }
    environ["FIELD_ENCRYPTION_KEYS_JSON"] = json.dumps(
        {
            key_id: values[f"field-encryption/{key_id}"]
            for key_id in (
                encryption.active_key_id,
                *encryption.retained_key_ids,
            )
        }
    )
    return environ


class _FakeMacOSKeyring:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)
        self.get_calls: list[tuple[str, str]] = []
        self.set_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.failure: Exception | None = None

    def get_password(self, service: str, username: str) -> str | None:
        self.get_calls.append((service, username))
        if self.failure is not None:
            raise self.failure
        return self.values.get(username)

    def set_password(
        self,
        service: str,
        username: str,
        password: str,
    ) -> None:
        self.set_calls.append((service, username, password))
        if self.failure is not None:
            raise self.failure
        self.values[username] = password

    def delete_password(self, service: str, username: str) -> None:
        self.delete_calls.append((service, username))
        if self.failure is not None:
            raise self.failure
        self.values.pop(username, None)


class _StaticSecretProvider:
    provider_name = "test"

    def load(self, *, encryption: EncryptionConfig) -> RuntimeSecrets:
        return RuntimeSecrets()


def test_runtime_secrets_are_immutable_and_forbid_unknown_fields():
    runtime_secrets = RuntimeSecrets()

    with pytest.raises(ValidationError, match="frozen"):
        runtime_secrets.live_trading_confirm = ""  # type: ignore[misc]
    with pytest.raises(ValidationError, match="unexpected"):
        RuntimeSecrets(unexpected="value")


def test_runtime_secrets_freeze_nested_keys_and_defensively_copy_input():
    input_keys = {"local-primary-2026-07": "injected-test-key-material"}
    runtime_secrets = RuntimeSecrets(field_encryption_keys=input_keys)

    input_keys["local-primary-2026-07"] = "replacement-test-key-material"

    assert (
        runtime_secrets.field_encryption_keys[
            "local-primary-2026-07"
        ].get_secret_value()
        == "injected-test-key-material"
    )
    with pytest.raises(TypeError):
        runtime_secrets.field_encryption_keys["new-key"] = "new-value"
    with pytest.raises(ValidationError, match="field_encryption_keys"):
        RuntimeSecrets(field_encryption_keys={"bad-key": ["not-a-secret"]})
    assert runtime_secrets.model_dump(mode="json", warnings="error")[
        "field_encryption_keys"
    ] == {"local-primary-2026-07": "**********"}


def test_secret_provider_contract_is_runtime_checkable():
    provider = _StaticSecretProvider()
    protocol_signature = inspect.signature(SecretProvider.load)
    provider_signature = inspect.signature(provider.load)

    assert isinstance(provider, SecretProvider)
    assert tuple(protocol_signature.parameters) == ("self", "encryption")
    assert (
        protocol_signature.parameters["encryption"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )
    assert (
        provider_signature.parameters["encryption"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )
    assert provider.load(
        encryption=EncryptionConfig(active_key_id="test-key-1")
    ) == RuntimeSecrets()


def test_keychain_provider_reads_exact_accounts_without_subprocess_or_logging(
    caplog,
    monkeypatch,
):
    encryption = _encryption_config(retained=("local-retained-2026-06",))
    backend = _FakeMacOSKeyring(_account_values(encryption))

    def forbidden_subprocess(*_args, **_kwargs):
        raise AssertionError("Keychain access must not invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)
    caplog.set_level(logging.DEBUG)

    loaded = MacOSKeychainSecretProvider(backend=backend).load(
        encryption=encryption
    )

    assert loaded.alpaca_api_key.get_secret_value() == "paper-key"
    assert backend.get_calls == [
        *[(_SERVICE, account) for account in _SIMPLE_ACCOUNTS],
        (
            _SERVICE,
            f"field-encryption/{encryption.active_key_id}",
        ),
        (
            _SERVICE,
            "field-encryption/local-retained-2026-06",
        ),
    ]
    assert "paper-key" not in caplog.text
    assert "paper-secret" not in caplog.text


def test_keychain_provider_requires_every_configured_field_key():
    encryption = _encryption_config(retained=("local-retained-2026-06",))
    values = _account_values(encryption)
    values.pop("field-encryption/local-retained-2026-06")

    with pytest.raises(
        SecretUnavailable,
        match="field-encryption/local-retained-2026-06",
    ):
        MacOSKeychainSecretProvider(
            backend=_FakeMacOSKeyring(values)
        ).load(encryption=encryption)


def test_keychain_backend_failure_has_stable_secret_free_error():
    backend = _FakeMacOSKeyring(_account_values())
    marker = "provider exception containing secret-test-marker"
    backend.failure = RuntimeError(marker)

    with pytest.raises(SecretUnavailable) as captured:
        MacOSKeychainSecretProvider(backend=backend).load(
            encryption=_encryption_config()
        )

    assert captured.value.field_name == "anthropic_api_key"
    assert captured.value.stable_code == "keyring_read_failed"
    assert marker not in str(captured.value)
    assert marker not in repr(captured.value)


def test_default_keychain_provider_refuses_non_macos(monkeypatch):
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Linux")

    with pytest.raises(UnsafeKeyringBackend, match="macOS"):
        MacOSKeychainSecretProvider()


def test_default_keychain_provider_rejects_non_native_selected_backend(
    monkeypatch,
):
    import keyring
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(keyring, "get_keyring", object)

    with pytest.raises(UnsafeKeyringBackend, match="macOS"):
        MacOSKeychainSecretProvider()


def test_environment_provider_parses_only_the_injected_mapping():
    encryption = _encryption_config()
    provider = EnvironmentSecretProvider(
        environ=_environment(encryption)
    )

    loaded = provider.load(encryption=encryption)

    assert loaded.alpaca_api_key.get_secret_value() == "paper-key"
    assert (
        loaded.field_encryption_keys[
            encryption.active_key_id
        ].get_secret_value()
        == _key(f"field-encryption:{encryption.active_key_id}")
    )


@pytest.mark.parametrize("role", _PRODUCTION_ROLES)
def test_production_roles_reject_environment_provider(
    app_config,
    role,
):
    with pytest.raises(
        UnsafeSecretProvider,
        match="requires macOS Keychain",
    ):
        load_role_secrets(
            role,
            config=app_config,
            provider=EnvironmentSecretProvider(
                environ=_environment(app_config.encryption)
            ),
        )


@pytest.mark.parametrize("role", _PRODUCTION_ROLES)
def test_test_runtime_secret_injection_bypasses_all_external_providers(
    app_config,
    role,
):
    injected = RuntimeSecrets(app_api_token="injected")

    class _ForbiddenProvider:
        provider_name = "forbidden"

        def load(self, *, encryption):
            raise AssertionError("injected tests must not read a provider")

    assert (
        load_role_secrets(
            role,
            config=app_config,
            provider=_ForbiddenProvider(),
            runtime_secrets=injected,
        )
        is injected
    )


def test_valid_role_keys_are_distinct_32_byte_material(app_config):
    loaded = load_role_secrets(
        "app",
        config=app_config,
        provider=MacOSKeychainSecretProvider(
            backend=_FakeMacOSKeyring(
                _account_values(app_config.encryption)
            )
        ),
    )

    decoded = [
        base64.b64decode(loaded.candidate_signing_key.get_secret_value()),
        base64.b64decode(loaded.backup_encryption_key.get_secret_value()),
        *[
            base64.b64decode(value.get_secret_value())
            for value in loaded.field_encryption_keys.values()
        ],
    ]
    assert {len(value) for value in decoded} == {32}
    assert len(set(decoded)) == len(decoded)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda values, config: values.__setitem__(
                "candidate_signing_key",
                "not-valid-base64",
            ),
            "candidate_signing_key",
        ),
        (
            lambda values, config: values.__setitem__(
                "backup_encryption_key",
                base64.b64encode(b"short").decode(),
            ),
            "backup_encryption_key",
        ),
        (
            lambda values, config: values.__setitem__(
                f"field-encryption/{config.encryption.active_key_id}",
                "not-valid-base64",
            ),
            "field-encryption",
        ),
        (
            lambda values, config: values.__setitem__(
                "backup_encryption_key",
                values["candidate_signing_key"],
            ),
            "distinct",
        ),
        (
            lambda values, config: values.__setitem__(
                "candidate_signing_key",
                base64.b64encode(bytes(32)).decode(),
            ),
            "known example",
        ),
        (
            lambda values, config: values.__setitem__(
                "app_api_token",
                "short",
            ),
            "app_api_token",
        ),
    ],
)
def test_role_validation_rejects_unsafe_material_before_composition(
    app_config,
    mutate,
    match,
):
    values = _account_values(app_config.encryption)
    mutate(values, app_config)

    with pytest.raises(SecretValidationError, match=match):
        load_role_secrets(
            "app",
            config=app_config,
            provider=MacOSKeychainSecretProvider(
                backend=_FakeMacOSKeyring(values)
            ),
        )


def test_migrate_env_requires_private_file_mode_before_keychain_writes(
    tmp_path,
):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_API_TOKEN=not-read\n", encoding="utf-8")
    env_file.chmod(0o644)
    backend = _FakeMacOSKeyring({})

    with pytest.raises(PermissionError, match="0600"):
        secret_ops.main(
            ["migrate-env", "--env-file", str(env_file)],
            backend=backend,
        )

    assert backend.set_calls == []


def test_migrate_env_verifies_keychain_without_printing_or_mutating_values(
    app_config,
    capsys,
    monkeypatch,
    tmp_path,
):
    environment = _environment(app_config.encryption)
    env_file = tmp_path / ".env"
    original = "".join(
        f"{name}={value}\n"
        for name, value in environment.items()
    ).encode()
    env_file.write_bytes(original)
    env_file.chmod(0o600)
    backend = _FakeMacOSKeyring({})

    def forbidden_prompt(_prompt: str) -> str:
        raise AssertionError("complete migration input must not prompt")

    def forbidden_subprocess(*_args, **_kwargs):
        raise AssertionError("secret values must not enter subprocesses")

    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)

    assert secret_ops.main(
        ["migrate-env", "--env-file", str(env_file)],
        backend=backend,
        config=app_config,
        prompt=forbidden_prompt,
    ) == 0

    output = capsys.readouterr().out
    assert "app_api_token: stored verified" in output
    assert "archive or delete it manually" in output
    assert environment["APP_API_TOKEN"] not in output
    assert environment["CANDIDATE_SIGNING_KEY"] not in output
    assert env_file.read_bytes() == original
    assert backend.values["app_api_token"] == environment["APP_API_TOKEN"]


def test_audit_and_set_commands_report_metadata_only(
    app_config,
    capsys,
):
    values = _account_values(app_config.encryption)
    backend = _FakeMacOSKeyring(values)
    replacement = "replacement-operator-secret-A7v9qL2mN4pR6tU8"

    assert secret_ops.main(
        ["audit"],
        backend=backend,
        config=app_config,
    ) == 0
    audit_output = capsys.readouterr().out
    assert "provider: macos-keychain" in audit_output
    assert f"active-key-id: {app_config.encryption.active_key_id}" in audit_output
    assert "last-successful-load:" in audit_output
    assert values["app_api_token"] not in audit_output

    assert secret_ops.main(
        ["set", "app_api_token"],
        backend=backend,
        config=app_config,
        prompt=lambda _prompt: replacement,
    ) == 0
    set_output = capsys.readouterr().out
    assert set_output.strip() == "app_api_token: stored verified"
    assert replacement not in set_output
    assert backend.values["app_api_token"] == replacement


def test_set_encryption_key_rejects_malformed_material_without_writing(
    app_config,
):
    backend = _FakeMacOSKeyring({})

    with pytest.raises(SecretValidationError, match="32-byte Base64"):
        secret_ops.main(
            [
                "set-encryption-key",
                app_config.encryption.active_key_id,
            ],
            backend=backend,
            config=app_config,
            prompt=lambda _prompt: "not-base64",
        )

    assert backend.set_calls == []
