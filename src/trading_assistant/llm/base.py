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

from ..identity import canonical_request_id
from .budget import (
    ProviderBudgetService,
    ProviderInputEstimator,
)
from .payloads import (
    to_gemini_contents,
    to_openai,
    validate_llm_payload,
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
        self.__delegate = delegate
        self.budgets = budgets
        self.provider = provider
        self.category = category
        self.max_output_tokens = max_output_tokens
        self.estimator = estimator

    def _reconcile_unknown_after_failure(
        self,
        reservation_id: str,
        original_error: BaseException,
    ) -> None:
        try:
            self.budgets.mark_unknown(reservation_id)
        except Exception:
            try:
                original_error.add_note(
                    "provider reservation reconciliation failed"
                )
            except Exception:
                pass

    def create(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str | None = None,
        request_id: str,
    ):
        request_id = canonical_request_id(request_id)
        validate_llm_payload(
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
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
        try:
            self.budgets.mark_started(reservation.reservation_id)
        except BaseException as start_error:
            self._reconcile_unknown_after_failure(
                reservation.reservation_id,
                start_error,
            )
            raise
        try:
            response = self.__delegate.create(
                system=system,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                request_id=request_id,
            )
        except BaseException as delegate_error:
            self._reconcile_unknown_after_failure(
                reservation.reservation_id,
                delegate_error,
            )
            raise

        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                usage_counts = None
            else:
                try:
                    input_tokens = getattr(usage, "input_tokens")
                    output_tokens = getattr(usage, "output_tokens")
                except AttributeError:
                    usage_counts = None
                else:
                    usage_counts = (
                        (input_tokens, output_tokens)
                        if (
                            type(input_tokens) is int
                            and input_tokens >= 0
                            and type(output_tokens) is int
                            and output_tokens >= 0
                        )
                        else None
                    )
        except BaseException as usage_error:
            self._reconcile_unknown_after_failure(
                reservation.reservation_id,
                usage_error,
            )
            raise

        if usage_counts is None:
            self.budgets.mark_unknown(reservation.reservation_id)
            return response
        input_tokens, output_tokens = usage_counts
        try:
            self.budgets.settle(
                reservation.reservation_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except BaseException as settlement_error:
            self._reconcile_unknown_after_failure(
                reservation.reservation_id,
                settlement_error,
            )
            raise
        return response


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
    usage = None
    if provider_usage is not None:
        try:
            input_tokens = getattr(provider_usage, "prompt_tokens")
            output_tokens = getattr(
                provider_usage,
                "completion_tokens",
            )
        except AttributeError:
            pass
        else:
            if (
                type(input_tokens) is int
                and input_tokens >= 0
                and type(output_tokens) is int
                and output_tokens >= 0
            ):
                usage = Usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
    return LLMResponse(
        content=blocks,
        stop_reason=stop,
        usage=usage,
        model=getattr(resp, "model", ""),
    )
