"""Migrate, audit, and update macOS Keychain runtime secrets."""

from __future__ import annotations

import argparse
import getpass
import hmac
import json
import os
from pathlib import Path
import stat
from typing import Callable

from ..config import AppConfig, load_config
from ..logging import register_all_secrets
from ..security.secrets import (
    KEYCHAIN_SERVICE,
    EnvironmentSecretProvider,
    KeyringBackend,
    MacOSKeychainSecretProvider,
    RuntimeSecrets,
    SecretUnavailable,
    SecretValidationError,
    _SIMPLE_SECRET_FIELDS,
    _configured_key_ids,
    _required_fields,
    _validate_key_material,
    _validate_required_fields,
    app_secret_quality_ok,
    secret_is_set,
    secret_value,
    validate_base64_key,
    validate_key_id,
)

Prompt = Callable[[str], str]


def _verified_provider(
    backend: KeyringBackend | None,
) -> MacOSKeychainSecretProvider:
    return MacOSKeychainSecretProvider(backend=backend)


def _read_private_env(path: Path) -> dict[str, str]:
    try:
        path_metadata = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise PermissionError(".env must be a regular file with mode 0600") from None
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or not stat.S_ISREG(opened_metadata.st_mode)
            or stat.S_IMODE(opened_metadata.st_mode) != 0o600
            or path_metadata.st_dev != opened_metadata.st_dev
            or path_metadata.st_ino != opened_metadata.st_ino
        ):
            raise PermissionError(
                ".env must be a regular file with mode 0600"
            )
        with os.fdopen(
            descriptor,
            "r",
            encoding="utf-8",
        ) as source:
            descriptor = -1
            text = source.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid .env assignment on line {line_number}")
        name, value = line.split("=", maxsplit=1)
        name = name.strip()
        value = value.strip()
        if not name or name in values:
            raise ValueError(f"invalid .env assignment on line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def _prompt_for_migration_values(
    environ: dict[str, str],
    *,
    config: AppConfig,
    prompt: Prompt,
) -> dict[str, str]:
    collected = dict(environ)
    required = {
        *_required_fields("app", config),
        "candidate_signing_key",
        "backup_encryption_key",
    }
    for field_name in _SIMPLE_SECRET_FIELDS:
        env_name = field_name.upper()
        if field_name in required and not collected.get(env_name, ""):
            collected[env_name] = prompt(f"{field_name}: ")

    raw_field_keys = collected.get("FIELD_ENCRYPTION_KEYS_JSON", "")
    if raw_field_keys:
        try:
            parsed = json.loads(raw_field_keys)
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
    else:
        parsed = {}
    for key_id in _configured_key_ids(config.encryption):
        if not parsed.get(key_id):
            parsed[key_id] = prompt(f"field-encryption/{key_id}: ")
    collected["FIELD_ENCRYPTION_KEYS_JSON"] = json.dumps(parsed)
    return collected


def _store_and_verify(
    backend: KeyringBackend,
    account: str,
    value: str,
) -> None:
    try:
        backend.set_password(KEYCHAIN_SERVICE, account, value)
    except Exception:
        raise SecretUnavailable(account, "keyring_write_failed") from None
    try:
        retrieved = backend.get_password(KEYCHAIN_SERVICE, account)
    except Exception:
        raise SecretUnavailable(account, "keyring_verify_failed") from None
    if retrieved is None or not hmac.compare_digest(value, retrieved):
        raise SecretUnavailable(account, "keyring_verify_failed")
    print(f"{account}: stored verified")


def _migrate_env(
    *,
    migration_path: Path,
    config: AppConfig,
    provider: MacOSKeychainSecretProvider,
    prompt: Prompt,
) -> int:
    environ = _prompt_for_migration_values(
        _read_private_env(migration_path),
        config=config,
        prompt=prompt,
    )
    loaded = EnvironmentSecretProvider(environ=environ).load(
        encryption=config.encryption
    )
    register_all_secrets(loaded)
    _validate_required_fields("app", config, loaded)
    _validate_key_material(config, loaded)

    for field_name in _SIMPLE_SECRET_FIELDS:
        value = secret_value(getattr(loaded, field_name))
        if value:
            _store_and_verify(provider.backend, field_name, value)
    for key_id, wrapped in loaded.field_encryption_keys.items():
        _store_and_verify(
            provider.backend,
            f"field-encryption/{key_id}",
            secret_value(wrapped),
        )
    print(
        f"{migration_path}: verified; archive or delete it manually after verification"
    )
    return 0


def _audit(
    *,
    config: AppConfig,
    provider: MacOSKeychainSecretProvider,
) -> int:
    loaded: RuntimeSecrets | None
    try:
        loaded = provider.load(encryption=config.encryption)
    except SecretUnavailable:
        loaded = None

    print(f"provider: {provider.provider_name}")
    if loaded is not None:
        for field_name in _SIMPLE_SECRET_FIELDS:
            state = (
                "present"
                if secret_is_set(getattr(loaded, field_name))
                else "missing"
            )
            print(f"{field_name}: {state}")
        for key_id in _configured_key_ids(config.encryption):
            print(f"field-encryption/{key_id}: present")
    else:
        for account in (
            *_SIMPLE_SECRET_FIELDS,
            *(
                f"field-encryption/{key_id}"
                for key_id in _configured_key_ids(config.encryption)
            ),
        ):
            try:
                value = (
                    provider.backend.get_password(
                        KEYCHAIN_SERVICE,
                        account,
                    )
                )
            except Exception:
                state = "unavailable"
            else:
                state = "present" if value else "missing"
            print(f"{account}: {state}")
    print(f"active-key-id: {config.encryption.active_key_id}")
    retained = ",".join(config.encryption.retained_key_ids) or "none"
    print(f"retained-key-ids: {retained}")
    loaded_at = provider.last_successful_load_at
    print(
        "last-successful-load: "
        + (loaded_at.isoformat() if loaded_at is not None else "never")
    )
    return 0


def _set_simple_secret(
    field_name: str,
    *,
    provider: MacOSKeychainSecretProvider,
    prompt: Prompt,
) -> int:
    if field_name not in _SIMPLE_SECRET_FIELDS:
        raise ValueError("field name is not a simple RuntimeSecrets field")
    value = prompt(f"{field_name}: ")
    if not value:
        raise SecretValidationError(
            field_name,
            "empty",
            f"{field_name} must be non-empty",
        )
    if field_name == "app_api_token" and not app_secret_quality_ok(value):
        raise SecretValidationError(
            field_name,
            "weak_operator_secret",
            "app_api_token fails operator secret quality requirements",
        )
    if field_name in {
        "candidate_signing_key",
        "backup_encryption_key",
    }:
        buffer = validate_base64_key(field_name, value)
        try:
            pass
        finally:
            for index in range(len(buffer)):
                buffer[index] = 0
    _store_and_verify(provider.backend, field_name, value)
    return 0


def _set_encryption_key(
    key_id: str,
    *,
    provider: MacOSKeychainSecretProvider,
    prompt: Prompt,
) -> int:
    key_id = validate_key_id(key_id)
    value = prompt(f"field-encryption/{key_id}: ")
    buffer = validate_base64_key(
        f"field-encryption/{key_id}",
        value,
    )
    try:
        _store_and_verify(
            provider.backend,
            f"field-encryption/{key_id}",
            value,
        )
    finally:
        for index in range(len(buffer)):
            buffer[index] = 0
    return 0


def main(
    argv: list[str] | None = None,
    *,
    backend: KeyringBackend | None = None,
    config: AppConfig | None = None,
    prompt: Prompt = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate-env")
    migrate.add_argument(
        "--env-file",
        dest="migration_path",
        type=Path,
        default=Path(".env"),
    )
    commands.add_parser("audit")
    set_secret = commands.add_parser("set")
    set_secret.add_argument("field_name")
    set_key = commands.add_parser("set-encryption-key")
    set_key.add_argument("key_id")
    args = parser.parse_args(argv)

    selected_config = config or load_config()
    provider = _verified_provider(backend)
    if args.command == "migrate-env":
        return _migrate_env(
            migration_path=args.migration_path,
            config=selected_config,
            provider=provider,
            prompt=prompt,
        )
    if args.command == "audit":
        return _audit(config=selected_config, provider=provider)
    if args.command == "set":
        return _set_simple_secret(
            args.field_name,
            provider=provider,
            prompt=prompt,
        )
    return _set_encryption_key(
        args.key_id,
        provider=provider,
        prompt=prompt,
    )


if __name__ == "__main__":
    raise SystemExit(main())
