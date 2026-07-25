"""Smoke test the MCP wrapper: tools delegate to the configured TradingService."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_assistant.broker.mock import MockBroker
from trading_assistant.db.models import AuditEvent
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
    mcp_server._audit = None


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
    res = mcp_server.propose_order(
        "AAPL",
        "buy",
        "market",
        "MCP test proposal",
        notional="400",
    )
    assert res["status"] == "proposed"
    assert res["executed"] is False
    assert mcp_server.get_open_orders()[0]["status"] == "proposed"
    with configured.session_factory() as session:
        receipt = session.query(AuditEvent).filter_by(
            action="mcp.propose_order"
        ).one()
    assert receipt.actor == "assistant:mcp"
    assert receipt.request_id
    assert receipt.result_code == "proposed"


def test_mcp_proposal_success_survives_supplementary_audit_failure(
    configured,
    monkeypatch,
):
    class FailingBoundaryAudit:
        def record(self, *args, **kwargs):
            raise RuntimeError("supplementary MCP audit unavailable")

    monkeypatch.setattr(
        mcp_server,
        "_audit",
        FailingBoundaryAudit(),
    )

    result = mcp_server.propose_order(
        "AAPL",
        "buy",
        "market",
        "MCP boundary audit failure drill",
        notional="100",
    )

    assert result["status"] == "proposed"
    assert result["executed"] is False
    assert len(configured.get_pending()) == 1


def test_mcp_rule_mutation_receipts_preserve_channel_actor_and_request(
    configured,
):
    created = mcp_server.create_conditional_rule(
        "AAPL",
        {"price_below": "90"},
        {"side": "buy", "notional": "100"},
        "MCP rule provenance create",
    )
    canceled = mcp_server.cancel_rule(
        created["rule_id"],
        "MCP rule provenance cancel",
    )

    assert canceled["canceled"] is True
    with configured.session_factory() as session:
        receipts = (
            session.query(AuditEvent)
            .filter(
                AuditEvent.action.in_(
                    ("mcp.rule_create", "mcp.rule_cancel")
                )
            )
            .order_by(AuditEvent.id)
            .all()
        )
    assert [row.action for row in receipts] == [
        "mcp.rule_create",
        "mcp.rule_cancel",
    ]
    assert all(row.actor == "assistant:mcp" for row in receipts)
    assert all(row.request_id for row in receipts)
    assert all(row.latency_ms >= 0 for row in receipts)


@pytest.mark.parametrize(
    ("workflow", "success"),
    [
        ("create", True),
        ("create", False),
        ("cancel", True),
        ("cancel", False),
    ],
)
def test_mcp_rule_provenance_success_and_failure(
    configured,
    workflow,
    success,
):
    if workflow == "create":
        operation = lambda: mcp_server.create_conditional_rule(
            "AAPL",
            (
                {"price_below": "90"}
                if success
                else {"unsupported_condition": "90"}
            ),
            {"side": "buy", "notional": "100"},
            f"MCP create provenance {success}",
        )
        action = "mcp.rule_create"
    else:
        created = mcp_server.create_conditional_rule(
            "AAPL",
            {"price_below": "90"},
            {"side": "buy", "notional": "100"},
            "prepare MCP cancel provenance",
        )
        rule_id = created["rule_id"] if success else 999_999
        operation = lambda: mcp_server.cancel_rule(
            rule_id,
            f"MCP cancel provenance {success}",
        )
        action = "mcp.rule_cancel"

    if workflow == "create" and not success:
        with pytest.raises(ValueError):
            operation()
    else:
        result = operation()
        assert ("error" not in result) is success

    with configured.session_factory() as session:
        receipt = (
            session.query(AuditEvent)
            .filter_by(action=action)
            .order_by(AuditEvent.id.desc())
            .first()
        )
    assert receipt is not None
    assert receipt.actor == "assistant:mcp"
    assert receipt.request_id
    assert receipt.idempotency_key == ""
    assert receipt.result_code == (
        (
            "active"
            if workflow == "create"
            else "completed"
        )
        if success
        else "failed"
    )
