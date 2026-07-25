"""One fail-closed production composition root and runtime safety helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_assistant.config import BrokerKind, Secrets, TradingMode
from trading_assistant.db.migrate import upgrade
from trading_assistant.db.schema import SchemaOutOfDate
from trading_assistant.db.session import create_db_engine


def _migrated_secrets(tmp_path: Path) -> Secrets:
    database_url = f"sqlite:///{tmp_path}/runtime.db"
    upgrade(create_db_engine(database_url))
    return Secrets(
        database_url=database_url,
        app_api_token="operator-secret-for-bootstrap-tests",
    )


def test_application_container_reuses_exact_trading_service_components(
    tmp_path,
    app_config,
):
    from trading_assistant.bootstrap import build_container

    container = build_container(
        app_config,
        _migrated_secrets(tmp_path),
    )

    assert container.snapshot_service is container.service.snapshot_service
    assert container.order_application is container.service.order_application
    assert container.order_submission is container.service.order_submission
    assert container.reconciliation is container.service.reconciliation
    assert container.breakers is container.service.breakers
    assert container.rule_worker.service is container.service
    assert container.rule_worker.repository is container.service.rule_repository
    assert container.session_auth.session_factory is container.session_factory


@pytest.mark.parametrize(
    ("config_update", "message"),
    [
        (
            lambda cfg: {
                "trading": cfg.trading.model_copy(
                    update={
                        "mode": TradingMode.LIVE,
                        "broker": BrokerKind.ALPACA,
                    }
                )
            },
            "live trading is locked out",
        ),
        (
            lambda cfg: {
                "features": cfg.features.model_copy(
                    update={"auto_execute_preapproved_rules": True}
                )
            },
            "auto-execution",
        ),
        (
            lambda cfg: {
                "execution": cfg.execution.model_copy(
                    update={"prefer_bracket_orders": True}
                )
            },
            "automatic bracket",
        ),
        (
            lambda cfg: {
                "llm": cfg.llm.model_copy(
                    update={"fallback_provider": "groq"}
                )
            },
            "cross-provider",
        ),
    ],
)
def test_bootstrap_rejects_every_dangerous_runtime_switch(
    tmp_path,
    app_config,
    config_update,
    message,
):
    from trading_assistant.bootstrap import build_container

    unsafe = app_config.model_copy(update=config_update(app_config))

    with pytest.raises(RuntimeError, match=message):
        build_container(unsafe, _migrated_secrets(tmp_path))


def test_bootstrap_rejects_outdated_schema_before_provider_construction(
    tmp_path,
    app_config,
    monkeypatch,
):
    from trading_assistant import bootstrap

    database_url = f"sqlite:///{tmp_path}/outdated.db"
    create_db_engine(database_url)
    broker_built = False

    def forbidden_broker(*_args, **_kwargs):
        nonlocal broker_built
        broker_built = True
        raise AssertionError("provider construction must follow schema gate")

    monkeypatch.setattr(bootstrap, "build_broker", forbidden_broker)

    with pytest.raises(SchemaOutOfDate):
        bootstrap.build_container(
            app_config,
            Secrets(
                database_url=database_url,
                app_api_token="operator-secret-for-bootstrap-tests",
            ),
        )

    assert broker_built is False


def test_heartbeat_upserts_one_row_per_source(make_service):
    from sqlalchemy import select

    from trading_assistant.db.models import Heartbeat

    service = make_service()
    for _ in range(5):
        service.write_heartbeat("daemon")
    service.write_heartbeat("app")

    with service.session_factory() as session:
        daemon = session.scalars(
            select(Heartbeat).where(Heartbeat.source == "daemon")
        ).all()
        app = session.scalars(
            select(Heartbeat).where(Heartbeat.source == "app")
        ).all()

    assert len(daemon) == 1
    assert len(app) == 1


def test_private_logging_is_idempotent_rotating_and_redacted(tmp_path):
    import logging
    import stat

    from trading_assistant.logging import (
        configure_logging,
        register_secret,
    )

    path = tmp_path / "private" / "runtime.log"
    marker = "secret-for-runtime-log-test"
    register_secret(marker)

    configure_logging(
        log_path=path,
        max_bytes=64,
        backup_count=1,
    )
    configure_logging(
        log_path=path,
        max_bytes=64,
        backup_count=1,
    )
    logging.getLogger("task9").warning("value=%s", marker)
    logging.getLogger("task9").warning("rotation=%s", "x" * 128)

    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_trading_assistant_path", None) == str(path)
    ]
    assert len(handlers) == 1
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rotated = path.with_name(f"{path.name}.1")
    assert rotated.exists()
    assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
    combined = (
        path.read_text(encoding="utf-8")
        + rotated.read_text(encoding="utf-8")
    )
    assert marker not in combined
    assert "REDACTED" in combined
