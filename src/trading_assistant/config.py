"""Configuration loading.

Two sources, deliberately separated:

* ``config.yaml`` — non-secret operating parameters, above all the risk limits.
  Parsed into pydantic models with ``extra="forbid"`` so a misspelled or unknown
  key raises at startup rather than being silently ignored (A8). A silently
  dropped risk limit is the worst failure mode this project has.
* ``.env`` — secrets, loaded via pydantic-settings (:class:`Secrets`).

Live trading is gated by a double-lock (guardrail #1): ``trading.mode == "live"``
in the YAML AND ``LIVE_TRADING_CONFIRM`` equal to the exact confirmation string.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LIVE_CONFIRM_STRING = "I_UNDERSTAND_LIVE_TRADING"


class _Strict(BaseModel):
    """Base for all YAML config models: unknown keys are a hard error (A8)."""

    model_config = ConfigDict(extra="forbid")


class TradingMode(str, enum.Enum):
    PAPER = "paper"
    LIVE = "live"


class BrokerKind(str, enum.Enum):
    MOCK = "mock"
    ALPACA = "alpaca"


class TradingConfig(_Strict):
    mode: TradingMode = TradingMode.PAPER
    broker: BrokerKind = BrokerKind.MOCK


class RiskConfig(_Strict):
    ticker_allowlist: list[str] = Field(min_length=1)
    max_notional_per_order: float = Field(gt=0)
    max_position_per_ticker: float = Field(gt=0)
    max_portfolio_exposure: float = Field(gt=0)
    daily_realized_loss_limit: float = Field(gt=0)
    price_sanity_pct: float = Field(gt=0)
    reject_when_market_closed: bool = True
    proposal_ttl_minutes: int = Field(gt=0)

    @field_validator("ticker_allowlist")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [t.upper() for t in v]


class FeaturesConfig(_Strict):
    auto_execute_preapproved_rules: bool = False
    telegram_notifications: bool = False


class LLMConfig(_Strict):
    model: str
    max_tokens: int = Field(gt=0)


class DaemonConfig(_Strict):
    poll_interval_seconds: int = Field(gt=0)
    use_websocket: bool = True


class AppConfig(_Strict):
    trading: TradingConfig
    risk: RiskConfig
    features: FeaturesConfig
    llm: LLMConfig
    daemon: DaemonConfig


class Secrets(BaseSettings):
    """Secrets from the environment / ``.env``. Never logged (see logging.py)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    anthropic_api_key: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    live_trading_confirm: str = ""
    database_url: str = "sqlite:///./trading_assistant.db"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    app_host: str = "127.0.0.1"
    app_port: int = 8000


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Parse and validate ``config.yaml``. Raises on unknown/invalid keys (A8)."""
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    return AppConfig.model_validate(raw)


def live_trading_enabled(config: AppConfig, secrets: Secrets) -> bool:
    """Guardrail #1: both locks must be set, else we are NOT live."""
    return (
        config.trading.mode is TradingMode.LIVE
        and secrets.live_trading_confirm == LIVE_CONFIRM_STRING
    )
