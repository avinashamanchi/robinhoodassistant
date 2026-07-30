"""Agentic loop with a scripted LLM backend (no API key)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
from threading import Lock
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from trading_assistant.app.agent import Agent, READ_ONLY_TOOL_SPECS
from trading_assistant.app.limits import (
    DurableRateLimiter,
    LimitStoreUnavailable,
)
from trading_assistant.db.models import (
    AuditEvent,
    LLMDecision,
    Order,
    RateWindow,
    Rule,
)
from trading_assistant.security.candidates import (
    CandidateDraftService,
    CandidateSigner,
)
from tests.conftest import decrypt_test_sensitive


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
        self.request_ids: list[str] = []
        self.message_snapshots: list[list[dict]] = []

    def create(
        self,
        *,
        system,
        messages,
        tools,
        tool_choice=None,
        request_id,
    ):
        self.calls += 1
        self.request_ids.append(request_id)
        self.message_snapshots.append(deepcopy(messages))
        return self._responses.pop(0)


def _agent_for_service(
    svc,
    responses,
    *,
    max_turns=8,
    rate_limiter=None,
    broker_read_limit=None,
):
    backend = ScriptedBackend(responses)
    signer = CandidateSigner(b"k" * 32)
    drafts = CandidateDraftService(svc, signer)
    agent = Agent(
        backend,
        svc,
        svc.session_factory,
        model="mock",
        max_tokens=100,
        max_turns=max_turns,
        candidate_drafts=drafts,
        rate_limiter=rate_limiter or DurableRateLimiter(
            svc.session_factory
        ),
        broker_read_limit=(
            broker_read_limit
            or svc.config.security.rate_limits.broker_read
        ),
    )
    agent.test_session_binding = signer.session_binding(
        actor="operator:test",
        session_id=7,
        authenticated_at=datetime.now(timezone.utc),
    )
    return agent, svc


def _agent(make_service, responses, *, max_turns=8):
    return _agent_for_service(
        make_service(),
        responses,
        max_turns=max_turns,
    )


def _chat(
    agent,
    message,
    *,
    limit_principal="session:7:operator:test",
):
    return agent.chat(
        message,
        actor="operator:test",
        reason=message,
        request_id="agent-test-request",
        session_binding=getattr(agent, "test_session_binding", ""),
        limit_principal=limit_principal,
    )


def _first_tool_output(backend: ScriptedBackend) -> dict:
    raw = backend.message_snapshots[1][-1]["content"][0]["content"]
    return json.loads(raw)


def test_agent_calls_tool_then_replies(make_service):
    agent, svc = _agent(
        make_service,
        [
            _resp("tool_use", [_tool("t1", "get_market_data", {"ticker": "AAPL"})]),
            _resp("end_turn", [_text("AAPL is trading at 100.")]),
        ],
    )
    out = _chat(agent, "what's AAPL at?")
    assert out.reply == "AAPL is trading at 100."
    assert out.candidates == ()
    assert _first_tool_output(agent.backend)["last"] == "100"
    assert agent.backend.request_ids == [
        "agent-test-request",
        "agent-test-request",
    ]


def test_agent_uses_one_normalized_request_id_for_provider_turns_and_audit(
    make_service,
):
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool(
                        "t1",
                        "get_market_data",
                        {"ticker": "AAPL"},
                    )
                ],
            ),
            _resp("end_turn", [_text("Read-only lookup complete.")]),
        ],
    )

    agent.chat(
        "Propose $100 of AAPL",
        actor="operator:test",
        reason="capture stable parent identity",
        request_id="  http-request-123  ",
        limit_principal="session:7:operator:test",
    )

    assert agent.backend.request_ids == [
        "http-request-123",
        "http-request-123",
    ]
    with svc.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AuditEvent)
        ) == 0


@pytest.mark.parametrize(
    "request_id",
    [
        None,
        "",
        " ",
        "a" * 65,
        "request id",
        "request\nid",
        "reque\u0301st",
        "request-😀",
    ],
    ids=[
        "non-string",
        "empty",
        "blank",
        "too-long",
        "internal-space",
        "control",
        "nfd-unicode",
        "emoji",
    ],
)
def test_agent_rejects_noncanonical_request_id_before_backend_or_audit(
    make_service,
    request_id,
):
    agent, svc = _agent(
        make_service,
        [_resp("end_turn", [_text("must not run")])],
    )

    with pytest.raises(ValueError, match="request_id"):
        agent.chat(
            "hello",
            actor="operator:test",
            reason="reject invalid request identity",
            request_id=request_id,
            limit_principal="session:7:operator:test",
        )

    assert agent.backend.calls == 0
    with svc.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AuditEvent)
        ) == 0


def test_agent_accepts_full_safe_request_id_character_set_at_64_characters(
    make_service,
):
    request_id = "AZaz09._:-" + ("r" * 54)
    agent, _svc = _agent(
        make_service,
        [_resp("end_turn", [_text("ok")])],
    )

    _chat_result = agent.chat(
        "hello",
        actor="operator:test",
        reason="accept maximum request identity",
        request_id=request_id,
        limit_principal="session:7:operator:local",
    )

    assert len(request_id) == 64
    assert agent.backend.request_ids == [request_id]


def test_agent_module_does_not_expose_a_raw_anthropic_backend():
    from trading_assistant.app import agent as agent_module

    assert not hasattr(agent_module, "AnthropicBackend")


def test_general_chat_surface_has_no_mutating_tools():
    assert isinstance(READ_ONLY_TOOL_SPECS, tuple)
    names = {tool["name"] for tool in READ_ONLY_TOOL_SPECS}

    assert {
        "propose_order",
        "create_conditional_rule",
        "cancel_rule",
    }.isdisjoint(names)
    assert {
        "draft_order_candidate",
        "draft_rule_candidate",
    }.issubset(names)
    server_fields = {
        "reference_price",
        "quote_as_of",
        "actor",
        "session_binding",
        "issued_at",
        "expires_at",
        "nonce",
        "signature",
    }
    for tool in READ_ONLY_TOOL_SPECS:
        if tool["name"].startswith("draft_"):
            assert server_fields.isdisjoint(
                tool["input_schema"]["properties"]
            )


def test_agent_drafts_signed_order_without_persisting_or_executing(make_service):
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool(
                        "t1",
                        "draft_order_candidate",
                        {
                            "ticker": "AAPL",
                            "side": "buy",
                            "order_type": "market",
                            "notional": "100",
                            "thesis": "operator should inspect this draft",
                        },
                    )
                ],
            ),
            _resp("end_turn", [_text("Drafted for your inspection.")]),
        ],
    )
    out = _chat(agent, "buy $100 of AAPL")
    assert out.reply == "Drafted for your inspection."
    assert len(out.candidates) == 1
    assert out.candidates[0].kind == "order"
    assert out.candidates[0].payload.notional == 100
    assert _first_tool_output(agent.backend) == {
        "status": "candidate_drafted",
        "kind": "order",
    }
    assert "signature" not in json.dumps(
        agent.backend.message_snapshots[1]
    )
    assert svc.broker.submit_calls == 0
    with svc.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(Rule)) == 0


def test_agent_rejects_removed_mutation_tools_without_state_change(make_service):
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
            _resp("end_turn", [_text("No mutation was performed.")]),
        ],
    )

    out = _chat(agent, "try a removed mutation")

    with svc.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(Rule)) == 0
        assert session.scalar(
            select(func.count()).select_from(AuditEvent)
        ) == 0
    assert out.candidates == ()
    assert _first_tool_output(agent.backend) == {"error": "unknown_tool"}
    assert svc.broker.submit_calls == 0


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
                        "get_market_data",
                        {"ticker": "AAPL"},
                    )
                ],
            ),
            _resp("end_turn", [_text("Lookup failed safely.")]),
        ],
    )
    svc.get_market_data = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("raw provider tool detail")
    )

    result = agent.chat(
        "Look up AAPL",
        actor="operator:local",
        reason="operator requested an AAPL lookup",
        request_id="chat-request-failure",
        limit_principal="session:7:operator:test",
    )

    output = _first_tool_output(agent.backend)
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
        def create(
            self,
            *,
            system,
            messages,
            tools,
            tool_choice=None,
            request_id,
        ):
            raise RuntimeError("provider exploded")

    svc = make_service()
    agent = Agent(
        BoomBackend(),
        svc,
        svc.session_factory,
        model="mock",
        max_tokens=100,
        max_turns=8,
        rate_limiter=DurableRateLimiter(svc.session_factory),
        broker_read_limit=svc.config.security.rate_limits.broker_read,
    )
    out = _chat(agent, "hi")
    assert out.candidates == ()
    assert "couldn't complete" in out.reply.lower()
    # Still records the (failed) decision — no crash on the None response.
    with svc.session_factory() as s:
        assert s.execute(select(func.count()).select_from(LLMDecision)).scalar_one() == 1
    assert "provider exploded" not in caplog.text


def test_agent_stops_at_max_turns(make_service):
    # Backend always asks for a tool -> terminate locally at the shared bound.
    responses = [
        _resp("tool_use", [_tool(f"t{i}", "get_account_summary", {})])
        for i in range(20)
    ]
    agent, svc = _agent(make_service, responses, max_turns=3)
    out = _chat(agent, "loop forever")
    assert agent.backend.calls == 3
    assert out.reply == "Tool execution stopped: tool_call_budget_exhausted."


def test_prose_only_model_mention_does_not_create_candidate(make_service):
    agent, svc = _agent(
        make_service,
        [_resp("end_turn", [_text("I could draft an AAPL order.")])],
    )

    out = _chat(agent, "discuss an AAPL order")

    assert out.candidates == ()
    with svc.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(Rule)) == 0


def test_agent_caps_candidates_before_fifth_quote_read(make_service):
    draft = {
        "ticker": "AAPL",
        "side": "buy",
        "order_type": "market",
        "notional": "100",
        "thesis": "bounded draft",
    }
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool(f"t{index}", "draft_order_candidate", draft)
                    for index in range(5)
                ],
            ),
            _resp("end_turn", [_text("Four drafts returned.")]),
        ],
    )
    quote_calls = 0
    original_get_quote = svc.broker.get_quote

    def counted_get_quote(ticker):
        nonlocal quote_calls
        quote_calls += 1
        return original_get_quote(ticker)

    svc.broker.get_quote = counted_get_quote

    out = _chat(agent, "draft at most four")

    assert len(out.candidates) == 4
    assert quote_calls == 4
    with svc.session_factory() as session:
        assert sorted(
            session.scalars(
                select(RateWindow.hits).where(
                    RateWindow.policy_name == "broker_read"
                )
            )
        ) == [4, 4]


def test_agent_hard_caps_48_tool_blocks_across_one_model_response(
    make_service,
):
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool(
                        f"tool-{index}",
                        "get_market_data",
                        {"ticker": "AAPL"},
                    )
                    for index in range(48)
                ],
            )
        ],
        max_turns=8,
    )
    quote_calls = 0
    original_get_quote = svc.broker.get_quote

    def counted_get_quote(ticker):
        nonlocal quote_calls
        quote_calls += 1
        return original_get_quote(ticker)

    svc.broker.get_quote = counted_get_quote

    reply = _chat(agent, "fan out")

    assert quote_calls == 8
    assert agent.backend.calls == 1
    assert reply.reply == "Tool execution stopped: tool_call_budget_exhausted."
    with svc.session_factory() as session:
        decision = session.scalar(select(LLMDecision))
        calls = json.loads(
            decrypt_test_sensitive(decision, "tool_calls_json")
        )
        assert len(calls) == 48


def test_agent_exact_tool_budget_returns_without_second_provider_call(
    make_service,
):
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool(
                        f"tool-{index}",
                        "get_market_data",
                        {"ticker": "AAPL"},
                    )
                    for index in range(3)
                ],
            )
        ],
        max_turns=3,
    )
    quote_calls = 0
    original_get_quote = svc.broker.get_quote

    def counted_get_quote(ticker):
        nonlocal quote_calls
        quote_calls += 1
        return original_get_quote(ticker)

    svc.broker.get_quote = counted_get_quote

    reply = _chat(agent, "exactly exhaust the tool budget")

    assert quote_calls == 3
    assert agent.backend.calls == 1
    assert reply.reply == "Tool execution stopped: tool_call_budget_exhausted."


def test_agent_tool_budget_is_aggregate_across_model_turns(make_service):
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool(
                        f"first-{index}",
                        "get_market_data",
                        {"ticker": "AAPL"},
                    )
                    for index in range(5)
                ],
            ),
            _resp(
                "tool_use",
                [
                    _tool(
                        f"second-{index}",
                        "get_market_data",
                        {"ticker": "AAPL"},
                    )
                    for index in range(5)
                ],
            ),
        ],
        max_turns=8,
    )
    quote_calls = 0
    original_get_quote = svc.broker.get_quote

    def counted_get_quote(ticker):
        nonlocal quote_calls
        quote_calls += 1
        return original_get_quote(ticker)

    svc.broker.get_quote = counted_get_quote

    reply = _chat(agent, "cross-turn fan out")

    assert quote_calls == 8
    assert agent.backend.calls == 2
    assert reply.reply == "Tool execution stopped: tool_call_budget_exhausted."


def test_each_broker_backed_tool_consumes_one_durable_read(
    make_service,
):
    draft = {
        "ticker": "AAPL",
        "side": "buy",
        "order_type": "market",
        "notional": "100",
        "thesis": "bounded broker read",
    }
    agent, svc = _agent(
        make_service,
        [
            _resp(
                "tool_use",
                [
                    _tool("market", "get_market_data", {"ticker": "AAPL"}),
                    _tool("account", "get_account_summary", {}),
                    _tool("draft", "draft_order_candidate", draft),
                ],
            ),
            _resp("end_turn", [_text("done")]),
        ],
    )

    reply = _chat(agent, "three broker-backed tools")

    assert reply.reply == "done"
    with svc.session_factory() as session:
        assert sorted(
            session.scalars(
                select(RateWindow.hits).where(
                    RateWindow.policy_name == "broker_read"
                )
            )
        ) == [3, 3]


def test_broker_read_limits_isolate_authenticated_session_principals(
    make_service,
):
    svc = make_service()
    limiter = DurableRateLimiter(svc.session_factory)
    first, _ = _agent_for_service(
        svc,
        [
            _resp(
                "tool_use",
                [_tool("first", "get_market_data", {"ticker": "AAPL"})],
            ),
            _resp("end_turn", [_text("first done")]),
        ],
        rate_limiter=limiter,
    )
    second, _ = _agent_for_service(
        svc,
        [
            _resp(
                "tool_use",
                [_tool("second", "get_market_data", {"ticker": "AAPL"})],
            ),
            _resp("end_turn", [_text("second done")]),
        ],
        rate_limiter=limiter,
    )

    _chat(first, "first", limit_principal="session:11:operator:test")
    _chat(second, "second", limit_principal="session:12:operator:test")

    with svc.session_factory() as session:
        assert sorted(
            session.scalars(
                select(RateWindow.hits).where(
                    RateWindow.policy_name == "broker_read"
                )
            )
        ) == [1, 1, 2]


def test_concurrent_chats_share_atomic_broker_read_capacity(make_service):
    svc = make_service()
    limiter = DurableRateLimiter(svc.session_factory)
    broker_limit = (
        svc.config.security.rate_limits.broker_read.model_copy(
            update={
                "requests": 5,
                "global_requests": 5,
                "window_seconds": 60,
            }
        )
    )
    agents = [
        _agent_for_service(
            svc,
            [
                _resp(
                    "tool_use",
                    [
                        _tool(
                            f"{chat_index}-{tool_index}",
                            "get_market_data",
                            {"ticker": "AAPL"},
                        )
                        for tool_index in range(4)
                    ],
                ),
                _resp("end_turn", [_text("done")]),
            ],
            rate_limiter=limiter,
            broker_read_limit=broker_limit,
        )[0]
        for chat_index in range(2)
    ]
    quote_calls = 0
    quote_lock = Lock()
    original_get_quote = svc.broker.get_quote

    def counted_get_quote(ticker):
        nonlocal quote_calls
        with quote_lock:
            quote_calls += 1
        return original_get_quote(ticker)

    svc.broker.get_quote = counted_get_quote

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(
                lambda agent: _chat(
                    agent,
                    "concurrent",
                    limit_principal="session:21:operator:test",
                ),
                agents,
            )
        )

    assert quote_calls == 5
    assert sorted(agent.backend.calls for agent in agents) in (
        [1, 1],
        [1, 2],
    )
    reply_texts = {reply.reply for reply in replies}
    assert reply_texts <= {
        "done",
        "Tool execution stopped: broker_read_rate_limited.",
    }
    assert "Tool execution stopped: broker_read_rate_limited." in reply_texts


def test_broker_read_denial_stops_remaining_tools_without_broker_call(
    make_service,
):
    svc = make_service()
    limiter = DurableRateLimiter(svc.session_factory)
    broker_limit = (
        svc.config.security.rate_limits.broker_read.model_copy(
            update={
                "requests": 1,
                "global_requests": 1,
                "window_seconds": 60,
            }
        )
    )
    from trading_assistant.app.limits import LimitSpec

    limiter.consume_pair(
        LimitSpec(
            name="broker_read",
            principal_requests=1,
            global_requests=1,
            window_seconds=60,
        ),
        principal="session:31:operator:test",
    )
    agent, _ = _agent_for_service(
        svc,
        [
            _resp(
                "tool_use",
                [
                    _tool("one", "get_market_data", {"ticker": "AAPL"}),
                    _tool("two", "get_account_summary", {}),
                ],
            )
        ],
        rate_limiter=limiter,
        broker_read_limit=broker_limit,
    )
    svc.broker.get_quote = lambda *_args, **_kwargs: pytest.fail(
        "rate denial must precede the broker"
    )
    svc.broker.get_account = lambda *_args, **_kwargs: pytest.fail(
        "rate denial must stop remaining broker tools"
    )

    reply = _chat(
        agent,
        "denied",
        limit_principal="session:31:operator:test",
    )

    assert agent.backend.calls == 1
    assert reply.reply == "Tool execution stopped: broker_read_rate_limited."


def test_broker_read_store_failure_stops_without_broker_call(
    make_service,
):
    class BrokenLimiter:
        def consume_pair(self, *_args, **_kwargs):
            raise LimitStoreUnavailable("offline")

    svc = make_service()
    agent, _ = _agent_for_service(
        svc,
        [
            _resp(
                "tool_use",
                [
                    _tool("one", "get_market_data", {"ticker": "AAPL"}),
                    _tool("two", "get_account_summary", {}),
                ],
            )
        ],
        rate_limiter=BrokenLimiter(),
    )
    svc.broker.get_quote = lambda *_args, **_kwargs: pytest.fail(
        "store failure must precede the broker"
    )
    svc.broker.get_account = lambda *_args, **_kwargs: pytest.fail(
        "store failure must stop remaining broker tools"
    )

    reply = _chat(agent, "store unavailable")

    assert agent.backend.calls == 1
    assert reply.reply == "Tool execution stopped: broker_read_unavailable."
