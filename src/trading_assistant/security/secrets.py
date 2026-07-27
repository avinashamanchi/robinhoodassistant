"""Typed runtime secrets and the provider boundary that supplies them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from ..config import EncryptionConfig


class RuntimeSecrets(BaseModel):
    """Secret values after an explicit provider has loaded and validated them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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
    field_encryption_keys: dict[str, SecretStr] = Field(default_factory=dict)
    backup_encryption_key: SecretStr = SecretStr("")
    live_trading_confirm: SecretStr = SecretStr("")


@runtime_checkable
class SecretProvider(Protocol):
    provider_name: str

    def load(self, *, encryption: EncryptionConfig) -> RuntimeSecrets: ...
