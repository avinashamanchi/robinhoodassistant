"""Daemon entrypoint: build the stack from config/secrets and run the monitor loop.

    uv run python -m trading_assistant.daemon.main
"""

from __future__ import annotations

import asyncio

from ..config import Secrets, load_config
from ..notifications.base import build_notifier
from .backoff import RetryPolicy, retry_read
from .monitor import Monitor


def build_monitor() -> Monitor:
    from ..logging import runtime_startup

    secrets = Secrets()
    with runtime_startup("daemon", secrets):
        return _build_monitor(load_config(), secrets)


def _build_monitor(config, secrets: Secrets) -> Monitor:
    from .. import bootstrap

    container = bootstrap.build_container(
        config,
        secrets,
        runtime_role="daemon",
    )
    service = container.service
    notifier = build_notifier(config, secrets)
    container.rule_worker.notifier = notifier
    shadow = None
    screen_source = None
    if config.features.shadow_mode:
        from decimal import Decimal

        from ..analyst.analyst import Analyst
        from ..analyst.live_features import build_live_feature_provider, build_screen_source
        from ..analyst.planning import PlanningService
        from ..analyst.shadow import ShadowRunner
        from ..llm.factory import build_llm_backend

        analyst = Analyst(
            build_llm_backend(
                config,
                secrets,
                provider_budget=container.provider_budget,
                category="analysis",
            ),
            max_tokens=config.llm.max_tokens,
            suppress_ranging=config.analyst.suppress_ranging,
            max_attempts=(
                config.security.provider_budget.max_structured_attempts
            ),
        )
        planning = PlanningService(service, analyst, build_live_feature_provider(config, secrets), secrets)
        universe = config.screener.universe or config.risk.ticker_allowlist
        screen_source = build_screen_source([s.upper() for s in universe], secrets)

        def _price(sym: str):
            try:
                quote = retry_read(
                    lambda: service.broker.get_quote(sym),
                    RetryPolicy(),
                )
                return Decimal(str(quote.last))
            except Exception:
                return None

        shadow = ShadowRunner(service, planning, screen_source, _price, top_n=3)

    return Monitor(
        service,
        notifier,
        auto_execute=config.features.auto_execute_preapproved_rules,
        poll_interval_seconds=config.daemon.poll_interval_seconds,
        max_quote_age_seconds=config.daemon.max_quote_age_seconds,
        cycle_timeout_seconds=config.daemon.cycle_timeout_seconds,
        daily_task_timeout_seconds=config.daemon.daily_task_timeout_seconds,
        shadow=shadow,
        digest_source=screen_source,
        rule_worker=container.rule_worker,
        rate_limiter=container.rate_limiter,
        leases=container.leases,
        provider_budget=container.provider_budget,
    )


def main() -> None:
    from ..logging import runtime_startup

    secrets = Secrets()
    with runtime_startup("daemon", secrets):
        monitor = _build_monitor(load_config(), secrets)
        asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
