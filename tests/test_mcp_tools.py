"""Smoke test the MCP wrapper: tools delegate to the configured TradingService."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_assistant.broker.mock import MockBroker
from trading_assistant.mcp_server import server as mcp_server
from trading_assistant.risk.clock import FakeClock
from trading_assistant.service import TradingService


@pytest.fixture
def configured(app_config, session_factory):
    broker = MockBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = TradingService(broker, session_factory, app_config, FakeClock(is_open=True))
    mcp_server.configure(svc)
    yield svc
    mcp_server._service = None  # reset global so other tests aren't affected


def test_tools_are_registered():
    names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert {
        "get_market_data",
        "get_account_summary",
        "get_open_orders",
        "get_order_status",
        "propose_order",
        "create_conditional_rule",
        "list_rules",
        "cancel_rule",
    } <= names


def test_get_market_data_tool(configured):
    assert mcp_server.get_market_data("AAPL")["last"] == "100"


def test_propose_order_tool_creates_pending(configured):
    res = mcp_server.propose_order("AAPL", "buy", "market", notional="400")
    assert res["status"] == "proposed"
    assert res["executed"] is False
    assert mcp_server.get_open_orders()[0]["status"] == "proposed"
