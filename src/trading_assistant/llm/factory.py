"""Select exactly one explicitly configured LLM backend."""

from __future__ import annotations

from ..config import AppConfig, Secrets
from .budget import (
    ProviderBudgetUnavailable,
    ProviderInputEstimator,
    Utf8ByteUpperBoundEstimator,
)


_PROVIDER_INPUT_ESTIMATORS: dict[str, ProviderInputEstimator] = {
    "anthropic": Utf8ByteUpperBoundEstimator(),
    "gemini": Utf8ByteUpperBoundEstimator(),
    "groq": Utf8ByteUpperBoundEstimator(),
}


def resolve_input_estimator(provider: str) -> ProviderInputEstimator:
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be non-empty")
    estimator = _PROVIDER_INPUT_ESTIMATORS.get(provider)
    if estimator is None:
        raise ProviderBudgetUnavailable(
            f"no provider input estimator registered for {provider!r}"
        )
    return estimator


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
    resolve_input_estimator(config.llm.provider)
    return _make_backend(config.llm.provider, config, secrets)
