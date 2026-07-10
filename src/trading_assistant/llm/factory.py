"""Select the LLM backend from config, with an optional runtime fallback.

``llm.provider`` picks the primary; ``llm.fallback_provider`` (optional) is tried
if the primary raises at call time (e.g. Gemini auth/quota fails -> Groq).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..config import AppConfig, Secrets

log = logging.getLogger(__name__)


class FallbackBackend:
    """Try the primary backend; on any exception, fall back to the secondary."""

    def __init__(self, primary, fallback) -> None:
        self._primary = primary
        self._fallback = fallback

    def create(self, *, system: str, messages: list[dict], tools: list[dict]) -> Any:
        try:
            return self._primary.create(system=system, messages=messages, tools=tools)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "primary LLM backend failed (%s); falling back", type(exc).__name__
            )
            return self._fallback.create(system=system, messages=messages, tools=tools)


def _make_backend(provider: str, config: AppConfig, secrets: Secrets):
    llm = config.llm
    if provider == "anthropic":
        from .anthropic_backend import AnthropicBackend

        return AnthropicBackend(secrets.anthropic_api_key, llm.model, llm.max_tokens)
    if provider == "gemini":
        from .gemini_backend import GeminiBackend

        return GeminiBackend(secrets.gemini_api_key, llm.gemini_model, llm.max_tokens)
    if provider == "groq":
        from .groq_backend import GroqBackend

        return GroqBackend(secrets.groq_api_key, llm.groq_model, llm.max_tokens)
    raise ValueError(f"unknown LLM provider: {provider}")


def build_llm_backend(config: AppConfig, secrets: Secrets):
    primary = _make_backend(config.llm.provider, config, secrets)
    if config.llm.fallback_provider:
        fallback = _make_backend(config.llm.fallback_provider, config, secrets)
        return FallbackBackend(primary, fallback)
    return primary
