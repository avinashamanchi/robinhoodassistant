"""Runtime secrets are typed, immutable, and supplied by a provider."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import inspect
import json
import logging
from pathlib import Path
import subprocess

import pytest
from keyring.backends.chainer import ChainerBackend
from keyring.backends.fail import Keyring as FailKeyring
from keyring.backends.null import Keyring as NullKeyring
from pydantic import SecretStr, ValidationError

from trading_assistant.config import BrokerKind, EncryptionConfig
from trading_assistant.ops import secrets as secret_ops
from trading_assistant import preflight
import trading_assistant.security.secrets as secret_module
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
    validate_base64_key,
)

_ORIGINAL_DEFAULT_KEYRING_BACKEND = secret_module._default_keyring_backend


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
    "backup",
    "validate-analyst",
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


class _FailOnceAfterWriteKeyring(_FakeMacOSKeyring):
    def __init__(
        self,
        values: dict[str, str],
        *,
        fail_at_write: int,
    ) -> None:
        super().__init__(values)
        self.fail_at_write = fail_at_write
        self.write_attempts = 0
        self.failed = False

    def set_password(
        self,
        service: str,
        username: str,
        password: str,
    ) -> None:
        self.write_attempts += 1
        self.set_calls.append((service, username, password))
        self.values[username] = password
        if (
            not self.failed
            and self.write_attempts == self.fail_at_write
        ):
            self.failed = True
            raise RuntimeError("injected write failure with secret marker")


class _FailOnceDuringVerificationKeyring(_FakeMacOSKeyring):
    def __init__(
        self,
        values: dict[str, str],
        *,
        account: str,
    ) -> None:
        super().__init__(values)
        self.account = account
        self.written = False
        self.failed = False

    def set_password(
        self,
        service: str,
        username: str,
        password: str,
    ) -> None:
        super().set_password(service, username, password)
        if username == self.account:
            self.written = True

    def get_password(self, service: str, username: str) -> str | None:
        self.get_calls.append((service, username))
        if (
            username == self.account
            and self.written
            and not self.failed
        ):
            self.failed = True
            raise RuntimeError("injected verification failure with marker")
        return self.values.get(username)


class _CorruptOnceAfterVerificationKeyring(_FakeMacOSKeyring):
    def __init__(
        self,
        values: dict[str, str],
        *,
        account: str,
        corrupt_value: str,
    ) -> None:
        super().__init__(values)
        self.account = account
        self.corrupt_value = corrupt_value
        self.written = False
        self.corrupted = False

    def set_password(
        self,
        service: str,
        username: str,
        password: str,
    ) -> None:
        super().set_password(service, username, password)
        if username == self.account:
            self.written = True

    def get_password(self, service: str, username: str) -> str | None:
        self.get_calls.append((service, username))
        value = self.values.get(username)
        if (
            username == self.account
            and self.written
            and not self.corrupted
        ):
            self.corrupted = True
            self.values[username] = self.corrupt_value
        return value


class _CorruptOtherAccountAfterVerificationKeyring(_FakeMacOSKeyring):
    def __init__(
        self,
        values: dict[str, str],
        *,
        trigger_account: str,
        corrupt_account: str,
        corrupt_value: str,
    ) -> None:
        super().__init__(values)
        self.trigger_account = trigger_account
        self.corrupt_account = corrupt_account
        self.corrupt_value = corrupt_value
        self.trigger_written = False
        self.trigger_verified = False
        self.corrupted = False

    def set_password(
        self,
        service: str,
        username: str,
        password: str,
    ) -> None:
        super().set_password(service, username, password)
        if username == self.trigger_account:
            self.trigger_written = True

    def get_password(self, service: str, username: str) -> str | None:
        self.get_calls.append((service, username))
        if (
            username == self.trigger_account
            and self.trigger_written
            and not self.trigger_verified
        ):
            self.trigger_verified = True
        elif (
            username == self.corrupt_account
            and self.trigger_verified
            and not self.corrupted
        ):
            self.corrupted = True
            return self.corrupt_value
        return self.values.get(username)


def _replacement_environment(
    encryption: EncryptionConfig,
) -> dict[str, str]:
    values = {
        "ANTHROPIC_API_KEY": "replacement-anthropic",
        "GEMINI_API_KEY": "replacement-gemini",
        "GROQ_API_KEY": "replacement-groq",
        "OPENROUTER_API_KEY": "replacement-openrouter",
        "MARKETSTACK_API_KEY": "replacement-marketstack",
        "APP_API_TOKEN": "Q8!vN3#mR7$pL2&tX9-zC5_kW4sD6gH1",
        "ALPACA_API_KEY": "replacement-paper-key",
        "ALPACA_SECRET_KEY": "replacement-paper-secret",
        "DATABASE_URL": "sqlite:///replacement-role.db",
        "TELEGRAM_BOT_TOKEN": "replacement-telegram-bot",
        "TELEGRAM_CHAT_ID": "replacement-telegram-chat",
        "CANDIDATE_SIGNING_KEY": _key("replacement-candidate"),
        "BACKUP_ENCRYPTION_KEY": _key("replacement-backup"),
        "LIVE_TRADING_CONFIRM": "",
    }
    values["FIELD_ENCRYPTION_KEYS_JSON"] = json.dumps(
        {
            key_id: _key(f"replacement-field:{key_id}")
            for key_id in (
                encryption.active_key_id,
                *encryption.retained_key_ids,
            )
        }
    )
    return values


def _private_env_file(
    tmp_path: Path,
    environment: dict[str, str],
) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "".join(f"{name}={value}\n" for name, value in environment.items()),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return env_file


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
        _ORIGINAL_DEFAULT_KEYRING_BACKEND()


class _PlaintextBackend:
    pass


@pytest.mark.parametrize(
    "selected_backend",
    [
        pytest.param(object.__new__(FailKeyring), id="fail"),
        pytest.param(object.__new__(NullKeyring), id="null"),
        pytest.param(_PlaintextBackend(), id="plaintext"),
        pytest.param(object.__new__(ChainerBackend), id="chainer"),
    ],
)
def test_default_keychain_provider_rejects_each_unsafe_backend_family(
    monkeypatch,
    selected_backend,
):
    import keyring
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        keyring,
        "get_keyring",
        lambda: selected_backend,
    )

    with pytest.raises(UnsafeKeyringBackend, match="macOS"):
        _ORIGINAL_DEFAULT_KEYRING_BACKEND()


def test_repository_guard_blocks_uninjected_default_keychain():
    with pytest.raises(AssertionError, match="must inject"):
        MacOSKeychainSecretProvider()


def test_environment_provider_parses_only_the_injected_mapping():
    encryption = _encryption_config()
    provider = EnvironmentSecretProvider(
        environ=_environment(encryption),
        encryption=encryption,
    )

    loaded = provider.load(encryption=encryption)

    assert loaded.alpaca_api_key.get_secret_value() == "paper-key"
    assert (
        loaded.field_encryption_keys[
            encryption.active_key_id
        ].get_secret_value()
        == _key(f"field-encryption:{encryption.active_key_id}")
    )


def test_environment_provider_retains_structured_secrets_not_raw_field_key_json():
    encryption = _encryption_config(
        retained=("local-retained-2026-06",)
    )
    environment = _environment(encryption)
    raw_field_keys = environment["FIELD_ENCRYPTION_KEYS_JSON"]

    provider = EnvironmentSecretProvider(
        environ=environment,
        encryption=encryption,
    )
    loaded = provider.load(encryption=encryption)

    assert tuple(loaded.field_encryption_keys) == (
        "local-primary-2026-07",
        "local-retained-2026-06",
    )
    assert raw_field_keys not in repr(provider.__dict__)
    assert "FIELD_ENCRYPTION_KEYS_JSON" not in repr(provider.__dict__)


@pytest.mark.parametrize(
    "nested_key",
    [
        pytest.param("COMPOSIO_API_KEY", id="composio"),
        pytest.param("unrelated-key-2026", id="unrelated"),
    ],
)
def test_environment_provider_rejects_unknown_nested_field_key_without_retention(
    capsys,
    nested_key,
):
    encryption = _encryption_config()
    marker = "nested-field-key-marker-must-not-be-retained"
    environment = _environment(encryption)
    nested = json.loads(environment["FIELD_ENCRYPTION_KEYS_JSON"])
    nested[nested_key] = marker
    environment["FIELD_ENCRYPTION_KEYS_JSON"] = json.dumps(nested)

    with pytest.raises(SecretValidationError) as captured:
        EnvironmentSecretProvider(
            environ=environment,
            encryption=encryption,
        )

    assert captured.value.stable_code == "unexpected_key_id"
    assert marker not in str(captured.value)
    assert marker not in capsys.readouterr().out


def test_environment_provider_rejects_composio_nested_key_even_if_configured(
    capsys,
):
    encryption = EncryptionConfig(active_key_id="COMPOSIO_ACTIVE_KEY")
    marker = "configured-composio-marker-must-not-be-retained"
    environment = _environment(encryption)
    environment["FIELD_ENCRYPTION_KEYS_JSON"] = json.dumps(
        {"COMPOSIO_ACTIVE_KEY": marker}
    )

    with pytest.raises(SecretValidationError) as captured:
        EnvironmentSecretProvider(
            environ=environment,
            encryption=encryption,
        )

    assert captured.value.stable_code == "unexpected_key_id"
    assert marker not in str(captured.value)
    assert marker not in capsys.readouterr().out


def test_noncanonical_base64_pad_bits_cannot_bypass_known_example_rejection():
    canonical = base64.b64encode(bytes(32)).decode()
    noncanonical = canonical[:-2] + "B="
    assert base64.b64decode(noncanonical) == bytes(32)

    with pytest.raises(SecretValidationError) as captured:
        validate_base64_key("candidate_signing_key", noncanonical)

    assert captured.value.stable_code in {
        "invalid_base64",
        "known_example",
    }


def test_environment_provider_drops_composio_and_unknown_entries_without_retention(
    capsys,
):
    encryption = _encryption_config()
    marker = "compromised-composio-marker-must-not-be-retained"
    environment = {
        **_environment(encryption),
        "COMPOSIO_API_KEY": marker,
        "UNRELATED_SECRET": marker,
    }

    provider = EnvironmentSecretProvider(
        environ=environment,
        encryption=encryption,
    )
    loaded = provider.load(encryption=encryption)

    assert loaded.alpaca_api_key.get_secret_value() == "paper-key"
    assert "COMPOSIO_API_KEY" not in repr(provider.__dict__)
    assert "UNRELATED_SECRET" not in repr(provider.__dict__)
    assert marker not in repr(provider.__dict__)
    assert marker not in capsys.readouterr().out


def test_private_env_reader_drops_composio_without_returning_or_printing_it(
    capsys,
    tmp_path,
):
    marker = "compromised-composio-file-marker"
    env_file = _private_env_file(
        tmp_path,
        {
            "APP_API_TOKEN": "allowed-operator-value",
            "COMPOSIO_API_KEY": marker,
            "UNRELATED_SECRET": marker,
        },
    )

    loaded = secret_ops._read_private_env(env_file)

    assert loaded == {"APP_API_TOKEN": "allowed-operator-value"}
    assert marker not in repr(loaded)
    assert marker not in capsys.readouterr().out


def test_migration_prompt_collection_drops_unallowlisted_input_without_retention(
    app_config,
    capsys,
):
    marker = "compromised-prompt-input-marker"
    environment = {
        **_environment(app_config.encryption),
        "COMPOSIO_API_KEY": marker,
        "UNRELATED_SECRET": marker,
    }

    loaded = secret_ops._prompt_for_migration_values(
        environment,
        config=app_config,
        prompt=lambda _prompt: pytest.fail("unexpected prompt"),
    )

    assert isinstance(loaded, RuntimeSecrets)
    assert "COMPOSIO_API_KEY" not in repr(loaded)
    assert "UNRELATED_SECRET" not in repr(loaded)
    assert environment["FIELD_ENCRYPTION_KEYS_JSON"] not in repr(loaded)
    assert marker not in repr(loaded)
    assert marker not in capsys.readouterr().out


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
                environ=_environment(app_config.encryption),
                encryption=app_config.encryption,
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


@pytest.mark.parametrize(
    "nested_key",
    [
        pytest.param("COMPOSIO_API_KEY", id="composio"),
        pytest.param("unrelated-key-2026", id="unrelated"),
    ],
)
def test_migrate_env_rejects_unknown_nested_field_key_without_writes_or_leaks(
    app_config,
    capsys,
    nested_key,
    tmp_path,
):
    marker = "nested-migration-marker-must-not-be-retained"
    environment = _environment(app_config.encryption)
    nested = json.loads(environment["FIELD_ENCRYPTION_KEYS_JSON"])
    nested[nested_key] = marker
    environment["FIELD_ENCRYPTION_KEYS_JSON"] = json.dumps(nested)
    env_file = _private_env_file(tmp_path, environment)
    backend = _FakeMacOSKeyring(
        _account_values(app_config.encryption)
    )

    with pytest.raises(SecretValidationError) as captured:
        secret_ops.main(
            ["migrate-env", "--env-file", str(env_file)],
            backend=backend,
            config=app_config,
            prompt=lambda _prompt: pytest.fail("unexpected prompt"),
        )

    assert captured.value.stable_code == "unexpected_key_id"
    assert backend.set_calls == []
    assert backend.delete_calls == []
    assert marker not in str(captured.value)
    assert marker not in capsys.readouterr().out


def test_audit_validates_current_app_role_and_reports_current_timestamp_only(
    app_config,
    capsys,
):
    values = _account_values(app_config.encryption)
    backend = _FakeMacOSKeyring(values)

    assert secret_ops.main(
        ["audit"],
        backend=backend,
        config=app_config,
    ) == 0
    audit_output = capsys.readouterr().out
    assert "provider: macos-keychain" in audit_output
    assert f"active-key-id: {app_config.encryption.active_key_id}" in audit_output
    assert "audit-read: complete" in audit_output
    assert "current-app-role-validation: passed" in audit_output
    timestamp_line = next(
        line
        for line in audit_output.splitlines()
        if line.startswith("current-app-role-validation-at: ")
    )
    timestamp = timestamp_line.split(": ", maxsplit=1)[1]
    assert datetime.fromisoformat(timestamp).tzinfo is not None
    assert "historical-role-load: unavailable" in audit_output
    assert "last-successful-role-load" not in audit_output
    assert values["app_api_token"] not in audit_output


def test_set_command_reports_metadata_only(
    app_config,
    capsys,
):
    values = _account_values(app_config.encryption)
    backend = _FakeMacOSKeyring(values)
    replacement = "replacement-operator-secret-A7v9qL2mN4pR6tU8"

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


@pytest.mark.parametrize("failure_index", range(1, 15))
def test_migrate_env_rolls_back_failure_at_every_write_and_retries_safely(
    app_config,
    capsys,
    failure_index,
    tmp_path,
):
    original = _account_values(app_config.encryption)
    environment = _replacement_environment(app_config.encryption)
    env_file = _private_env_file(tmp_path, environment)
    backend = _FailOnceAfterWriteKeyring(
        original,
        fail_at_write=failure_index,
    )

    with pytest.raises(SecretUnavailable):
        secret_ops.main(
            ["migrate-env", "--env-file", str(env_file)],
            backend=backend,
            config=app_config,
            prompt=lambda _prompt: pytest.fail("unexpected prompt"),
        )

    failed_output = capsys.readouterr().out
    assert "stored verified" not in failed_output
    assert backend.values == original

    assert secret_ops.main(
        ["migrate-env", "--env-file", str(env_file)],
        backend=backend,
        config=app_config,
        prompt=lambda _prompt: pytest.fail("unexpected prompt"),
    ) == 0

    parsed_field_keys = json.loads(
        environment["FIELD_ENCRYPTION_KEYS_JSON"]
    )
    for account in _SIMPLE_ACCOUNTS:
        value = environment.get(account.upper(), "")
        if value:
            assert backend.values[account] == value
    for key_id, value in parsed_field_keys.items():
        assert backend.values[f"field-encryption/{key_id}"] == value


def test_set_rolls_back_new_account_after_verification_failure_and_retries(
    app_config,
    capsys,
):
    original = _account_values(app_config.encryption)
    original.pop("marketstack_api_key")
    backend = _FailOnceDuringVerificationKeyring(
        original,
        account="marketstack_api_key",
    )
    replacement = "replacement-marketstack-key"

    with pytest.raises(SecretUnavailable):
        secret_ops.main(
            ["set", "marketstack_api_key"],
            backend=backend,
            config=app_config,
            prompt=lambda _prompt: replacement,
        )

    assert "marketstack_api_key" not in backend.values
    assert (
        _SERVICE,
        "marketstack_api_key",
    ) in backend.delete_calls
    assert "stored verified" not in capsys.readouterr().out

    assert secret_ops.main(
        ["set", "marketstack_api_key"],
        backend=backend,
        config=app_config,
        prompt=lambda _prompt: replacement,
    ) == 0
    assert backend.values["marketstack_api_key"] == replacement


def test_set_rolls_back_postwrite_global_validation_failure_and_retries(
    app_config,
    capsys,
):
    original = _account_values(app_config.encryption)
    backend = _CorruptOtherAccountAfterVerificationKeyring(
        original,
        trigger_account="marketstack_api_key",
        corrupt_account="candidate_signing_key",
        corrupt_value=original["backup_encryption_key"],
    )
    replacement = "replacement-marketstack-key"

    with pytest.raises(SecretValidationError, match="distinct"):
        secret_ops.main(
            ["set", "marketstack_api_key"],
            backend=backend,
            config=app_config,
            prompt=lambda _prompt: replacement,
        )

    assert backend.values == original
    assert "stored verified" not in capsys.readouterr().out

    assert secret_ops.main(
        ["set", "marketstack_api_key"],
        backend=backend,
        config=app_config,
        prompt=lambda _prompt: replacement,
    ) == 0
    assert backend.values["marketstack_api_key"] == replacement


def test_set_rolls_back_postwrite_value_mismatch_even_when_still_valid(
    app_config,
    capsys,
):
    original = _account_values(app_config.encryption)
    backend = _CorruptOnceAfterVerificationKeyring(
        original,
        account="candidate_signing_key",
        corrupt_value=_key("unexpected-but-valid-candidate"),
    )
    replacement = _key("intended-candidate-replacement")

    with pytest.raises(SecretUnavailable):
        secret_ops.main(
            ["set", "candidate_signing_key"],
            backend=backend,
            config=app_config,
            prompt=lambda _prompt: replacement,
        )

    assert backend.values == original
    assert "stored verified" not in capsys.readouterr().out


def test_set_rejects_update_when_complete_role_state_is_invalid(
    app_config,
):
    values = _account_values(app_config.encryption)
    values.pop("backup_encryption_key")
    backend = _FakeMacOSKeyring(values)

    with pytest.raises(SecretValidationError):
        secret_ops.main(
            ["set", "marketstack_api_key"],
            backend=backend,
            config=app_config,
            prompt=lambda _prompt: "replacement-marketstack",
        )

    assert backend.set_calls == []
    assert backend.values == values


def test_set_rejects_live_confirmation_that_invalidates_paper_role(
    app_config,
):
    values = _account_values(app_config.encryption)
    backend = _FakeMacOSKeyring(values)

    with pytest.raises(SecretValidationError, match="paper"):
        secret_ops.main(
            ["set", "live_trading_confirm"],
            backend=backend,
            config=app_config,
            prompt=lambda _prompt: "ENABLE_LIVE_TRADING",
        )

    assert backend.set_calls == []
    assert backend.values == values


def test_set_encryption_key_rejects_material_shared_with_candidate(
    app_config,
):
    values = _account_values(app_config.encryption)
    backend = _FakeMacOSKeyring(values)
    before = dict(values)

    with pytest.raises(SecretValidationError, match="distinct"):
        secret_ops.main(
            [
                "set-encryption-key",
                app_config.encryption.active_key_id,
            ],
            backend=backend,
            config=app_config,
            prompt=lambda _prompt: values["candidate_signing_key"],
        )

    assert backend.set_calls == []
    assert backend.values == before


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda values, config: values.__setitem__(
                "candidate_signing_key",
                "not-valid-base64",
            ),
            id="malformed",
        ),
        pytest.param(
            lambda values, config: values.__setitem__(
                f"field-encryption/{config.encryption.active_key_id}",
                values["candidate_signing_key"],
            ),
            id="shared",
        ),
        pytest.param(
            lambda values, config: values.pop(
                f"field-encryption/{config.encryption.active_key_id}"
            ),
            id="missing",
        ),
    ],
)
def test_audit_blocks_current_role_validation_without_success_timestamp(
    app_config,
    capsys,
    mutate,
):
    values = _account_values(app_config.encryption)
    mutate(values, app_config)

    assert secret_ops.main(
        ["audit"],
        backend=_FakeMacOSKeyring(values),
        config=app_config,
    ) == 1

    output = capsys.readouterr().out
    assert "audit-read: complete" in output
    assert "current-app-role-validation: blocked" in output
    assert "current-app-role-validation-at: unavailable" in output
    assert "historical-role-load: unavailable" in output
    assert "current-app-role-validation: passed" not in output
    assert "last-successful-role-load" not in output
    candidate = values.get("candidate_signing_key")
    if candidate is not None:
        assert candidate not in output


def test_preflight_partial_keychain_prints_needs_without_external_checks(
    app_config,
    capsys,
    monkeypatch,
):
    config = app_config.model_copy(
        update={
            "trading": app_config.trading.model_copy(
                update={"broker": BrokerKind.ALPACA}
            )
        }
    )
    values = _account_values(config.encryption)
    marker = values["anthropic_api_key"]
    values.pop("app_api_token")
    values.pop("alpaca_secret_key")
    provider = MacOSKeychainSecretProvider(
        backend=_FakeMacOSKeyring(values)
    )

    monkeypatch.setattr(preflight, "load_config", lambda *_args: config)

    def forbidden_external_check(*_args, **_kwargs):
        raise AssertionError("partial preflight must not call dependencies")

    monkeypatch.setattr(preflight, "_alpaca", forbidden_external_check)
    monkeypatch.setattr(preflight, "_db", forbidden_external_check)
    monkeypatch.setattr(preflight, "_build_service", forbidden_external_check)

    assert preflight.run(provider=provider) == 1

    output = capsys.readouterr().out
    assert "runtime secret role validation" in output
    assert "NEEDS-ME" in output
    assert "Alpaca paper auth" in output
    assert "NOT READY" in output
    assert marker not in output
    assert provider.last_successful_role_load_at is None
