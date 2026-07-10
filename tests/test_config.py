"""A8: config fails fast on unknown/misspelled keys; live double-lock (guardrail #1)."""

from __future__ import annotations

import textwrap

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


def _write(tmp_path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text))
    return p


def test_valid_config_loads_and_normalizes(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.trading.mode is TradingMode.PAPER
    # allowlist uppercased
    assert cfg.risk.ticker_allowlist == ["AAPL", "MSFT"]
    assert cfg.risk.proposal_ttl_minutes == 15


def test_typo_in_risk_key_fails_to_load(tmp_path):
    """A silently-ignored risk limit is the worst failure mode — it must raise."""
    typo = VALID.replace("max_notional_per_order", "max_notional_per_ordr")
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, typo))


def test_unknown_top_level_section_fails(tmp_path):
    extra = VALID + "\nmystery:\n  foo: 1\n"
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, extra))


def test_non_positive_limit_rejected(tmp_path):
    bad = VALID.replace("max_notional_per_order: 500", "max_notional_per_order: 0")
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, bad))


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
