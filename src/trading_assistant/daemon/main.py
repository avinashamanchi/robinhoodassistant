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
    config = load_config()
    secrets = Secrets()
    engine = create_db_engine(secrets.database_url)
    create_all(engine)
    session_factory = make_session_factory(engine)
    service = TradingService(
        build_broker(config, secrets),
        session_factory,
        config,
        build_clock(config, secrets),
    )
    return Monitor(
        service,
        build_notifier(config, secrets),
        auto_execute=config.features.auto_execute_preapproved_rules,
        poll_interval_seconds=config.daemon.poll_interval_seconds,
    )


def main() -> None:
    configure_logging()
    asyncio.run(build_monitor().run())


if __name__ == "__main__":
    main()
