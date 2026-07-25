"""Agentic loop with a scripted LLM backend (no API key)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from trading_assistant.app.agent import Agent
from trading_assistant.db.models import AuditEvent, LLMDecision


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool(id, name, inp):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=inp)


def _resp(stop_reason, content):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        model="mock",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class ScriptedBackend:
    """Returns a pre-scripted sequence of responses, one per create() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def create(self, *, system, messages, tools, tool_choice=None):
        self.calls += 1
        return self._responses.pop(0)


def _agent(make_service, responses):
    svc = make_service()
    backend = ScriptedBackend(responses)
    return Agent(backend, svc, svc.session_factory, model="mock", max_tokens=100), svc


def _chat(agent, message):
    return agent.chat(
        message,
        actor="operator:test",
        reason=message,
        request_id="agent-test-request",
    )


def test_agent_calls_tool_then_replies(make_service):
    agent, svc = _agent(
        make_service,
        [
            _resp("tool_use", [_tool("t1", "get_market_data", {"ticker": "AAPL"})]),
            _resp("end_turn", [_text("AAPL is trading at 100.")]),
        ],
    )
    out = _chat(agent, "what's AAPL at?")
    assert out["reply"] == "AAPL is trading at 100."
    assert out["tool_calls"][0]["name"] == "get_market_data"
    assert out["tool_calls"][0]["output"]["last"] == "100"


def test_agent_proposes_order_but_does_not_execute(make_service):
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool(
                        "t1",
                        "propose_order",
                        {
                            "ticker": "AAPL",
                            "side": "buy",
                            "order_type": "market",
                            "notional": "100",
                        },
                    )
                ],
            ),
            _resp("end_turn", [_text("Proposed. Awaiting your approval.")]),
        ],
    )
    out = _chat(agent, "buy $100 of AAPL")
    assert out["tool_calls"][0]["output"]["status"] == "proposed"
    assert out["tool_calls"][0]["output"]["executed"] is False
    assert svc.broker.submit_calls == 0
    assert len(svc.get_pending()) == 1


def test_agent_mutating_tools_preserve_operator_provenance(make_service):
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool(
                        "t1",
                        "propose_order",
                        {
                            "ticker": "AAPL",
                            "side": "buy",
                            "order_type": "market",
                            "notional": "100",
                        },
                    ),
                    _tool(
                        "t2",
                        "create_conditional_rule",
                        {
                            "ticker": "AAPL",
                            "condition": {"price_below": 90},
                            "action": {"side": "buy", "notional": "50"},
                        },
                    ),
                ],
            ),
            _resp(
                "tool_use",
                [_tool("t3", "cancel_rule", {"rule_id": 1})],
            ),
            _resp("end_turn", [_text("Mutations queued for human review.")]),
        ],
    )

    agent.chat(
        "Queue a $100 AAPL proposal and a $50 rule, then cancel the rule",
        actor="operator:local",
        reason="operator requested these exact chat mutations",
        request_id="chat-request-123",
    )

    with svc.session_factory() as session:
        audits = session.query(AuditEvent).filter(
            AuditEvent.action.in_(
                ("order.propose", "rule.create", "rule.cancel")
            )
        ).all()
    assert {audit.action for audit in audits} == {
        "order.propose",
        "rule.create",
        "rule.cancel",
    }
    assert {audit.actor for audit in audits} == {"operator:local"}
    assert {
        audit.reason for audit in audits
    } == {"operator requested these exact chat mutations"}
    assert {audit.request_id for audit in audits} == {"chat-request-123"}


def test_agent_tool_failure_does_not_return_raw_exception_text(
    make_service,
    caplog,
):
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool(
                        "t1",
                        "propose_order",
                        {
                            "ticker": "AAPL",
                            "side": "buy",
                            "order_type": "market",
                            "notional": "100",
                        },
                    )
                ],
            ),
            _resp("end_turn", [_text("Proposal failed safely.")]),
        ],
    )
    svc.propose_order = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("raw provider tool detail")
    )

    result = agent.chat(
        "Propose $100 of AAPL",
        actor="operator:local",
        reason="operator requested an AAPL proposal",
        request_id="chat-request-failure",
    )

    output = result["tool_calls"][0]["output"]
    assert output == {"error": "tool_failed"}
    assert "raw provider tool detail" not in str(result)
    assert "raw provider tool detail" not in caplog.text


def test_agent_records_decision(make_service):
    agent, svc = _agent(make_service, [_resp("end_turn", [_text("hello")])])
    _chat(agent, "hi")
    with svc.session_factory() as s:
        assert s.execute(select(func.count()).select_from(LLMDecision)).scalar_one() == 1


def test_agent_chat_survives_backend_error(make_service, caplog):
    """An LLM/provider error must not 500 the chat endpoint — return a graceful reply."""
    class BoomBackend:
        def create(self, *, system, messages, tools, tool_choice=None):
            raise RuntimeError("provider exploded")

    svc = make_service()
    agent = Agent(BoomBackend(), svc, svc.session_factory, model="mock", max_tokens=100)
    out = _chat(agent, "hi")
    assert out["tool_calls"] == []
    assert "couldn't complete" in out["reply"].lower()
    # Still records the (failed) decision — no crash on the None response.
    with svc.session_factory() as s:
        assert s.execute(select(func.count()).select_from(LLMDecision)).scalar_one() == 1
    assert "provider exploded" not in caplog.text


def test_agent_stops_at_max_turns(make_service):
    # Backend always asks for a tool -> loop must terminate at max_turns, not hang.
    responses = [
        _resp("tool_use", [_tool(f"t{i}", "get_account_summary", {})])
        for i in range(20)
    ]
    agent, svc = _agent(make_service, responses)
    agent.max_turns = 3
    out = _chat(agent, "loop forever")
    assert agent.backend.calls == 3
    assert out["reply"] == ""  # never produced final text, but returned cleanly
