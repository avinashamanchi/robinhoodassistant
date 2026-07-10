"""The agentic loop.

Flow: user message -> Claude (native tool use) -> intercept tool calls -> route to
TradingService -> feed results back -> final text. The LLM only sees read-only +
propose + rule tools; it has NO execution tool. Every decision is persisted to
``llm_decisions``.

The Anthropic client is abstracted behind :class:`LLMBackend` so tests run a
scripted backend with no API key.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from ..db.models import LLMDecision
from ..service import TradingService

SYSTEM_PROMPT = (
    "You are a trading assistant. You can look up market data and account info, "
    "and you can PROPOSE orders and conditional rules. You can never execute a "
    "trade: proposing only queues an order for a human to approve. Always size "
    "orders explicitly with either qty (shares) or notional (USD), never both. "
    "Be concise and state clearly when you have created a proposal."
)

# Anthropic tool schemas. These mirror the MCP tools; the LLM sees no execute tool.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_market_data",
        "description": "Latest price, bid/ask, and day change for a ticker.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_account_summary",
        "description": "Buying power, equity, cash, and current positions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_open_orders",
        "description": "Orders still live (proposed/approved/submitted/partially filled).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_order_status",
        "description": "Full record for one order by local id.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "integer"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "propose_order",
        "description": (
            "Propose an order for human approval. Does NOT execute. Provide exactly "
            "one of qty or notional."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "order_type": {"type": "string", "enum": ["market", "limit"]},
                "qty": {"type": "string"},
                "notional": {"type": "string"},
                "limit_price": {"type": "string"},
            },
            "required": ["ticker", "side", "order_type"],
        },
    },
    {
        "name": "create_conditional_rule",
        "description": "Store a standing rule, e.g. condition {price_below: 175}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "condition": {"type": "object"},
                "action": {"type": "object"},
            },
            "required": ["ticker", "condition", "action"],
        },
    },
    {
        "name": "list_rules",
        "description": "List all standing conditional rules.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_rule",
        "description": "Cancel a standing conditional rule by id.",
        "input_schema": {
            "type": "object",
            "properties": {"rule_id": {"type": "integer"}},
            "required": ["rule_id"],
        },
    },
]


class LLMBackend(Protocol):
    def create(
        self, *, system: str, messages: list[dict], tools: list[dict]
    ) -> Any: ...


class ToolRouter:
    """Maps a tool name + input to the corresponding TradingService method."""

    def __init__(self, service: TradingService) -> None:
        self.service = service

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        s = self.service
        table = {
            "get_market_data": lambda: s.get_market_data(tool_input["ticker"]),
            "get_account_summary": lambda: s.get_account_summary(),
            "get_open_orders": lambda: {"orders": s.get_open_orders()},
            "get_order_status": lambda: (
                s.get_order_status(tool_input["order_id"]) or {"error": "not found"}
            ),
            "propose_order": lambda: s.propose_order(**tool_input),
            "create_conditional_rule": lambda: s.create_conditional_rule(
                tool_input["ticker"], tool_input["condition"], tool_input["action"]
            ),
            "list_rules": lambda: {"rules": s.list_rules()},
            "cancel_rule": lambda: s.cancel_rule(tool_input["rule_id"]),
        }
        if name not in table:
            return {"error": f"unknown tool {name}"}
        try:
            return table[name]()
        except Exception as exc:  # surface tool errors to the model, don't crash
            return {"error": f"{type(exc).__name__}: {exc}"}


def _block_to_dict(block: Any) -> dict[str, Any]:
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    return {"type": btype}


class Agent:
    def __init__(
        self,
        backend: LLMBackend,
        service: TradingService,
        session_factory: sessionmaker[Session],
        model: str,
        max_tokens: int,
        max_turns: int = 8,
    ) -> None:
        self.backend = backend
        self.router = ToolRouter(service)
        self.session_factory = session_factory
        self.model = model
        self.max_tokens = max_tokens
        self.max_turns = max_turns

    def chat(self, user_message: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        tool_calls: list[dict[str, Any]] = []
        final_text = ""
        last_resp = None

        for _ in range(self.max_turns):
            resp = self.backend.create(
                system=SYSTEM_PROMPT, messages=messages, tools=TOOL_SPECS
            )
            last_resp = resp
            messages.append(
                {"role": "assistant", "content": [_block_to_dict(b) for b in resp.content]}
            )

            if getattr(resp, "stop_reason", None) == "tool_use":
                results = []
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use":
                        output = self.router.dispatch(block.name, dict(block.input))
                        tool_calls.append(
                            {"name": block.name, "input": block.input, "output": output}
                        )
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(output),
                            }
                        )
                messages.append({"role": "user", "content": results})
                continue

            final_text = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
            break

        self._record(user_message, tool_calls, final_text, last_resp)
        return {"reply": final_text, "tool_calls": tool_calls}

    def _record(self, prompt, tool_calls, reply, resp) -> None:
        usage = getattr(resp, "usage", None)
        with self.session_factory() as s:
            s.add(
                LLMDecision(
                    prompt=prompt,
                    tool_calls_json=json.dumps(
                        [{"name": t["name"], "input": t["input"]} for t in tool_calls]
                    ),
                    reasoning_summary=reply[:2000],
                    model=self.model,
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                )
            )
            s.commit()


class AnthropicBackend:
    """Real backend. Lazily constructs the Anthropic client so tests never need a key."""

    def __init__(self, api_key: str, model: str, max_tokens: int) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def create(self, *, system: str, messages: list[dict], tools: list[dict]) -> Any:
        return self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
