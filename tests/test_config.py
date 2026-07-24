"""A8: config fails fast on unknown/misspelled keys; live double-lock (guardrail #1)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_assistant.config import (
    LIVE_CONFIRM_STRING,
    Secrets,
    TradingMode,
    live_trading_enabled,
    load_config,
)

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
  model: claude-sonnet-4-6
  max_tokens: 4096
daemon:
  poll_interval_seconds: 15
  use_websocket: true
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


def test_deployed_config_keeps_automatic_execution_disabled():
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = load_config(config_path)

    assert cfg.features.auto_execute_preapproved_rules is False
    assert cfg.execution.prefer_bracket_orders is False


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
