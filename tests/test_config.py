"""A8: config fails fast on unknown/misspelled keys; live double-lock (guardrail #1)."""

from __future__ import annotations

import textwrap
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from trading_assistant.config import (
    LIVE_CONFIRM_STRING,
    Secrets,
    TradingMode,
    WindowLimitConfig,
    live_trading_enabled,
    load_config,
)
from trading_assistant.security.secrets import RuntimeSecrets

VALID = """
trading:
  mode: paper
  broker: mock
risk:
  ticker_allowlist: [AAPL, msft]
  max_notional_per_order: 500
  max_position_per_ticker: 2000
  max_portfolio_exposure: 10000
  daily_realized_loss_limit: 500
  price_sanity_pct: 5.0
  reject_when_market_closed: true
  proposal_ttl_minutes: 15
features:
  auto_execute_preapproved_rules: false
  telegram_notifications: false
llm:
  provider: gemini
  model: claude-sonnet-4-6
  gemini_model: gemini-3.6-flash
  max_tokens: 4096
daemon:
  poll_interval_seconds: 15
  use_websocket: true
security:
  provider_budget:
    prices:
      "gemini:gemini-3.6-flash":
        model: gemini-3.6-flash
        effective_date: 2026-07-09
        input_usd_per_million: 1.50
        output_usd_per_million: 7.50
        source_url: https://ai.google.dev/gemini-api/docs/latest-model
encryption:
  active_key_id: test-primary-key
"""

FLOAT_RISK_LIMITS = (
    "max_notional_per_order",
    "max_position_per_ticker",
    "max_portfolio_exposure",
    "daily_realized_loss_limit",
    "price_sanity_pct",
    "per_trade_risk_pct",
    "max_quote_age_seconds",
    "max_spread_pct",
    "max_daily_total_loss",
    "max_account_drawdown_pct",
)


def _write(tmp_path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text))
    return p


def _with_risk_limit(text: str, field: str, value: str) -> str:
    lines = textwrap.dedent(text).splitlines()
    replacement = f"  {field}: {value}"
    for index, line in enumerate(lines):
        if line.startswith(f"  {field}:"):
            lines[index] = replacement
            break
    else:
        proposal_index = lines.index("  proposal_ttl_minutes: 15")
        lines.insert(proposal_index, replacement)
    return "\n".join(lines) + "\n"


def test_valid_config_loads_and_normalizes(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.trading.mode is TradingMode.PAPER
    assert cfg.trading.request_timeout_seconds == 10.0
    assert cfg.trading.reconciliation_max_age_seconds == 300.0
    assert cfg.llm.request_timeout_seconds == 45.0
    # allowlist uppercased
    assert cfg.risk.ticker_allowlist == ["AAPL", "MSFT"]
    assert cfg.risk.proposal_ttl_minutes == 15
    assert cfg.daemon.cycle_timeout_seconds == 90.0
    assert cfg.daemon.daily_task_timeout_seconds == 120.0
    assert cfg.daemon.heartbeat_stale_seconds == 180.0
    assert cfg.risk.max_quote_age_seconds == 60.0
    assert cfg.risk.max_spread_pct == 1.0
    assert cfg.risk.max_daily_total_loss == 500.0
    assert cfg.risk.max_account_drawdown_pct == 10.0
    assert cfg.risk.require_broker_reconciled is True
    assert cfg.features.auto_execute_preapproved_rules is False
    assert cfg.execution.prefer_bracket_orders is False
    assert cfg.security.session_hours == 8
    assert cfg.security.reauthentication_minutes == 5
    assert cfg.server.secure_cookies is True


def test_deployed_config_keeps_automatic_execution_disabled():
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = load_config(config_path)

    assert cfg.features.auto_execute_preapproved_rules is False
    assert cfg.execution.prefer_bracket_orders is False
    assert cfg.security.session_hours == 8
    assert cfg.security.reauthentication_minutes == 5
    assert cfg.server.secure_cookies is True


def test_loopback_server_defaults_are_explicit(app_config):
    assert app_config.server.bind_host == "127.0.0.1"
    assert app_config.server.port == 8020
    assert str(app_config.server.origin) == "https://localhost:8020"
    assert app_config.server.allowed_hosts == [
        "localhost",
        "127.0.0.1",
        "::1",
    ]
    assert app_config.integrations.webhooks_enabled is False
    assert app_config.integrations.composio_enabled is False


def test_runtime_secrets_never_include_bind_or_provider_urls():
    names = set(RuntimeSecrets.model_fields)
    assert "app_host" not in names
    assert "app_port" not in names
    assert "alpaca_paper_base_url" not in names


def test_unknown_server_key_fails(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text())
    raw["server"]["trust_proxy_headers"] = True
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="trust_proxy_headers"):
        load_config(path)


def test_security_policy_defaults_are_explicit(app_config):
    security = app_config.security
    assert security.rate_limits.login == WindowLimitConfig(
        requests=5, global_requests=20, window_seconds=900, concurrency=2
    )
    assert security.rate_limits.backtest.daily_requests == 6
    assert security.rate_limits.backtest.global_daily_requests == 6
    assert security.provider_budget.daily_calls == 100
    assert security.provider_budget.daily_input_tokens == 1_000_000
    assert security.provider_budget.daily_output_tokens == 200_000
    assert security.provider_budget.max_chat_tool_turns == 8
    assert security.provider_budget.max_structured_attempts == 2
    price = security.provider_budget.prices["gemini:gemini-3.6-flash"]
    assert price.input_usd_per_million == Decimal("1.50")
    assert price.output_usd_per_million == Decimal("7.50")
    assert app_config.llm.gemini_model == "gemini-3.6-flash"
    assert security.backtest_limits.runtime_seconds == 1_200
    assert security.request_bounds.default_body_bytes == 16_384


def test_typo_in_nested_rate_limit_fails(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text())
    raw["security"]["rate_limits"]["chat"]["window_second"] = 600
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="window_second"):
        load_config(path)


def test_selected_model_without_price_record_fails_to_load(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text())
    raw["llm"]["gemini_model"] = "gemini-3.6-pro"
    path = tmp_path / "unpriced-model.yaml"
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="exactly one reviewed price"):
        load_config(path)


def test_duplicate_price_records_for_selected_model_fail_to_load(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text())
    prices = raw["security"]["provider_budget"]["prices"]
    prices["gemini:reviewed-duplicate"] = dict(
        prices["gemini:gemini-3.6-flash"]
    )
    path = tmp_path / "duplicate-price.yaml"
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="exactly one reviewed price"):
        load_config(path)


@pytest.mark.parametrize(
    "security",
    [
        "security:\n  session_hours: 0\n",
        "security:\n  reauthentication_minutes: 0\n",
        "security:\n  cookie_secure: false\n  misspelled: true\n",
    ],
)
def test_invalid_or_unknown_security_config_fails_closed(
    tmp_path, security
):
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, VALID + "\n" + security))


def test_typo_in_risk_key_fails_to_load(tmp_path):
    """A silently-ignored risk limit is the worst failure mode — it must raise."""
    typo = VALID.replace("max_notional_per_order", "max_notional_per_ordr")
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, typo))


@pytest.mark.parametrize("field", FLOAT_RISK_LIMITS)
def test_typo_in_float_risk_limit_fails_to_load(tmp_path, field):
    explicit = _with_risk_limit(VALID, field, "1")
    typo = explicit.replace(
        f"  {field}: 1",
        f"  {field}_typo: 1",
    )

    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, typo))


@pytest.mark.parametrize("field", FLOAT_RISK_LIMITS)
@pytest.mark.parametrize("yaml_value", [".inf", "-.inf", ".nan"])
def test_nonfinite_float_risk_limit_fails_to_load(
    tmp_path,
    field,
    yaml_value,
):
    with pytest.raises(ValidationError):
        load_config(
            _write(
                tmp_path,
                _with_risk_limit(VALID, field, yaml_value),
            )
        )


def test_unknown_top_level_section_fails(tmp_path):
    extra = VALID + "\nmystery:\n  foo: 1\n"
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, extra))


def test_non_positive_limit_rejected(tmp_path):
    bad = VALID.replace("max_notional_per_order: 500", "max_notional_per_order: 0")
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, bad))


@pytest.mark.parametrize(
    "old,new",
    [
        ("broker: mock", "broker: mock\n  request_timeout_seconds: 0"),
        ("max_tokens: 4096", "max_tokens: 4096\n  request_timeout_seconds: 0"),
        (
            "poll_interval_seconds: 15",
            "poll_interval_seconds: 15\n  heartbeat_stale_seconds: 0",
        ),
    ],
)
def test_non_positive_external_timeout_rejected(tmp_path, old, new):
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, VALID.replace(old, new)))


@pytest.mark.parametrize("yaml_value", [".nan", ".inf", "-.inf"])
def test_nonfinite_reconciliation_freshness_fails_to_load(
    tmp_path,
    yaml_value,
):
    invalid = VALID.replace(
        "broker: mock",
        (
            "broker: mock\n"
            f"  reconciliation_max_age_seconds: {yaml_value}"
        ),
    )

    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, invalid))


@pytest.mark.parametrize(
    "yaml_value",
    [
        ".nan",
        ".inf",
        "-.inf",
        "0",
        "-1",
        "true",
    ],
)
def test_invalid_daemon_heartbeat_stale_threshold_fails_to_load(
    tmp_path,
    yaml_value,
):
    invalid = VALID.replace(
        "poll_interval_seconds: 15",
        (
            "poll_interval_seconds: 15\n"
            f"  heartbeat_stale_seconds: {yaml_value}"
        ),
    )

    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, invalid))


def test_unknown_daemon_heartbeat_setting_remains_forbidden(tmp_path):
    invalid = VALID.replace(
        "poll_interval_seconds: 15",
        (
            "poll_interval_seconds: 15\n"
            "  heartbeat_stale_second: 180"
        ),
    )

    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, invalid))


def test_live_double_lock(tmp_path):
    cfg = load_config(_write(tmp_path, VALID.replace("mode: paper", "mode: live")))
    # Missing confirmation string -> still not live.
    assert live_trading_enabled(cfg, Secrets(live_trading_confirm="")) is False
    assert live_trading_enabled(cfg, Secrets(live_trading_confirm="wrong")) is False
    # Both locks set -> live.
    assert live_trading_enabled(cfg, Secrets(live_trading_confirm=LIVE_CONFIRM_STRING))


def test_paper_never_live_even_with_confirm(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))  # mode: paper
    assert live_trading_enabled(cfg, Secrets(live_trading_confirm=LIVE_CONFIRM_STRING)) is False
