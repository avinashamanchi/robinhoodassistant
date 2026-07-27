"""Runtime secrets are typed, immutable, and supplied by a provider."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trading_assistant.config import EncryptionConfig
from trading_assistant.security.secrets import RuntimeSecrets, SecretProvider


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


def test_secret_provider_contract_is_runtime_checkable():
    provider = _StaticSecretProvider()

    assert isinstance(provider, SecretProvider)
    assert provider.load(
        encryption=EncryptionConfig(active_key_id="test-key-1")
    ) == RuntimeSecrets()
