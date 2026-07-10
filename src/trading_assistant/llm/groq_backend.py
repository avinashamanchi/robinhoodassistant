"""Groq backend (OpenAI-compatible chat completions with tool use)."""

from __future__ import annotations

from typing import Any, Optional

from .base import LLMResponse, from_openai, to_openai


class GroqBackend:
    def __init__(
        self, api_key: str, model: str, max_tokens: int = 1024, client: Any = None
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    def _get_client(self):
        if self._client is None:
            from groq import Groq

            self._client = Groq(api_key=self._api_key)
        return self._client

    def create(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        oai_messages, oai_tools = to_openai(system, messages, tools)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": self.max_tokens,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"
        resp = self._get_client().chat.completions.create(**kwargs)
        return from_openai(resp)
