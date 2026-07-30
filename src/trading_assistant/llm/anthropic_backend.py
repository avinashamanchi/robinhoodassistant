"""Anthropic backend. Returns the raw SDK response, which already matches the
normalized shape the agent/analyst consume (content blocks + stop_reason + usage).
"""

from __future__ import annotations

from typing import Any, Optional

from ..security.outbound import (
    OutboundPolicy,
    new_httpx_client,
    require_origin,
)
from .payloads import build_anthropic_payload


_ANTHROPIC_ORIGIN = "https://api.anthropic.com"
_ANTHROPIC_POLICY = OutboundPolicy(_ANTHROPIC_ORIGIN)


class AnthropicBackend:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        timeout_seconds: float = 45.0,
        client: Any = None,
        runtime_role: str = "app",
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._runtime_role = runtime_role

    def _get_client(self):
        if self._client is None:
            from anthropic import Anthropic

            require_origin(
                self._runtime_role,
                "llm.anthropic",
                _ANTHROPIC_ORIGIN,
            )
            self._client = Anthropic(
                api_key=self._api_key,
                base_url=_ANTHROPIC_ORIGIN,
                timeout=self._timeout_seconds,
                max_retries=0,
                http_client=new_httpx_client(
                    _ANTHROPIC_POLICY,
                    read_timeout=self._timeout_seconds,
                ),
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
