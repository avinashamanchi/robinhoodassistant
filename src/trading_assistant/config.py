"""Configuration loading.

Two sources, deliberately separated:

* ``config.yaml`` — non-secret operating parameters, above all the risk limits.
  Parsed into pydantic models with ``extra="forbid"`` so a misspelled or unknown
  key raises at startup rather than being silently ignored (A8). A silently
  dropped risk limit is the worst failure mode this project has.
* runtime secrets — supplied separately through a typed secret provider.

Legacy live-mode fields remain parseable for configuration compatibility, but
the safety-foundation production bootstrap and broker factory reject/ignore
them: this release is hard-locked to Alpaca paper trading.
"""

from __future__ import annotations

import enum
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .security.secrets import RuntimeSecrets

LIVE_CONFIRM_STRING = "I_UNDERSTAND_LIVE_TRADING"


class _Strict(BaseModel):
    """Base for all YAML config models: unknown keys are a hard error (A8)."""

    model_config = ConfigDict(
        extra="forbid",
        url_preserve_empty_path=True,
        validate_default=True,
    )


class ServerConfig(_Strict):
    bind_host: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    port: int = Field(default=8020, ge=1024, le=65535)
    origin: AnyUrl = "https://localhost:8020"
    allowed_hosts: list[Literal["localhost", "127.0.0.1", "::1"]] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1"]
    )
    tls_cert_path: Path = Path(".local/tls/localhost.pem")
    tls_key_path: Path = Path(".local/tls/localhost-key.pem")
    secure_cookies: Literal[True] = True


class ProviderOriginsConfig(_Strict):
    alpaca_trading: AnyUrl = "https://paper-api.alpaca.markets"
    alpaca_data: AnyUrl = "https://data.alpaca.markets"
    alpaca_stream: AnyUrl = "wss://stream.data.alpaca.markets"
    anthropic: AnyUrl = "https://api.anthropic.com"
    gemini: AnyUrl = "https://generativelanguage.googleapis.com"
    groq: AnyUrl = "https://api.groq.com"
    telegram: AnyUrl = "https://api.telegram.org"
    coingecko: AnyUrl = "https://api.coingecko.com"


class EncryptionConfig(_Strict):
    required: Literal[True] = True
    schema_version: Literal[1] = 1
    active_key_id: str = Field(min_length=8, max_length=64)
    retained_key_ids: list[str] = Field(default_factory=list)
    backup_key_id: str = Field(
        default="local-backup-2026-07",
        min_length=8,
        max_length=64,
    )
    backup_directory: Path = Path(".local/encrypted-backups")


class IntegrationsConfig(_Strict):
    webhooks_enabled: Literal[False] = False
    composio_enabled: Literal[False] = False


class TradingMode(str, enum.Enum):
    PAPER = "paper"
    LIVE = "live"


class BrokerKind(str, enum.Enum):
    MOCK = "mock"
    ALPACA = "alpaca"


class TradingConfig(_Strict):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: TradingMode = TradingMode.PAPER
    broker: BrokerKind = BrokerKind.MOCK
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    reconciliation_max_age_seconds: float = Field(default=300.0, gt=0)


class RiskConfig(_Strict):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

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
    max_quote_age_seconds: float = Field(default=60.0, gt=0)
    max_spread_pct: float = Field(default=1.0, gt=0)
    max_daily_total_loss: float = Field(default=500.0, gt=0)
    max_account_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
    require_broker_reconciled: bool = True

    @field_validator("ticker_allowlist")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [t.upper() for t in v]


class FeaturesConfig(_Strict):
    auto_execute_preapproved_rules: bool = False
    telegram_notifications: bool = False
    shadow_mode: bool = False        # D1: screen+analyze+grade live, zero orders


class ExecutionConfig(_Strict):
    prefer_bracket_orders: bool = False  # disabled until paper concurrency review


class WindowLimitConfig(_Strict):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    requests: int = Field(gt=0)
    global_requests: int = Field(gt=0)
    window_seconds: int = Field(gt=0)
    concurrency: int = Field(default=1, gt=0)
    daily_requests: Optional[int] = Field(default=None, gt=0)
    global_daily_requests: Optional[int] = Field(default=None, gt=0)


class RateLimitsConfig(_Strict):
    login: WindowLimitConfig = WindowLimitConfig(
        requests=5, global_requests=20, window_seconds=900, concurrency=2
    )
    session_read: WindowLimitConfig = WindowLimitConfig(
        requests=120, global_requests=240, window_seconds=60, concurrency=16
    )
    broker_read: WindowLimitConfig = WindowLimitConfig(
        requests=30, global_requests=60, window_seconds=60, concurrency=4
    )
    mutation: WindowLimitConfig = WindowLimitConfig(
        requests=20, global_requests=40, window_seconds=60, concurrency=4
    )
    approval: WindowLimitConfig = WindowLimitConfig(
        requests=10, global_requests=20, window_seconds=300, concurrency=1
    )
    privileged: WindowLimitConfig = WindowLimitConfig(
        requests=5, global_requests=10, window_seconds=300, concurrency=1
    )
    chat: WindowLimitConfig = WindowLimitConfig(
        requests=10, global_requests=20, window_seconds=600, concurrency=1
    )
    analysis: WindowLimitConfig = WindowLimitConfig(
        requests=5, global_requests=10, window_seconds=600, concurrency=1
    )
    backtest: WindowLimitConfig = WindowLimitConfig(
        requests=2,
        global_requests=6,
        window_seconds=3600,
        concurrency=1,
        daily_requests=6,
        global_daily_requests=6,
    )
    provider_read: WindowLimitConfig = WindowLimitConfig(
        requests=180, global_requests=240, window_seconds=60, concurrency=8
    )
    panic: WindowLimitConfig = WindowLimitConfig(
        requests=60, global_requests=120, window_seconds=60, concurrency=1
    )


class ProviderPriceConfig(_Strict):
    model: str
    effective_date: date
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    source_url: AnyUrl


class ProviderBudgetConfig(_Strict):
    daily_calls: int = Field(default=100, gt=0)
    daily_input_tokens: int = Field(default=1_000_000, gt=0)
    daily_output_tokens: int = Field(default=200_000, gt=0)
    reservation_ttl_seconds: int = Field(default=300, gt=0)
    max_chat_tool_turns: int = Field(
        default=8,
        gt=0,
        le=8,
        description=(
            "Maximum provider turns and aggregate model tool calls per chat"
        ),
    )
    max_structured_attempts: int = Field(default=2, gt=0, le=2)
    backtest_llm_enabled: bool = False
    prices: dict[str, ProviderPriceConfig] = Field(default_factory=dict)


class BacktestLimitConfig(_Strict):
    runtime_seconds: int = Field(default=1_200, gt=0, le=1_200)
    max_symbols: int = Field(default=20, gt=0)
    max_calendar_days: int = Field(default=3_000, gt=0)


class RequestBoundsConfig(_Strict):
    default_body_bytes: int = Field(default=16_384, gt=0)
    chat_body_bytes: int = Field(default=32_768, gt=0)
    max_header_count: int = Field(default=64, gt=0)
    max_header_bytes: int = Field(default=16_384, gt=0)


class SecurityConfig(_Strict):
    session_hours: int = Field(default=8, gt=0)
    reauthentication_minutes: int = Field(default=5, gt=0)
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    provider_budget: ProviderBudgetConfig = Field(
        default_factory=ProviderBudgetConfig
    )
    backtest_limits: BacktestLimitConfig = Field(
        default_factory=BacktestLimitConfig
    )
    request_bounds: RequestBoundsConfig = Field(
        default_factory=RequestBoundsConfig
    )


class LLMConfig(_Strict):
    model: str                                   # anthropic model
    max_tokens: int = Field(gt=0)
    provider: str = "anthropic"                  # anthropic | gemini | groq
    # Retained as an explicit null-only compatibility field. Bootstrap and the
    # factory reject non-null values so financial context never crosses vendors.
    fallback_provider: Optional[str] = None
    gemini_model: str = "gemini-3.6-flash"
    groq_model: str = "llama-3.3-70b-versatile"
    request_timeout_seconds: float = Field(default=45.0, gt=0)


class DaemonConfig(_Strict):
    poll_interval_seconds: int = Field(gt=0)
    use_websocket: bool = True
    max_quote_age_seconds: float = Field(default=60.0, gt=0)  # staleness gate (A4)
    cycle_timeout_seconds: float = Field(default=90.0, gt=0)
    daily_task_timeout_seconds: float = Field(default=120.0, gt=0)
    heartbeat_stale_seconds: float = Field(
        default=180.0,
        gt=0,
        allow_inf_nan=False,
        strict=True,
    )


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


class ScreenerConfig(_Strict):
    universe: list[str] = Field(default_factory=list)  # empty -> use risk allowlist
    top_n: int = Field(default=10, gt=0)


class AnalystExtrasConfig(_Strict):
    news_enabled: bool = False
    version: str = "v2"              # tags graded calls; bump to reset the scorecard
    suppress_ranging: bool = True    # v2: force NO_TRADE in RANGING regimes


class AppConfig(_Strict):
    server: ServerConfig = Field(default_factory=ServerConfig)
    provider_origins: ProviderOriginsConfig = Field(
        default_factory=ProviderOriginsConfig
    )
    encryption: EncryptionConfig
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    trading: TradingConfig
    risk: RiskConfig
    features: FeaturesConfig
    llm: LLMConfig
    daemon: DaemonConfig
    # Phase 7 additions (optional so pre-Phase-7 configs still load).
    crypto_risk: Optional[RiskConfig] = None
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    screener: ScreenerConfig = Field(default_factory=ScreenerConfig)
    analyst: AnalystExtrasConfig = Field(default_factory=AnalystExtrasConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    @model_validator(mode="after")
    def _require_one_reviewed_price_for_selected_model(self) -> AppConfig:
        selected_models = {
            "anthropic": self.llm.model,
            "gemini": self.llm.gemini_model,
            "groq": self.llm.groq_model,
        }
        selected_model = selected_models.get(self.llm.provider)
        matches = [
            price
            for key, price in self.security.provider_budget.prices.items()
            if key.split(":", maxsplit=1)[0] == self.llm.provider
            and price.model == selected_model
        ]
        if len(matches) != 1:
            raise ValueError(
                "selected LLM provider/model must have exactly one reviewed price"
            )
        return self


# Source/test compatibility only. Runtime entry points move to SecretProvider
# in Task 2, after role-specific validation exists.
Secrets = RuntimeSecrets


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Parse and validate ``config.yaml``. Raises on unknown/invalid keys (A8)."""
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    return AppConfig.model_validate(raw)


def live_trading_enabled(config: AppConfig, secrets: RuntimeSecrets) -> bool:
    """Guardrail #1: both locks must be set, else we are NOT live."""
    return (
        config.trading.mode is TradingMode.LIVE
        and secrets.live_trading_confirm.get_secret_value() == LIVE_CONFIRM_STRING
    )
