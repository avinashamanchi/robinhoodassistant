"""Normalized LLM response shape + translators from our Anthropic-style
messages/tools to OpenAI (Groq) and Gemini formats.

Our internal message format (produced by app.agent / analyst) is Anthropic-shaped:
  messages: [{"role": "user"|"assistant", "content": str | list[block]}]
  block:    {"type":"text","text":...}
            {"type":"tool_use","id":...,"name":...,"input":{...}}
            {"type":"tool_result","tool_use_id":...,"content": "<json str>"}
  tools:    [{"name","description","input_schema": {json-schema}}]
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .budget import (
    ProviderBudgetService,
    ProviderInputEstimator,
)


# ── normalized response (what the agent/analyst consume) ────────
@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage | None = field(default_factory=Usage)
    model: str = ""


class BudgetedLLMBackend:
    """Reserve and settle durable provider budget around one raw attempt."""

    def __init__(
        self,
        delegate,
        budgets: ProviderBudgetService,
        *,
        provider: str,
        category: str,
        max_output_tokens: int,
        estimator: ProviderInputEstimator | None = None,
    ) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be non-empty")
        if not isinstance(category, str) or not category.strip():
            raise ValueError("category must be non-empty")
        if (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        if estimator is None:
            from .factory import resolve_input_estimator

            estimator = resolve_input_estimator(provider)
        self.delegate = delegate
        self.budgets = budgets
        self.provider = provider
        self.category = category
        self.max_output_tokens = max_output_tokens
        self.estimator = estimator

    def create(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str | None = None,
        request_id: str = "",
    ):
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("budgeted LLM calls require request_id")
        input_reservation = self.estimator.estimate_upper_bound(
            system=system,
            messages=messages,
            tools=tools,
        )
        reservation = self.budgets.reserve(
            provider=self.provider,
            category=self.category,
            request_id=request_id,
            input_tokens=input_reservation,
            output_tokens=self.max_output_tokens,
        )
        self.budgets.mark_started(reservation.reservation_id)
        try:
            response = self.delegate.create(
                system=system,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                request_id=request_id,
            )
        except Exception:
            self.budgets.mark_unknown(reservation.reservation_id)
            raise

        usage = getattr(response, "usage", None)
        if usage is None:
            self.budgets.mark_unknown(reservation.reservation_id)
            return response
        try:
            input_tokens = int(
                getattr(usage, "input_tokens", 0) or 0
            )
            output_tokens = int(
                getattr(usage, "output_tokens", 0) or 0
            )
            if input_tokens < 0 or output_tokens < 0:
                raise ValueError("provider usage must be non-negative")
        except (TypeError, ValueError, OverflowError):
            self.budgets.mark_unknown(reservation.reservation_id)
            return response
        self.budgets.settle(
            reservation.reservation_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return response


# ── OpenAI / Groq translation ───────────────────────────────────
def to_openai(system: str, messages: list[dict], tools: list[dict]) -> tuple[list[dict], list[dict]]:
    out: list[dict] = [{"role": "system", "content": system}]
    for msg in messages:
        role, content = msg["role"], msg["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        else:  # user: may carry tool_result blocks
            handled = False
            for b in content:
                if b.get("type") == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": b["tool_use_id"],
                            "content": b.get("content", ""),
                        }
                    )
                    handled = True
            texts = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            if texts or not handled:
                out.append({"role": "user", "content": texts})
    tools_oai = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]
    return out, tools_oai


def from_openai(resp: Any) -> LLMResponse:
    choice = resp.choices[0]
    msg = choice.message
    blocks: list = []
    if getattr(msg, "tool_calls", None):
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            blocks.append(ToolUseBlock(id=tc.id, name=tc.function.name, input=args))
        stop = "tool_use"
    else:
        blocks.append(TextBlock(text=msg.content or ""))
        stop = "end_turn"
    provider_usage = getattr(resp, "usage", None)
    return LLMResponse(
        content=blocks,
        stop_reason=stop,
        usage=(
            Usage(
                input_tokens=(
                    getattr(provider_usage, "prompt_tokens", 0) or 0
                ),
                output_tokens=(
                    getattr(provider_usage, "completion_tokens", 0) or 0
                ),
            )
            if provider_usage is not None
            else None
        ),
        model=getattr(resp, "model", ""),
    )


# ── Gemini translation ──────────────────────────────────────────
def to_gemini_contents(messages: list[dict]) -> list[dict]:
    """Return Gemini-style contents (dicts; the backend maps to SDK types).

    Builds a tool_use_id -> name map so tool_result blocks can be turned into
    function_response parts (Gemini keys those by function name).
    """
    id_to_name: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg["content"], list):
            for b in msg["content"]:
                if b.get("type") == "tool_use":
                    id_to_name[b["id"]] = b["name"]

    contents: list[dict] = []
    for msg in messages:
        role = "model" if msg["role"] == "assistant" else "user"
        content = msg["content"]
        parts: list[dict] = []
        if isinstance(content, str):
            parts.append({"text": content})
        else:
            for b in content:
                if b.get("type") == "text":
                    parts.append({"text": b["text"]})
                elif b.get("type") == "tool_use":
                    parts.append({"function_call": {"name": b["name"], "args": b["input"]}})
                elif b.get("type") == "tool_result":
                    name = id_to_name.get(b["tool_use_id"], "tool")
                    payload = b.get("content", "")
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        payload = {"result": payload}
                    parts.append(
                        {"function_response": {"name": name, "response": payload}}
                    )
        contents.append({"role": role, "parts": parts})
    return contents
