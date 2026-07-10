"""Anthropic backend. Returns the raw SDK response, which already matches the
normalized shape the agent/analyst consume (content blocks + stop_reason + usage).
"""

from __future__ import annotations

from typing import Any


class AnthropicBackend:
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
