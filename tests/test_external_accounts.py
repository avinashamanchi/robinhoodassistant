"""Read-only Robinhood/external integration: no write surface, cross-broker
warning (non-blocking), cache TTL, graceful degradation, secret redaction."""

from __future__ import annotations

import pathlib
from decimal import Decimal

from trading_assistant.broker.mock import MockBroker
from trading_assistant.external_accounts.base import (
    ExternalAccountSource,
    ExternalPosition,
)
from trading_assistant.external_accounts.caching import CachingExternalSource
from trading_assistant.external_accounts.mock import MockExternalSource
from trading_assistant.risk.clock import FakeClock
from trading_assistant.service import TradingService

FORBIDDEN = [
    "submit_order", "place_order", "cancel_order", "order_buy", "order_sell",
    "order_crypto", "cancel_all", "build_order", "transfer", "watchlist_add",
]


def _svc(app_config, session_factory, ext, price_symbol="NVDA"):
    broker = MockBroker()
    broker.set_price(price_symbol, Decimal("100"))
    return TradingService(
        broker, session_factory, app_config, FakeClock(is_open=True), external_source=ext
    )


# ── no write surface (hard non-goal) ────────────────────────────
def test_no_write_methods_anywhere_in_package():
    import trading_assistant.external_accounts as pkg

    for path in pathlib.Path(pkg.__path__[0]).glob("*.py"):
        text = path.read_text()
        for name in FORBIDDEN:
            # Match calls/definitions ("name("), not prose in docstrings ("no transfers").
            assert f"{name}(" not in text, f"forbidden '{name}(' found in {path.name}"


def test_protocol_and_mock_have_no_order_methods():
    src = MockExternalSource()
    assert isinstance(src, ExternalAccountSource)
    for name in ["submit_order", "place_order", "cancel_order", "buy", "sell"]:
        assert not hasattr(src, name)


# ── cross-broker warning: fires, never blocks ───────────────────
def test_cross_broker_warning_fires_but_does_not_block(app_config, session_factory):
    ext = MockExternalSource(
        positions=[ExternalPosition("NVDA", Decimal("30"), Decimal("90"), Decimal("100"), "rh")]
    )  # external NVDA value = 3000 > max_position_per_ticker (2000)
    svc = _svc(app_config, session_factory, ext)
    res = svc.propose_order("NVDA", "buy", "market", notional="100")
    assert res["status"] == "proposed"          # NOT blocked
    assert res["approved_by_risk"] is True
    assert any("cross-broker" in w for w in res["risk_warnings"])


def test_no_warning_when_external_small(app_config, session_factory):
    ext = MockExternalSource(
        positions=[ExternalPosition("NVDA", Decimal("5"), Decimal("90"), Decimal("100"), "rh")]
    )  # external value 500; combined well under the 2000 limit
    svc = _svc(app_config, session_factory, ext)
    res = svc.propose_order("NVDA", "buy", "market", notional="100")
    assert res["risk_warnings"] == []


def test_external_positions_reach_snapshot(app_config, session_factory):
    ext = MockExternalSource()
    svc = _svc(app_config, session_factory, ext, price_symbol="AAPL")
    combined = svc.get_combined_holdings()
    assert combined["external_available"] is True
    assert combined["external"] and combined["external"][0]["read_only"] is True


# ── cache TTL ───────────────────────────────────────────────────
def test_cache_ttl_respected():
    inner = MockExternalSource()
    cached = CachingExternalSource(inner, ttl_seconds=100)
    cached.get_positions()
    cached.get_positions()
    assert inner.fetch_count == 1               # second served from cache

    inner2 = MockExternalSource()
    no_cache = CachingExternalSource(inner2, ttl_seconds=0)
    no_cache.get_positions()
    no_cache.get_positions()
    assert inner2.fetch_count == 2              # ttl 0 -> always refetch


# ── graceful degradation ────────────────────────────────────────
def test_degrades_when_source_down():
    cached = CachingExternalSource(MockExternalSource(fail=True), ttl_seconds=100)
    assert cached.get_positions() == []         # failure -> empty, no raise
    assert cached.stale is True
    assert cached.get_account_summary() is None


def test_service_still_trades_when_external_down(app_config, session_factory):
    ext = CachingExternalSource(MockExternalSource(fail=True), ttl_seconds=100)
    svc = _svc(app_config, session_factory, ext, price_symbol="AAPL")
    assert svc.get_external_positions()["positions"] == []
    # The trading path is unaffected by the external source being down.
    assert svc.propose_order("AAPL", "buy", "market", notional="100")["status"] == "proposed"


def test_no_external_source_is_available_false(app_config, session_factory):
    svc = _svc(app_config, session_factory, None, price_symbol="AAPL")
    assert svc.get_external_positions()["available"] is False


# ── secret redaction ────────────────────────────────────────────
def test_rh_secrets_are_redacted():
    from trading_assistant.config import Secrets
    from trading_assistant.logging import redact, register_all_secrets

    secrets = Secrets(
        rh_username="me@example.com", rh_password="SUP3RSECRET", rh_totp_secret="TOTPKEY123"
    )
    register_all_secrets(secrets)
    line = "login me@example.com pw=SUP3RSECRET totp=TOTPKEY123"
    out = redact(line)
    assert "SUP3RSECRET" not in out
    assert "TOTPKEY123" not in out
    assert "me@example.com" not in out
