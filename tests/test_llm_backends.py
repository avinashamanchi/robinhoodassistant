"""Provider backends: message/tool translation + response normalization + fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_assistant.llm.base import from_openai, to_gemini_contents, to_openai
from trading_assistant.llm.anthropic_backend import AnthropicBackend
from trading_assistant.llm.budget import BudgetLimits, ProviderBudgetService
from trading_assistant.llm.gemini_backend import GeminiBackend
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
    assert out.usage is None


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


def test_groq_accepts_request_id_without_sending_it_to_sdk():
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="hello", tool_calls=None,
        ))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        model="llama",
    )
    backend = _groq(resp)

    backend.create(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        request_id="request-groq",
    )

    assert "request_id" not in backend._client.chat.completions.last


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
    assert out.usage is None


def test_from_openai_preserves_missing_usage():
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="hello",
            tool_calls=None,
        ))],
        usage=None,
        model="llama",
    )

    out = from_openai(resp)

    assert out.usage is None


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


def test_gemini_accepts_request_id_without_sending_it_to_sdk():
    class Models:
        def __init__(self):
            self.last = None

        def generate_content(self, **kwargs):
            self.last = kwargs
            return SimpleNamespace(
                candidates=[],
                usage_metadata=None,
                model_version="gemini",
            )

    client = SimpleNamespace(models=Models())
    backend = GeminiBackend("key", "model", client=client)

    backend.create(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        request_id="request-gemini",
    )

    assert "request_id" not in client.models.last


def test_anthropic_accepts_request_id_without_sending_it_to_sdk(monkeypatch):
    import anthropic

    class Messages:
        def __init__(self):
            self.last = None

        def create(self, **kwargs):
            self.last = kwargs
            return SimpleNamespace(content=[], usage=None)

    client = SimpleNamespace(messages=Messages())
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_kwargs: client)
    backend = AnthropicBackend("key", "model", 100)

    backend.create(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        request_id="request-anthropic",
    )

    assert "request_id" not in client.messages.last


# ── explicit provider selection ────────────────────────────────
def test_configured_cross_provider_fallback_is_rejected_without_construction(
    app_config,
    session_factory,
    monkeypatch,
):
    from trading_assistant.config import Secrets
    from trading_assistant.llm import factory

    providers: list[str] = []
    monkeypatch.setattr(
        factory,
        "_make_backend",
        lambda provider, *_args: providers.append(provider) or object(),
    )
    configured = app_config.model_copy(
        update={
            "llm": app_config.llm.model_copy(
                update={"fallback_provider": "groq"}
            )
        }
    )

    with pytest.raises(RuntimeError, match="cross-provider"):
        factory.build_llm_backend(
            configured,
            Secrets(),
            provider_budget=ProviderBudgetService(
                session_factory,
                BudgetLimits(
                    calls=10,
                    input_tokens=100_000,
                    output_tokens=10_000,
                ),
            ),
            category="chat",
        )

    assert providers == []


def test_primary_provider_failure_is_not_sent_to_a_second_vendor(
    app_config,
    session_factory,
    monkeypatch,
):
    from trading_assistant.config import Secrets
    from trading_assistant.llm import factory

    class Primary:
        def create(self, **_kwargs):
            raise RuntimeError("primary unavailable")

    providers: list[str] = []
    monkeypatch.setattr(
        factory,
        "_make_backend",
        lambda provider, *_args: providers.append(provider) or Primary(),
    )
    configured = app_config.model_copy(
        update={
            "llm": app_config.llm.model_copy(
                update={"fallback_provider": None}
            )
        }
    )
    backend = factory.build_llm_backend(
        configured,
        Secrets(),
        provider_budget=ProviderBudgetService(
            session_factory,
            BudgetLimits(
                calls=10,
                input_tokens=100_000,
                output_tokens=10_000,
            ),
        ),
        category="chat",
    )

    with pytest.raises(RuntimeError, match="primary unavailable"):
        backend.create(
            system="system",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            request_id="primary-failure",
        )

    assert providers == [configured.llm.provider]


def test_groq_client_uses_configured_timeout(monkeypatch):
    import groq

    seen = {}
    monkeypatch.setattr(groq, "Groq", lambda **kwargs: seen.update(kwargs) or object())
    backend = GroqBackend("key", "model", timeout_seconds=17)
    backend._get_client()
    assert seen["timeout"] == 17


def test_anthropic_client_uses_configured_timeout(monkeypatch):
    import anthropic
    from trading_assistant.llm.anthropic_backend import AnthropicBackend

    seen = {}
    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **kwargs: seen.update(kwargs) or object()
    )
    AnthropicBackend(
        "key",
        "model",
        100,
        timeout_seconds=19,
    )._get_client()
    assert seen["timeout"] == 19


def test_gemini_client_uses_configured_timeout(monkeypatch):
    from google import genai
    from trading_assistant.llm.gemini_backend import GeminiBackend

    seen = {}
    monkeypatch.setattr(
        genai, "Client", lambda **kwargs: seen.update(kwargs) or object()
    )
    GeminiBackend("key", "model", timeout_seconds=23)._get_client()
    assert seen["http_options"].timeout == 23_000
