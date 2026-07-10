"""Agentic loop with a scripted LLM backend (no API key)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from trading_assistant.app.agent import Agent
from trading_assistant.db.models import LLMDecision


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

    def create(self, *, system, messages, tools):
        self.calls += 1
        return self._responses.pop(0)


def _agent(make_service, responses):
    svc = make_service()
    backend = ScriptedBackend(responses)
    return Agent(backend, svc, svc.session_factory, model="mock", max_tokens=100), svc


def test_agent_calls_tool_then_replies(make_service):
    agent, svc = _agent(
        make_service,
        [
            _resp("tool_use", [_tool("t1", "get_market_data", {"ticker": "AAPL"})]),
            _resp("end_turn", [_text("AAPL is trading at 100.")]),
        ],
    )
    out = agent.chat("what's AAPL at?")
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
    out = agent.chat("buy $100 of AAPL")
    assert out["tool_calls"][0]["output"]["status"] == "proposed"
    assert out["tool_calls"][0]["output"]["executed"] is False
    assert svc.broker.submit_calls == 0
    assert len(svc.get_pending()) == 1


def test_agent_records_decision(make_service):
    agent, svc = _agent(make_service, [_resp("end_turn", [_text("hello")])])
    agent.chat("hi")
    with svc.session_factory() as s:
        assert s.execute(select(func.count()).select_from(LLMDecision)).scalar_one() == 1


def test_agent_stops_at_max_turns(make_service):
    # Backend always asks for a tool -> loop must terminate at max_turns, not hang.
    responses = [
        _resp("tool_use", [_tool(f"t{i}", "get_account_summary", {})])
        for i in range(20)
    ]
    agent, svc = _agent(make_service, responses)
    agent.max_turns = 3
    out = agent.chat("loop forever")
    assert agent.backend.calls == 3
    assert out["reply"] == ""  # never produced final text, but returned cleanly
