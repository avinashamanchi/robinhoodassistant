"""Select exactly one explicitly configured LLM backend."""

from __future__ import annotations

from ..config import AppConfig, Secrets
from ..security.secrets import secret_value
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


def selected_llm_model(config: AppConfig) -> str:
    provider = config.llm.provider
    selected_models = {
        "anthropic": config.llm.model,
        "gemini": config.llm.gemini_model,
        "groq": config.llm.groq_model,
    }
    try:
        return selected_models[provider]
    except KeyError:
        raise ValueError(f"unknown LLM provider: {provider}") from None


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
    runtime_role: str = "app",
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
    llm = config.llm
    model = selected_llm_model(config)
    if provider == "anthropic":
        from .anthropic_backend import AnthropicBackend

        delegate = AnthropicBackend(
            secret_value(secrets.anthropic_api_key),
            model,
            llm.max_tokens,
            timeout_seconds=llm.request_timeout_seconds,
            runtime_role=runtime_role,
        )
    elif provider == "gemini":
        from .gemini_backend import GeminiBackend

        delegate = GeminiBackend(
            secret_value(secrets.gemini_api_key),
            model,
            llm.max_tokens,
            timeout_seconds=llm.request_timeout_seconds,
            runtime_role=runtime_role,
        )
    elif provider == "groq":
        from .groq_backend import GroqBackend

        delegate = GroqBackend(
            secret_value(secrets.groq_api_key),
            model,
            llm.max_tokens,
            timeout_seconds=llm.request_timeout_seconds,
            runtime_role=runtime_role,
        )
    else:
        raise ValueError(f"unknown LLM provider: {provider}")
    return BudgetedLLMBackend(
        delegate,
        provider_budget,
        provider=provider,
        category=category,
        max_output_tokens=config.llm.max_tokens,
        estimator=estimator,
    )
