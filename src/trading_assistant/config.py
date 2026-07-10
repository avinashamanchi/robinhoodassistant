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
    # Non-blocking WARNING when combined (Alpaca + external) exposure in a ticker
    # would exceed max_position_per_ticker. Never blocks — external isn't ours.
    warn_on_cross_broker_concentration: bool = True
    # Percent of portfolio equity risked per trade (deterministic sizing, Phase 8).
    per_trade_risk_pct: float = Field(default=0.5, gt=0, le=100)

    @field_validator("ticker_allowlist")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [t.upper() for t in v]


class FeaturesConfig(_Strict):
    auto_execute_preapproved_rules: bool = False
    telegram_notifications: bool = False


class LLMConfig(_Strict):
    model: str                                   # anthropic model
    max_tokens: int = Field(gt=0)
    provider: str = "anthropic"                  # anthropic | gemini | groq
    fallback_provider: Optional[str] = None      # tried if primary errors at call time
    gemini_model: str = "gemini-flash-latest"
    groq_model: str = "llama-3.3-70b-versatile"


class DaemonConfig(_Strict):
    poll_interval_seconds: int = Field(gt=0)
    use_websocket: bool = True


class FillConfig(_Strict):
    market: str = "next_bar_open"
    limit: str = "bar_range_cross"
    max_participation_pct: float = Field(default=10.0, gt=0, le=100)


class BacktestConfig(_Strict):
    """Simulation cost model. Fees and slippage are deliberately separate (Phase 7)."""

    fills: FillConfig = Field(default_factory=FillConfig)
    slippage_bps: dict[str, float] = Field(
        default_factory=lambda: {"equity": 5.0, "crypto": 20.0}
    )
    fees_bps: dict[str, float] = Field(
        default_factory=lambda: {"equity": 0.0, "crypto": 25.0}
    )
    holdout_months: int = Field(default=12, gt=0)


class RobinhoodConfig(_Strict):
    enabled: bool = False              # OFF by default, like everything dangerous
    cache_ttl_seconds: float = Field(default=300.0, gt=0)
    token_path: str = "./.rh_token.pickle"


class ExternalAccountsConfig(_Strict):
    robinhood: RobinhoodConfig = Field(default_factory=RobinhoodConfig)


class ScreenerConfig(_Strict):
    universe: list[str] = Field(default_factory=list)  # empty -> use risk allowlist
    top_n: int = Field(default=10, gt=0)


class AnalystExtrasConfig(_Strict):
    news_enabled: bool = False


class AppConfig(_Strict):
    trading: TradingConfig
    risk: RiskConfig
    features: FeaturesConfig
    llm: LLMConfig
    daemon: DaemonConfig
    # Phase 7 additions (optional so pre-Phase-7 configs still load).
    crypto_risk: Optional[RiskConfig] = None
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    # Read-only external account sources (optional).
    external_accounts: Optional[ExternalAccountsConfig] = None
    screener: ScreenerConfig = Field(default_factory=ScreenerConfig)
    analyst: AnalystExtrasConfig = Field(default_factory=AnalystExtrasConfig)


class Secrets(BaseSettings):
    """Secrets from the environment / ``.env``. Never logged (see logging.py)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""
    marketstack_api_key: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    live_trading_confirm: str = ""
    database_url: str = "sqlite:///./trading_assistant.db"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    # Robinhood (read-only external source). Never logged (see logging.py).
    rh_username: str = ""
    rh_password: str = ""
    rh_totp_secret: str = ""
    rh_token_path: str = "./.rh_token.pickle"


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
