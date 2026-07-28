"""The agentic loop.

Flow: user message -> Claude (native tool use) -> intercept tool calls -> route
read-only queries or signed candidate drafts -> feed results back -> final text.
The LLM has no database-mutation or execution tool. Every decision is persisted
to ``llm_decisions``.

The Anthropic client is abstracted behind :class:`LLMBackend` so tests run a
scripted backend with no API key.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session, sessionmaker

from ..config import WindowLimitConfig
from ..db.models import LLMDecision
from ..identity import canonical_request_id
from ..security.sensitive_fields import sensitive_store
from ..security.candidates import (
    AgentReply,
    CandidateDraftService,
    CandidateError,
    SignedCandidate,
)
from ..service import TradingService
from .limits import (
    DurableRateLimiter,
    LimitSpec,
    LimitStoreUnavailable,
)

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a trading assistant. You can look up market data and account info, "
    "and you can DRAFT signed order or conditional-rule candidates for the "
    "operator to inspect. Drafting never queues, approves, or executes anything. "
    "Always size candidates explicitly with either quantity or notional, never "
    "both. Never claim that prose created a candidate."
)

# The general chat surface is read-only except for non-persisting draft tools.
READ_ONLY_TOOL_SPECS: tuple[dict[str, Any], ...] = (
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
        "name": "draft_order_candidate",
        "description": (
            "Draft a signed order candidate for explicit operator queueing. "
            "Does not persist or execute. Provide exactly one size form."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "order_type": {"type": "string", "enum": ["market", "limit"]},
                "quantity": {"type": "string"},
                "notional": {"type": "string"},
                "limit_price": {"type": "string"},
                "thesis": {"type": "string"},
            },
            "required": ["ticker", "side", "order_type", "thesis"],
            "additionalProperties": False,
        },
    },
    {
        "name": "draft_rule_candidate",
        "description": (
            "Draft a signed conditional-rule candidate for explicit operator "
            "queueing. This draft does not persist a rule."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "condition": {
                    "type": "object",
                    "properties": {
                        "comparator": {
                            "type": "string",
                            "enum": ["price_below", "price_above"],
                        },
                        "trigger_price": {"type": "string"},
                    },
                    "required": ["comparator", "trigger_price"],
                    "additionalProperties": False,
                },
                "action": {
                    "type": "object",
                    "properties": {
                        "side": {"type": "string", "enum": ["buy", "sell"]},
                        "order_type": {
                            "type": "string",
                            "enum": ["market", "limit"],
                        },
                        "quantity": {"type": "string"},
                        "notional": {"type": "string"},
                        "limit_price": {"type": "string"},
                    },
                    "required": ["side", "order_type"],
                    "additionalProperties": False,
                },
                "thesis": {"type": "string"},
            },
            "required": ["ticker", "condition", "action", "thesis"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_rules",
        "description": "List all standing conditional rules.",
        "input_schema": {"type": "object", "properties": {}},
    },
)

_BROKER_READ_TOOLS = frozenset(
    {
        "get_market_data",
        "get_account_summary",
        "draft_order_candidate",
        "draft_rule_candidate",
    }
)
_TOOL_STOP_REPLIES = {
    "tool_call_budget_exhausted": (
        "Tool execution stopped: tool_call_budget_exhausted."
    ),
    "broker_read_rate_limited": (
        "Tool execution stopped: broker_read_rate_limited."
    ),
    "broker_read_unavailable": (
        "Tool execution stopped: broker_read_unavailable."
    ),
}


class LLMBackend(Protocol):
    def create(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        request_id: str,
    ) -> Any: ...


class ToolRouter:
    """Maps a tool name + input to the corresponding TradingService method."""

    def __init__(
        self,
        service: TradingService,
        candidate_drafts: CandidateDraftService | None = None,
    ) -> None:
        self.service = service
        self.candidate_drafts = candidate_drafts

    @staticmethod
    def requires_broker_read(name: str) -> bool:
        return name in _BROKER_READ_TOOLS

    def dispatch(
        self,
        name: str,
        tool_input: dict[str, Any],
        *,
        actor: str,
        reason: str,
        request_id: str,
        session_binding: str,
    ) -> dict[str, Any]:
        s = self.service
        table = {
            "get_market_data": lambda: s.get_market_data(tool_input["ticker"]),
            "get_account_summary": lambda: s.get_account_summary(),
            "get_open_orders": lambda: {"orders": s.get_open_orders()},
            "get_order_status": lambda: (
                s.get_order_status(tool_input["order_id"]) or {"error": "not found"}
            ),
            "draft_order_candidate": lambda: self._draft(
                "order",
                tool_input,
                actor=actor,
                session_binding=session_binding,
            ),
            "draft_rule_candidate": lambda: self._draft(
                "rule",
                tool_input,
                actor=actor,
                session_binding=session_binding,
            ),
            "list_rules": lambda: {"rules": s.list_rules()},
        }
        if name not in table:
            return {"error": "unknown_tool"}
        try:
            return table[name]()
        except CandidateError as exc:
            return {"error": exc.code}
        except Exception:  # stable failure: never return provider/domain text
            log.error(
                "agent tool failed code=tool_failed tool=%s",
                name,
            )
            return {"error": "tool_failed"}

    def _draft(
        self,
        kind: Literal["order", "rule"],
        tool_input: dict[str, Any],
        *,
        actor: str,
        session_binding: str,
    ) -> dict[str, Any]:
        if self.candidate_drafts is None or not session_binding:
            raise CandidateError(
                "candidate_drafting_unavailable",
                status_code=503,
            )
        envelope = (
            self.candidate_drafts.draft_order(
                tool_input,
                actor=actor,
                session_binding=session_binding,
            )
            if kind == "order"
            else self.candidate_drafts.draft_rule(
                tool_input,
                actor=actor,
                session_binding=session_binding,
            )
        )
        return {"candidate": envelope.model_dump(mode="json")}


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
        *,
        max_turns: int,
        candidate_drafts: CandidateDraftService | None = None,
        rate_limiter: DurableRateLimiter,
        broker_read_limit: WindowLimitConfig,
    ) -> None:
        if (
            not isinstance(max_turns, int)
            or isinstance(max_turns, bool)
            or max_turns <= 0
        ):
            raise ValueError("max_turns must be a positive integer")
        self.backend = backend
        self.router = ToolRouter(service, candidate_drafts)
        self.session_factory = session_factory
        self.model = model
        self.max_tokens = max_tokens
        self.max_turns = max_turns
        # One reviewed bound limits both provider turns and the aggregate
        # number of model tool_use blocks across the whole chat.
        self.max_tool_calls = max_turns
        self.rate_limiter = rate_limiter
        self.broker_read_spec = LimitSpec(
            name="broker_read",
            principal_requests=broker_read_limit.requests,
            global_requests=broker_read_limit.global_requests,
            window_seconds=broker_read_limit.window_seconds,
            principal_daily_requests=broker_read_limit.daily_requests,
            global_daily_requests=(
                broker_read_limit.global_daily_requests
            ),
        )

    def chat(
        self,
        user_message: str,
        *,
        actor: str,
        reason: str,
        request_id: str,
        limit_principal: str,
        session_binding: str = "",
    ) -> AgentReply:
        actor = actor.strip()
        reason = reason.strip()
        request_id = canonical_request_id(request_id)
        if not actor or not reason:
            raise ValueError(
                "chat actor, reason, and request_id must be non-empty"
            )
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        tool_calls: list[dict[str, Any]] = []
        final_text = ""
        last_resp = None
        candidates: list[SignedCandidate] = []
        dispatched_tool_calls = 0

        for _ in range(self.max_turns):
            try:
                resp = self.backend.create(
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    tools=READ_ONLY_TOOL_SPECS,
                    request_id=request_id,
                )
            except Exception:  # noqa: BLE001 — never 500 the chat endpoint on an LLM error
                log.error("chat backend failed code=llm_backend_failed")
                final_text = (
                    "Sorry — I couldn't complete that request (the assistant model "
                    "returned an error). Please try rephrasing, or use the Plans page "
                    "to generate proposals directly."
                )
                break
            last_resp = resp
            messages.append(
                {"role": "assistant", "content": [_block_to_dict(b) for b in resp.content]}
            )

            if getattr(resp, "stop_reason", None) == "tool_use":
                results = []
                stop_code: str | None = None
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use":
                        if stop_code is not None:
                            output = {"error": stop_code}
                        elif dispatched_tool_calls >= self.max_tool_calls:
                            stop_code = "tool_call_budget_exhausted"
                            output = {"error": stop_code}
                        else:
                            dispatched_tool_calls += 1
                            output = None
                        is_draft = block.name in {
                            "draft_order_candidate",
                            "draft_rule_candidate",
                        }
                        if output is not None:
                            pass
                        elif is_draft and len(candidates) >= 4:
                            output = {"error": "candidate_limit_reached"}
                        else:
                            if self.router.requires_broker_read(
                                block.name
                            ):
                                try:
                                    broker_decision = (
                                        self.rate_limiter.consume_pair(
                                            self.broker_read_spec,
                                            principal=limit_principal,
                                        )
                                    )
                                except (
                                    LimitStoreUnavailable,
                                    ValueError,
                                ):
                                    stop_code = (
                                        "broker_read_unavailable"
                                    )
                                    output = {"error": stop_code}
                                else:
                                    if not broker_decision.allowed:
                                        stop_code = (
                                            "broker_read_rate_limited"
                                        )
                                        output = {"error": stop_code}
                            if output is None:
                                output = self.router.dispatch(
                                    block.name,
                                    dict(block.input),
                                    actor=actor,
                                    reason=reason,
                                    request_id=request_id,
                                    session_binding=session_binding,
                                )
                        raw_candidate = output.get("candidate")
                        if raw_candidate is not None:
                            try:
                                validated_candidate = (
                                    SignedCandidate.model_validate(
                                        raw_candidate
                                    )
                                )
                                candidates.append(validated_candidate)
                                output = {
                                    "status": "candidate_drafted",
                                    "kind": validated_candidate.kind,
                                }
                            except Exception:
                                output = {
                                    "error": "candidate_draft_invalid"
                                }
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
                if (
                    stop_code is None
                    and dispatched_tool_calls >= self.max_tool_calls
                ):
                    stop_code = "tool_call_budget_exhausted"
                if stop_code is not None:
                    final_text = _TOOL_STOP_REPLIES[stop_code]
                    break
                continue

            final_text = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
            break

        self._record(user_message, tool_calls, final_text, last_resp)
        return AgentReply(
            reply=final_text,
            candidates=tuple(candidates),
        )

    def _record(self, prompt, tool_calls, reply, resp) -> None:
        usage = getattr(resp, "usage", None)
        with self.session_factory() as s:
            row = LLMDecision(
                model=self.model,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
            )
            sensitive_store(s, self.session_factory).write_many(
                row,
                {
                    "prompt": prompt,
                    "tool_calls_json": json.dumps(
                        [{"name": t["name"], "input": t["input"]} for t in tool_calls]
                    ),
                    "reasoning_summary": (
                        reply[:2000] or "no final response"
                    ),
                },
            )
            s.commit()
