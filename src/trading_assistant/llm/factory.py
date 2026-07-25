"""Select exactly one explicitly configured LLM backend."""

from __future__ import annotations

from ..config import AppConfig, Secrets


def _make_backend(provider: str, config: AppConfig, secrets: Secrets):
    llm = config.llm
    if provider == "anthropic":
        from .anthropic_backend import AnthropicBackend

        return AnthropicBackend(
            secrets.anthropic_api_key,
            llm.model,
            llm.max_tokens,
            timeout_seconds=llm.request_timeout_seconds,
        )
    if provider == "gemini":
        from .gemini_backend import GeminiBackend

        return GeminiBackend(
            secrets.gemini_api_key,
            llm.gemini_model,
            llm.max_tokens,
            timeout_seconds=llm.request_timeout_seconds,
        )
    if provider == "groq":
        from .groq_backend import GroqBackend

        return GroqBackend(
            secrets.groq_api_key,
            llm.groq_model,
            llm.max_tokens,
            timeout_seconds=llm.request_timeout_seconds,
        )
    raise ValueError(f"unknown LLM provider: {provider}")


def build_llm_backend(config: AppConfig, secrets: Secrets):
    if config.llm.fallback_provider is not None:
        raise RuntimeError(
            "automatic cross-provider LLM fallback is disabled"
        )
    return _make_backend(config.llm.provider, config, secrets)
