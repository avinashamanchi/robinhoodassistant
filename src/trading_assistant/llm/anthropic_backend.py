"""Anthropic backend. Returns the raw SDK response, which already matches the
normalized shape the agent/analyst consume (content blocks + stop_reason + usage).
"""

from __future__ import annotations

from typing import Any, Optional


class AnthropicBackend:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        timeout_seconds: float = 45.0,
    ) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key, timeout=timeout_seconds)
        self._model = model
        self._max_tokens = max_tokens

    def create(
        self, *, system: str, messages: list[dict], tools: list[dict],
        tool_choice: Optional[str] = None,
    ) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
        if tools and tool_choice == "any":
            kwargs["tool_choice"] = {"type": "any"}
        return self._client.messages.create(**kwargs)
