"""Select exactly one explicitly configured LLM backend."""

from __future__ import annotations

from ..config import AppConfig, Secrets
from .base import BudgetedLLMBackend
from .budget import (
    AnthropicInputEstimator,
    GeminiInputEstimator,
    GroqInputEstimator,
    ProviderBudgetExceeded,
    ProviderBudgetService,
    ProviderBudgetUnavailable,
    ProviderInputEstimator,
)

_ALLOWED_CATEGORIES = frozenset(
    {"chat", "analysis", "untrusted", "backtest"}
)

_PROVIDER_INPUT_ESTIMATORS: dict[str, ProviderInputEstimator] = {
    "anthropic": AnthropicInputEstimator(),
    "gemini": GeminiInputEstimator(),
    "groq": GroqInputEstimator(),
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


class _DisabledBacktestBackend:
    def create(self, **_kwargs):
        raise ProviderBudgetExceeded("LLM backtests are disabled")


def _require_category(category: str) -> str:
    if (
        not isinstance(category, str)
        or category not in _ALLOWED_CATEGORIES
    ):
        raise ValueError(
            "category must be one of: analysis, backtest, chat, untrusted"
        )
    return category


def build_llm_backend(
    config: AppConfig,
    secrets: Secrets,
    *,
    provider_budget: ProviderBudgetService,
    category: str,
):
    category = _require_category(category)
    if config.llm.fallback_provider is not None:
        raise RuntimeError(
            "automatic cross-provider LLM fallback is disabled"
        )
    if not isinstance(provider_budget, ProviderBudgetService):
        raise TypeError(
            "provider_budget must be a ProviderBudgetService"
        )
    if (
        category == "backtest"
        and not config.security.provider_budget.backtest_llm_enabled
    ):
        return _DisabledBacktestBackend()
    provider = config.llm.provider
    estimator = resolve_input_estimator(provider)
    delegate = _make_backend(provider, config, secrets)
    return BudgetedLLMBackend(
        delegate,
        provider_budget,
        provider=provider,
        category=category,
        max_output_tokens=config.llm.max_tokens,
        estimator=estimator,
    )
