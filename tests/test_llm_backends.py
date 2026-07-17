"""Provider backends: message/tool translation + response normalization + fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_assistant.llm.base import to_gemini_contents, to_openai
from trading_assistant.llm.factory import FallbackBackend
from trading_assistant.llm.gemini_backend import from_gemini, _sanitize_schema
from trading_assistant.llm.groq_backend import GroqBackend

TOOLS = [{"name": "propose_order", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


# ── OpenAI/Groq translation ─────────────────────────────────────
def test_to_openai_translates_tool_roundtrip():
    messages = [
        {"role": "user", "content": "buy aapl"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "c1", "name": "propose_order", "input": {"ticker": "AAPL"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "{\"status\":\"proposed\"}"},
        ]},
    ]
    out, tools = to_openai("system", messages, TOOLS)
    assert out[0] == {"role": "system", "content": "system"}
    assistant = next(m for m in out if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["name"] == "propose_order"
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"
    assert tools[0]["function"]["name"] == "propose_order"


class _FakeCompletions:
    def __init__(self, resp):
        self.resp = resp
        self.last = None

    def create(self, **kw):
        self.last = kw
        return self.resp


def _groq(resp):
    client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(resp)))
    return GroqBackend("k", "llama", client=client)


def test_groq_tool_call_normalized():
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(id="c1", function=SimpleNamespace(
                name="propose_order", arguments='{"ticker":"AAPL"}'))],
        ))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        model="llama",
    )
    backend = _groq(resp)
    out = backend.create(system="s", messages=[{"role": "user", "content": "hi"}], tools=TOOLS)
    assert out.stop_reason == "tool_use"
    assert out.content[0].type == "tool_use"
    assert out.content[0].name == "propose_order"
    assert out.content[0].input == {"ticker": "AAPL"}
    assert out.usage.input_tokens == 10


class _RaisingCompletions:
    """Simulates Groq raising BadRequestError(tool_use_failed) — the model tried to
    call a tool but emitted malformed <function=...> syntax the SDK rejects."""
    def __init__(self, err):
        self.err = err

    def create(self, **kw):
        raise self.err


def _groq_raising(err):
    client = SimpleNamespace(chat=SimpleNamespace(completions=_RaisingCompletions(err)))
    return GroqBackend("k", "llama", client=client)


class _FakeBadRequest(Exception):
    def __init__(self, body):
        super().__init__(str(body))
        self.body = body


def test_groq_recovers_malformed_tool_call():
    # Shape mirrors groq.BadRequestError: .body carries error.failed_generation.
    err = _FakeBadRequest({"error": {
        "code": "tool_use_failed",
        "failed_generation": '<function=propose_order [{"order_type": "market", "qty": "1", "side": "buy", "ticker": "AAPL"}] </function>',
    }})
    out = _groq_raising(err).create(system="s", messages=[{"role": "user", "content": "buy aapl"}], tools=TOOLS)
    assert out.stop_reason == "tool_use"
    assert out.content[0].name == "propose_order"
    assert out.content[0].input == {"order_type": "market", "qty": "1", "side": "buy", "ticker": "AAPL"}


def test_groq_unrecoverable_error_propagates():
    err = RuntimeError("network down")
    with pytest.raises(RuntimeError):
        _groq_raising(err).create(system="s", messages=[{"role": "user", "content": "hi"}], tools=TOOLS)


def test_groq_text_normalized():
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello", tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        model="llama",
    )
    out = _groq(resp).create(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert out.stop_reason == "end_turn"
    assert out.content[0].type == "text" and out.content[0].text == "hello"


# ── Gemini translation ──────────────────────────────────────────
def test_from_gemini_function_call():
    part = SimpleNamespace(function_call=SimpleNamespace(name="submit_analysis", args={"action": "buy"}), text=None)
    resp = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))],
        usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=2),
        model_version="gemini",
    )
    out = from_gemini(resp)
    assert out.stop_reason == "tool_use"
    assert out.content[0].name == "submit_analysis"
    assert out.content[0].input == {"action": "buy"}


def test_from_gemini_text():
    part = SimpleNamespace(function_call=None, text="hi there")
    resp = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))],
        usage_metadata=None, model_version="gemini",
    )
    out = from_gemini(resp)
    assert out.stop_reason == "end_turn" and out.content[0].text == "hi there"


def test_to_gemini_contents_maps_tool_result_name():
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c1", "name": "get_market_data", "input": {"ticker": "AAPL"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "{\"last\":\"100\"}"},
        ]},
    ]
    contents = to_gemini_contents(messages)
    assert contents[0]["role"] == "model"
    assert contents[0]["parts"][0]["function_call"]["name"] == "get_market_data"
    # tool_result mapped to the right function name via the id->name map.
    assert contents[1]["parts"][0]["function_response"]["name"] == "get_market_data"


def test_sanitize_schema_collapses_union():
    schema = {"type": "object", "properties": {"note": {"type": ["string", "null"]}}}
    out = _sanitize_schema(schema)
    assert out["properties"]["note"]["type"] == "string"


# ── fallback ────────────────────────────────────────────────────
def test_fallback_used_on_primary_error():
    class Boom:
        def create(self, **kw):
            raise RuntimeError("primary down")

    class OK:
        def create(self, **kw):
            return "fallback-result"

    fb = FallbackBackend(Boom(), OK())
    assert fb.create(system="", messages=[], tools=[]) == "fallback-result"


def test_fallback_when_primary_returns_no_tool_call_but_one_required():
    """Gemini can answer a 200 in prose even with tools; if a tool was REQUIRED,
    the fallback provider must take over (this was 'analyst did not submit a plan')."""
    from trading_assistant.llm.base import LLMResponse, TextBlock, ToolUseBlock

    class ProseOnly:  # primary: no tool_use block
        def create(self, **kw):
            return LLMResponse(content=[TextBlock(text="here is my prose answer")])

    class ToolOK:  # fallback: emits the required tool call
        def create(self, **kw):
            return LLMResponse(content=[ToolUseBlock(id="x", name="submit_plan", input={"a": 1})])

    fb = FallbackBackend(ProseOnly(), ToolOK())
    # No forcing -> prose is fine, primary result kept.
    r_auto = fb.create(system="", messages=[], tools=[{"name": "t"}])
    assert r_auto.content[0].type == "text"
    # Forced -> primary's prose is unacceptable, fall back to the tool-calling provider.
    r_forced = fb.create(system="", messages=[], tools=[{"name": "t"}], tool_choice="any")
    assert r_forced.content[0].type == "tool_use"
