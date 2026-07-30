"""Migrate, audit, and update macOS Keychain runtime secrets."""

from __future__ import annotations

import argparse
import getpass
import hmac
import os
from pathlib import Path
import stat
from typing import Callable

from ..config import AppConfig, load_config
from ..logging import register_all_secrets
from ..security.secrets import (
    KEYCHAIN_SERVICE,
    KeyringBackend,
    MacOSKeychainSecretProvider,
    RuntimeSecrets,
    SecretBoundaryError,
    SecretUnavailable,
    SecretValidationError,
    _ENVIRONMENT_SECRET_NAMES,
    _SIMPLE_SECRET_FIELDS,
    _configured_key_ids,
    _parse_environment_field_keys,
    _required_fields,
    _validate_key_material,
    _validate_required_fields,
    app_secret_quality_ok,
    load_role_secrets,
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
    values: dict[str, str] = {}
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
            for line_number, raw_line in enumerate(source, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    raise ValueError(
                        f"invalid .env assignment on line {line_number}"
                    )
                name, value = line.split("=", maxsplit=1)
                name = name.strip()
                if not name:
                    raise ValueError(
                        f"invalid .env assignment on line {line_number}"
                    )
                if name not in _ENVIRONMENT_SECRET_NAMES:
                    continue
                if name in values:
                    raise ValueError(
                        f"invalid .env assignment on line {line_number}"
                    )
                value = value.strip()
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in {"'", '"'}
                ):
                    value = value[1:-1]
                values[name] = value
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return values


def _prompt_for_migration_values(
    environ: dict[str, str],
    *,
    config: AppConfig,
    prompt: Prompt,
) -> RuntimeSecrets:
    collected = {
        field_name: environ.get(field_name.upper(), "")
        for field_name in _SIMPLE_SECRET_FIELDS
    }
    required = {
        *_required_fields("app", config),
        "candidate_signing_key",
        "backup_encryption_key",
    }
    for field_name in _SIMPLE_SECRET_FIELDS:
        if field_name in required and not collected.get(field_name, ""):
            collected[field_name] = prompt(f"{field_name}: ")

    parsed = _parse_environment_field_keys(
        environ.get("FIELD_ENCRYPTION_KEYS_JSON", ""),
        encryption=config.encryption,
        allow_missing=True,
    )
    for key_id in _configured_key_ids(config.encryption):
        if not parsed.get(key_id):
            parsed[key_id] = prompt(f"field-encryption/{key_id}: ")
    return RuntimeSecrets(
        **collected,
        field_encryption_keys=parsed,
    )


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


def _configured_accounts(config: AppConfig) -> tuple[str, ...]:
    return (
        *_SIMPLE_SECRET_FIELDS,
        *(
            f"field-encryption/{key_id}"
            for key_id in _configured_key_ids(config.encryption)
        ),
    )


def _read_accounts(
    backend: KeyringBackend,
    accounts: tuple[str, ...],
    *,
    stable_code: str,
) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for account in accounts:
        try:
            values[account] = backend.get_password(
                KEYCHAIN_SERVICE,
                account,
            )
        except Exception:
            raise SecretUnavailable(account, stable_code) from None
    return values


def _runtime_from_accounts(
    config: AppConfig,
    values: dict[str, str | None],
) -> RuntimeSecrets:
    return RuntimeSecrets(
        **{
            field_name: values.get(field_name) or ""
            for field_name in _SIMPLE_SECRET_FIELDS
        },
        field_encryption_keys={
            key_id: (
                values.get(f"field-encryption/{key_id}") or ""
            )
            for key_id in _configured_key_ids(config.encryption)
        },
    )


def _validate_complete_account_state(
    config: AppConfig,
    values: dict[str, str | None],
) -> RuntimeSecrets:
    loaded = _runtime_from_accounts(config, values)
    register_all_secrets(loaded)
    _validate_required_fields("app", config, loaded)
    _validate_key_material(config, loaded)
    if secret_is_set(loaded.live_trading_confirm):
        raise SecretValidationError(
            "live_trading_confirm",
            "paper_role_required",
            "production secret state must remain paper-only",
        )
    return loaded


def _restore_accounts(
    backend: KeyringBackend,
    snapshot: dict[str, str | None],
    attempted: list[str],
) -> None:
    failed = False
    restored: set[str] = set()
    for account in reversed(attempted):
        if account in restored:
            continue
        restored.add(account)
        original = snapshot[account]
        try:
            if original is None:
                backend.delete_password(KEYCHAIN_SERVICE, account)
            else:
                backend.set_password(
                    KEYCHAIN_SERVICE,
                    account,
                    original,
                )
        except Exception:
            failed = True
    for account in restored:
        original = snapshot[account]
        try:
            current = backend.get_password(KEYCHAIN_SERVICE, account)
        except Exception:
            failed = True
            continue
        if original is None:
            failed = failed or current is not None
        elif current is None or not hmac.compare_digest(
            original,
            current,
        ):
            failed = True
    if failed:
        raise SecretUnavailable(
            "keychain-transaction",
            "rollback_failed",
        )


def _transactional_store(
    *,
    backend: KeyringBackend,
    config: AppConfig,
    updates: dict[str, str],
) -> tuple[str, ...]:
    accounts = _configured_accounts(config)
    unexpected = set(updates).difference(accounts)
    if unexpected:
        raise SecretValidationError(
            "keychain_account",
            "unconfigured_account",
            "secret account is not configured",
        )
    snapshot = _read_accounts(
        backend,
        accounts,
        stable_code="keyring_snapshot_failed",
    )
    proposed = dict(snapshot)
    proposed.update(updates)
    _validate_complete_account_state(config, proposed)

    attempted: list[str] = []
    try:
        for account, value in updates.items():
            attempted.append(account)
            _store_and_verify(backend, account, value)
        persisted = _read_accounts(
            backend,
            accounts,
            stable_code="keyring_postwrite_read_failed",
        )
        for account, expected in updates.items():
            current = persisted.get(account)
            if current is None or not hmac.compare_digest(
                expected,
                current,
            ):
                raise SecretUnavailable(
                    account,
                    "keyring_postwrite_mismatch",
                )
        _validate_complete_account_state(config, persisted)
    except Exception:
        _restore_accounts(backend, snapshot, attempted)
        raise
    return tuple(updates)


def _migrate_env(
    *,
    migration_path: Path,
    config: AppConfig,
    provider: MacOSKeychainSecretProvider,
    prompt: Prompt,
) -> int:
    loaded = _prompt_for_migration_values(
        _read_private_env(migration_path),
        config=config,
        prompt=prompt,
    )
    register_all_secrets(loaded)
    _validate_required_fields("app", config, loaded)
    _validate_key_material(config, loaded)

    updates: dict[str, str] = {}
    for field_name in _SIMPLE_SECRET_FIELDS:
        value = secret_value(getattr(loaded, field_name))
        if value:
            updates[field_name] = value
    for key_id, wrapped in loaded.field_encryption_keys.items():
        updates[f"field-encryption/{key_id}"] = secret_value(wrapped)
    stored = _transactional_store(
        backend=provider.backend,
        config=config,
        updates=updates,
    )
    for account in stored:
        print(f"{account}: stored verified")
    print(
        f"{migration_path}: verified; archive or delete it manually after verification"
    )
    return 0


def _audit(
    *,
    config: AppConfig,
    provider: MacOSKeychainSecretProvider,
) -> int:
    presence = provider.read_presence(encryption=config.encryption)
    try:
        load_role_secrets(
            "app",
            config=config,
            provider=provider,
        )
    except SecretBoundaryError:
        current_validation = "blocked"
        current_validation_at = None
    else:
        current_validation = "passed"
        current_validation_at = provider.last_successful_role_load_at

    print(f"provider: {provider.provider_name}")
    print(
        "audit-read: "
        + (
            "complete"
            if all(value is not None for value in presence.values())
            else "partial"
        )
    )
    for account, is_present in presence.items():
        state = (
            "unavailable"
            if is_present is None
            else ("present" if is_present else "missing")
        )
        print(f"{account}: {state}")
    print(f"active-key-id: {config.encryption.active_key_id}")
    retained = ",".join(config.encryption.retained_key_ids) or "none"
    print(f"retained-key-ids: {retained}")
    print(f"current-app-role-validation: {current_validation}")
    print(
        "current-app-role-validation-at: "
        + (
            current_validation_at.isoformat()
            if current_validation_at is not None
            else "unavailable"
        )
    )
    print("historical-role-load: unavailable (not persisted)")
    return 0 if current_validation == "passed" else 1


def _set_simple_secret(
    field_name: str,
    *,
    config: AppConfig,
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
    _transactional_store(
        backend=provider.backend,
        config=config,
        updates={field_name: value},
    )
    print(f"{field_name}: stored verified")
    return 0


def _set_encryption_key(
    key_id: str,
    *,
    config: AppConfig,
    provider: MacOSKeychainSecretProvider,
    prompt: Prompt,
) -> int:
    key_id = validate_key_id(key_id)
    if key_id not in _configured_key_ids(config.encryption):
        raise SecretValidationError(
            "encryption_key_id",
            "unconfigured_key_id",
            "encryption key ID is not active or retained",
        )
    value = prompt(f"field-encryption/{key_id}: ")
    buffer = validate_base64_key(
        f"field-encryption/{key_id}",
        value,
    )
    try:
        account = f"field-encryption/{key_id}"
        _transactional_store(
            backend=provider.backend,
            config=config,
            updates={account: value},
        )
    finally:
        for index in range(len(buffer)):
            buffer[index] = 0
    print(f"{account}: stored verified")
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
            config=selected_config,
            provider=provider,
            prompt=prompt,
        )
    return _set_encryption_key(
        args.key_id,
        config=selected_config,
        provider=provider,
        prompt=prompt,
    )


if __name__ == "__main__":
    raise SystemExit(main())
