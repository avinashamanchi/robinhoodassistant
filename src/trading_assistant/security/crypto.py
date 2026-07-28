"""Row-bound authenticated envelopes for sensitive database fields."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
import hmac
import json
import os
import re
from types import MappingProxyType
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from ..config import EncryptionConfig
    from .secrets import RuntimeSecrets


_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_VERSION = "v1"
_PREFIX = f"enc:{_VERSION}:"
_NONCE_LENGTH = 12
_TAG_LENGTH = 16


class SensitiveDataInvalid(ValueError):
    """Stable, data-free error for every envelope or key validation failure."""

    stable_code = "sensitive_data_invalid"

    def __init__(self, key_id: str | None = None) -> None:
        self.key_id = key_id if _valid_key_id(key_id) else None
        suffix = f" key_id={self.key_id}" if self.key_id is not None else ""
        super().__init__(f"{self.stable_code}{suffix}")


def _valid_key_id(value: object) -> bool:
    return isinstance(value, str) and _KEY_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class SensitiveFieldRef:
    table: str
    row: str
    column: str
    schema_version: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.table, str)
            or not self.table
            or "\x00" in self.table
            or not isinstance(self.row, str)
            or not self.row
            or "\x00" in self.row
            or not isinstance(self.column, str)
            or not self.column
            or "\x00" in self.column
            or isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version <= 0
        ):
            raise SensitiveDataInvalid()

    def associated_data(self) -> bytes:
        return json.dumps(
            {
                "column": self.column,
                "row": self.row,
                "schema": self.schema_version,
                "table": self.table,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


class SensitiveDataCipher:
    """AES-256-GCM with a strict, versioned, unpadded Base64URL envelope."""

    def __init__(
        self,
        keys: Mapping[str, bytes],
        *,
        active_key_id: str,
    ) -> None:
        try:
            if not isinstance(keys, Mapping) or not _valid_key_id(active_key_id):
                raise SensitiveDataInvalid(
                    active_key_id if _valid_key_id(active_key_id) else None
                )
            copied: dict[str, bytes] = {}
            for key_id, key in keys.items():
                if not _valid_key_id(key_id):
                    raise SensitiveDataInvalid()
                if not isinstance(key, bytes) or len(key) != 32:
                    raise SensitiveDataInvalid(key_id)
                copied[key_id] = bytes(key)
            if active_key_id not in copied:
                raise SensitiveDataInvalid(active_key_id)
            self._keys = MappingProxyType(copied)
            self.active_key_id = active_key_id
        except SensitiveDataInvalid:
            raise
        except Exception:
            raise SensitiveDataInvalid(
                active_key_id if _valid_key_id(active_key_id) else None
            ) from None

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def encrypt(self, plaintext: str, ref: SensitiveFieldRef) -> str:
        key_id = self.active_key_id
        try:
            if not isinstance(plaintext, str) or not plaintext:
                raise SensitiveDataInvalid(key_id)
            if not isinstance(ref, SensitiveFieldRef):
                raise SensitiveDataInvalid(key_id)
            nonce = os.urandom(_NONCE_LENGTH)
            ciphertext = AESGCM(self._keys[key_id]).encrypt(
                nonce,
                plaintext.encode("utf-8"),
                ref.associated_data(),
            )
            payload = base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=")
            return f"{_PREFIX}{key_id}:{payload.decode('ascii')}"
        except SensitiveDataInvalid:
            raise
        except Exception:
            raise SensitiveDataInvalid(key_id) from None

    def decrypt(self, envelope: str, ref: SensitiveFieldRef) -> str:
        key_id: str | None = None
        try:
            if not isinstance(envelope, str) or not isinstance(
                ref,
                SensitiveFieldRef,
            ):
                raise SensitiveDataInvalid()
            parts = envelope.split(":")
            if len(parts) != 4 or parts[0] != "enc":
                raise SensitiveDataInvalid()
            version, candidate_key_id, encoded = parts[1:]
            key_id = (
                candidate_key_id
                if _valid_key_id(candidate_key_id)
                else None
            )
            if version != _VERSION or key_id is None:
                raise SensitiveDataInvalid(key_id)
            key = self._keys.get(key_id)
            if key is None:
                raise SensitiveDataInvalid(key_id)
            if (
                not encoded
                or _BASE64URL.fullmatch(encoded) is None
                or len(encoded) % 4 == 1
            ):
                raise SensitiveDataInvalid(key_id)
            payload = base64.urlsafe_b64decode(
                encoded + "=" * (-len(encoded) % 4)
            )
            canonical = (
                base64.urlsafe_b64encode(payload)
                .rstrip(b"=")
                .decode("ascii")
            )
            if encoded != canonical or len(payload) < _NONCE_LENGTH + _TAG_LENGTH:
                raise SensitiveDataInvalid(key_id)
            plaintext = AESGCM(key).decrypt(
                payload[:_NONCE_LENGTH],
                payload[_NONCE_LENGTH:],
                ref.associated_data(),
            )
            decoded = plaintext.decode("utf-8")
            if not decoded:
                raise SensitiveDataInvalid(key_id)
            return decoded
        except SensitiveDataInvalid:
            raise
        except Exception:
            raise SensitiveDataInvalid(key_id) from None


def build_sensitive_data_cipher(
    encryption: EncryptionConfig,
    secrets: RuntimeSecrets,
) -> SensitiveDataCipher:
    """Build a cipher from exactly the validated active and retained secrets."""
    from .secrets import (
        SecretValidationError,
        validate_base64_key,
        validate_key_id,
    )

    expected_ids = (
        validate_key_id(encryption.active_key_id),
        *(validate_key_id(key_id) for key_id in encryption.retained_key_ids),
    )
    if len(expected_ids) != len(set(expected_ids)):
        raise SecretValidationError(
            "field_encryption_keys",
            "duplicate_key_id",
        )
    if tuple(secrets.field_encryption_keys) != expected_ids:
        raise SecretValidationError(
            "field_encryption_keys",
            "key_id_mismatch",
        )

    buffers: list[bytearray] = []
    decoded: dict[str, bytes] = {}
    try:
        for key_id in expected_ids:
            buffer = validate_base64_key(
                f"field-encryption/{key_id}",
                secrets.field_encryption_keys[key_id],
            )
            duplicate = any(
                hmac.compare_digest(buffer, previous)
                for previous in buffers
            )
            buffers.append(buffer)
            if duplicate:
                raise SecretValidationError(
                    "field_encryption_keys",
                    "shared_key_material",
                )
            decoded[key_id] = bytes(buffer)
        return SensitiveDataCipher(
            decoded,
            active_key_id=encryption.active_key_id,
        )
    finally:
        decoded.clear()
        for buffer in buffers:
            for index in range(len(buffer)):
                buffer[index] = 0
