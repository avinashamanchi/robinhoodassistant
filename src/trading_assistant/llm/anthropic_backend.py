"""Anthropic backend. Returns the raw SDK response, which already matches the
normalized shape the agent/analyst consume (content blocks + stop_reason + usage).
"""

from __future__ import annotations

from typing import Any, Optional

from .payloads import build_anthropic_payload


class AnthropicBackend:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        timeout_seconds: float = 45.0,
        client: Any = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def _get_client(self):
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(
                api_key=self._api_key,
                timeout=self._timeout_seconds,
            )
        return self._client

    def create(
        self, *, system: str, messages: list[dict], tools: list[dict],
        tool_choice: Optional[str] = None,
        request_id: str = "",
    ) -> Any:
        payload = build_anthropic_payload(
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            **payload,
        }
        return self._get_client().messages.create(**kwargs)
