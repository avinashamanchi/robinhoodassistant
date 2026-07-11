"""Daemon entrypoint: build the stack from config/secrets and run the monitor loop.

    uv run python -m trading_assistant.daemon.main
"""

from __future__ import annotations

import asyncio

from ..broker.factory import build_broker, build_clock
from ..config import Secrets, load_config
from ..db.models import create_all
from ..db.session import create_db_engine, make_session_factory
from ..logging import configure_logging
from ..notifications.base import build_notifier
from ..service import TradingService
from .monitor import Monitor


def build_monitor() -> Monitor:
    from ..external_accounts.factory import build_external_source
    from ..logging import register_all_secrets

    config = load_config()
    secrets = Secrets()
    register_all_secrets(secrets)
    engine = create_db_engine(secrets.database_url)
    create_all(engine)
    session_factory = make_session_factory(engine)
    service = TradingService(
        build_broker(config, secrets),
        session_factory,
        config,
        build_clock(config, secrets),
        external_source=build_external_source(config, secrets),
    )
    shadow = None
    screen_source = None
    if config.features.shadow_mode:
        from decimal import Decimal

        from ..analyst.analyst import Analyst
        from ..analyst.live_features import build_live_feature_provider, build_screen_source
        from ..analyst.planning import PlanningService
        from ..analyst.shadow import ShadowRunner
        from ..llm.factory import build_llm_backend

        analyst = Analyst(build_llm_backend(config, secrets), max_tokens=config.llm.max_tokens)
        planning = PlanningService(service, analyst, build_live_feature_provider(config, secrets), secrets)
        universe = config.screener.universe or config.risk.ticker_allowlist
        screen_source = build_screen_source([s.upper() for s in universe], secrets)

        def _price(sym: str):
            try:
                return Decimal(str(service.broker.get_quote(sym).last))
            except Exception:
                return None

        shadow = ShadowRunner(service, planning, screen_source, _price, top_n=3)

    return Monitor(
        service,
        build_notifier(config, secrets),
        auto_execute=config.features.auto_execute_preapproved_rules,
        poll_interval_seconds=config.daemon.poll_interval_seconds,
        max_quote_age_seconds=config.daemon.max_quote_age_seconds,
        shadow=shadow,
        digest_source=screen_source,
    )


def main() -> None:
    configure_logging()
    asyncio.run(build_monitor().run())


if __name__ == "__main__":
    main()
