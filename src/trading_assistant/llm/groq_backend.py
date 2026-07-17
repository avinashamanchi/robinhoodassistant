"""Groq backend (OpenAI-compatible chat completions with tool use)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .base import LLMResponse, ToolUseBlock, from_openai, to_openai

log = logging.getLogger(__name__)


def _failed_generation(err: Exception) -> str:
    """Pull Groq's `failed_generation` text out of a BadRequestError-shaped error."""
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict) and e.get("failed_generation"):
            return str(e["failed_generation"])
    return str(err)


def _recover_tool_use_failed(err: Exception) -> Optional[LLMResponse]:
    """Groq's smaller models sometimes emit a tool call in malformed
    ``<function=NAME [args]</function>`` syntax; the SDK then raises
    ``tool_use_failed`` rather than returning it. The intended call is recoverable
    from ``failed_generation`` — parse it so chat doesn't crash (500)."""
    text = _failed_generation(err)
    if "tool_use_failed" not in str(getattr(err, "body", "")) and "<function=" not in text:
        return None
    m = re.search(r"<function=([A-Za-z0-9_]+)", text)
    if not m:
        return None
    name = m.group(1)
    start = next((i for i, ch in enumerate(text[m.end():], m.end()) if ch in "{["), None)
    if start is None:
        return None
    try:
        args, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    if isinstance(args, list):  # Groq wraps the object in a single-element array
        args = next((a for a in args if isinstance(a, dict)), None)
    if not isinstance(args, dict):
        return None
    log.warning("recovered Groq tool_use_failed into %s(%s)", name, args)
    return LLMResponse(
        content=[ToolUseBlock(id="groq-recovered", name=name, input=args)],
        stop_reason="tool_use",
    )


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

    def create(
        self, *, system: str, messages: list[dict], tools: list[dict],
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        oai_messages, oai_tools = to_openai(system, messages, tools)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": self.max_tokens,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            # "any" -> the model MUST emit a tool call (used for structured
            # analyst output); default "auto" lets chat reply in plain text.
            kwargs["tool_choice"] = "required" if tool_choice == "any" else "auto"
        try:
            resp = self._get_client().chat.completions.create(**kwargs)
        except Exception as err:  # noqa: BLE001 — inspect for a recoverable tool_use_failed
            recovered = _recover_tool_use_failed(err)
            if recovered is not None:
                return recovered
            raise
        return from_openai(resp)
