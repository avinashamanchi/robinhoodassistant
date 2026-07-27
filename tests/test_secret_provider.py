"""Runtime secrets are typed, immutable, and supplied by a provider."""

from __future__ import annotations

import inspect

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
