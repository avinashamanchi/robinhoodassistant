"""Morning preflight: verify every subsystem before starting the daemon.

    python -m trading_assistant.preflight

Prints a PASS/FAIL/SKIP/NEEDS-ME table and exits non-zero on any FAIL or
NEEDS-ME item. Run this before starting the app + daemon each day.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from uuid import uuid4

from .config import BrokerKind, TradingMode, load_config
from .db.schema import SchemaOutOfDate
from .security.secrets import (
    RuntimeSecrets,
    app_secret_quality_ok,
    load_role_secrets,
    secret_is_set,
    secret_value,
)

PASS, FAIL, SKIP, NEEDS = "PASS", "FAIL", "SKIP", "NEEDS-ME"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


def _safe_exception_code(_exc: Exception) -> str:
    """Return a fixed code without exposing provider type or text."""
    return "dependency_failed"


def _config_parses() -> Result:
    try:
        load_config("config.yaml")
        return Result("config.yaml parses (extra=forbid)", PASS)
    except Exception as e:
        return Result("config.yaml parses", FAIL, _safe_exception_code(e))


def _paper_only(config) -> Result:
    ok = (
        config.trading.mode is TradingMode.PAPER
        and config.trading.broker is BrokerKind.ALPACA
    )
    return Result(
        "paper-only Alpaca configuration",
        PASS if ok else FAIL,
        f"mode={config.trading.mode.value} broker={config.trading.broker.value}",
    )


def _dangerous_switches_off(
    config,
    secrets: RuntimeSecrets,
) -> Result:
    enabled = []
    if config.features.auto_execute_preapproved_rules:
        enabled.append("autoexecute")
    if config.execution.prefer_bracket_orders:
        enabled.append("brackets")
    if config.llm.fallback_provider is not None:
        enabled.append("llm_fallback")
    if secret_is_set(secrets.live_trading_confirm):
        enabled.append("live_confirmation")
    return Result(
        "dangerous switches OFF",
        FAIL if enabled else PASS,
        "all disabled" if not enabled else "enabled=" + ",".join(enabled),
    )


def _app_secret_quality(secrets: RuntimeSecrets) -> Result:
    ok = app_secret_quality_ok(secrets.app_api_token)
    return Result(
        "operator login secret quality",
        PASS if ok else FAIL,
        (
            "configured (basic format/placeholder checks passed; "
            "not an entropy proof)"
            if ok
            else "APP_API_TOKEN must be >=32 characters, non-placeholder, "
            "and non-periodic"
        ),
    )


def _alpaca(
    secrets: RuntimeSecrets,
) -> tuple[Result, Result, Result]:
    if not (
        secret_is_set(secrets.alpaca_api_key)
        and secret_is_set(secrets.alpaca_secret_key)
    ):
        n = Result("Alpaca paper auth", NEEDS, "set ALPACA keys, then re-run")
        return n, Result("market clock reachable", NEEDS), Result("data bars reachable", NEEDS)

    try:
        from .broker.alpaca import AlpacaBroker, AlpacaClock

        broker = AlpacaBroker.from_credentials(
            secret_value(secrets.alpaca_api_key),
            secret_value(secrets.alpaca_secret_key),
            paper=True,
        )
    except Exception as exc:
        detail = _safe_exception_code(exc)
        auth = Result("Alpaca paper auth", FAIL, detail)
        data = Result("data reachable", FAIL, detail)
    else:
        try:
            acct = broker.get_account()
            positions = broker.get_positions()
            if (
                not acct.is_valid
                or any(
                    not position.risk_values_valid
                    for position in positions
                )
            ):
                raise ValueError("invalid broker account snapshot")
            auth = Result("Alpaca paper auth", PASS, f"equity={acct.equity}")
        except Exception as exc:
            auth = Result(
                "Alpaca paper auth",
                FAIL,
                _safe_exception_code(exc),
            )

        try:
            quote = broker.get_quote("AAPL")
            data = Result(
                "data reachable (AAPL quote)",
                PASS,
                f"last={quote.last}",
            )
        except Exception as exc:
            data = Result(
                "data reachable",
                FAIL,
                _safe_exception_code(exc),
            )

    try:
        clock_client = AlpacaClock.from_credentials(
            secret_value(secrets.alpaca_api_key),
            secret_value(secrets.alpaca_secret_key),
            paper=True,
        )
        clock = Result(
            "market clock reachable",
            PASS,
            f"open={clock_client.is_open()}",
        )
    except Exception as exc:
        clock = Result(
            "market clock reachable",
            FAIL,
            _safe_exception_code(exc),
        )

    return auth, clock, data


def _db(
    secrets: RuntimeSecrets,
) -> tuple[Result, Result, Result]:
    try:
        from sqlalchemy import text

        from .db.schema import require_current_schema
        from .db.session import create_db_engine

        engine = create_db_engine(secrets.database_url)
        require_current_schema(engine)
        schema = Result("database schema current", PASS)
        with engine.connect() as c:
            mode = c.execute(text("PRAGMA journal_mode")).scalar()
        wal = Result("DB WAL mode", PASS if str(mode).lower() == "wal" else FAIL, f"journal_mode={mode}")
        # Kill-switch state
        from sqlalchemy.orm import Session

        from .db.models import CircuitBreakerState
        with Session(engine) as s:
            tripped = [
                row.scope_key
                for row in s.query(CircuitBreakerState).filter_by(tripped=True).all()
            ]
        ks = Result("kill switches", PASS if not tripped else FAIL,
                    "all clear" if not tripped else f"TRIPPED: {tripped} (reset before trading)")
        return schema, wal, ks
    except SchemaOutOfDate:
        err = "schema_out_of_date"
        return (
            Result("database schema current", FAIL, err),
            Result("DB WAL mode", FAIL, err),
            Result("kill switches", FAIL, err),
        )
    except Exception as e:
        err = _safe_exception_code(e)
        return (
            Result("database schema current", FAIL, err),
            Result("DB WAL mode", FAIL, err),
            Result("kill switches", FAIL, err),
        )


def _reconciliation(service) -> Result:
    """Repair stale order statuses, then require local positions to match Alpaca."""
    try:
        request_id = uuid4().hex
        order_sync = service.sync_open_orders(
            actor="preflight:startup",
            reason="preflight broker order reconciliation",
            request_id=request_id,
        )
        positions = service.reconcile_positions(
            actor="preflight:startup",
            reason="preflight position reconciliation",
            request_id=request_id,
        )
        if order_sync.get("failed", 0):
            return Result(
                "broker/local reconciliation",
                FAIL,
                f"order status sync failures: {order_sync}",
            )
        if not positions["reconciled"]:
            return Result(
                "broker/local reconciliation",
                FAIL,
                f"drift={positions['drift']} order_sync={order_sync}",
            )
        return Result(
            "broker/local reconciliation",
            PASS,
            f"positions match; order_sync={order_sync}",
        )
    except Exception as e:
        return Result(
            "broker/local reconciliation", FAIL, _safe_exception_code(e)
        )


def _build_service(config, secrets: RuntimeSecrets):
    from .bootstrap import build_container

    return build_container(
        config,
        secrets,
        runtime_role="preflight",
    ).service


def _llm_provider_configured(
    config,
    secrets: RuntimeSecrets,
) -> Result:
    provider_keys = {
        "anthropic": secrets.anthropic_api_key,
        "gemini": secrets.gemini_api_key,
        "groq": secrets.groq_api_key,
    }
    provider = config.llm.provider
    if provider not in provider_keys:
        return Result("configured LLM provider", FAIL, "unsupported provider")
    if not secret_is_set(provider_keys[provider]):
        return Result(
            "configured LLM provider",
            NEEDS,
            f"set credentials for provider={provider}",
        )
    return Result("configured LLM provider", PASS, f"provider={provider}")


def _notification_configuration(
    config,
    secrets: RuntimeSecrets,
) -> Result:
    if not config.features.telegram_notifications:
        return Result("notification configuration", SKIP, "disabled; no message sent")
    configured = (
        secret_is_set(secrets.telegram_bot_token)
        and secret_is_set(secrets.telegram_chat_id)
    )
    return Result(
        "notification configuration",
        PASS if configured else FAIL,
        (
            "enabled; no message sent"
            if configured
            else "enabled but credentials missing; no message sent"
        ),
    )


def run() -> int:
    from .logging import runtime_startup

    config = load_config("config.yaml")
    secrets = load_role_secrets("preflight", config=config)
    with runtime_startup("preflight", secrets):
        return _run(config, secrets)


def _run(
    config,
    secrets: RuntimeSecrets | None = None,
) -> int:
    if secrets is None:
        secrets = config
        config = load_config("config.yaml")
    results = [
        _config_parses(),
        _paper_only(config),
        _dangerous_switches_off(config, secrets),
        _app_secret_quality(secrets),
    ]
    results.extend(_alpaca(secrets))
    results.extend(_db(secrets))
    if (
        secret_is_set(secrets.alpaca_api_key)
        and secret_is_set(secrets.alpaca_secret_key)
    ):
        results.append(_reconciliation(_build_service(config, secrets)))
    else:
        results.append(
            Result(
                "broker/local reconciliation",
                NEEDS,
                "set ALPACA keys, then re-run",
            )
        )
    results.append(_llm_provider_configured(config, secrets))
    results.append(_notification_configuration(config, secrets))

    width = max(len(r.name) for r in results)
    print("\nPREFLIGHT\n" + "-" * (width + 40))
    for r in results:
        print(f"  {r.status:8} {r.name:<{width}}  {r.detail}")
    failed = [r for r in results if r.status == FAIL]
    needs = [r for r in results if r.status == NEEDS]
    print("-" * (width + 40))
    print(f"  {len(failed)} FAIL · {len(needs)} NEEDS-ME · "
          f"{sum(r.status == PASS for r in results)} PASS · {sum(r.status == SKIP for r in results)} SKIP")
    not_ready = bool(failed or needs)
    print(
        "  => "
        + (
            "NOT READY — fix FAIL/NEEDS-ME items"
            if not_ready
            else "READY"
        )
        + "\n"
    )
    return 1 if not_ready else 0


if __name__ == "__main__":
    sys.exit(run())
