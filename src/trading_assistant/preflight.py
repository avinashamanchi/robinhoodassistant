"""Morning preflight: verify every subsystem before starting the daemon.

    python -m trading_assistant.preflight

Prints a PASS/FAIL/SKIP/NEEDS-ME table and exits non-zero on any FAIL. Run this
before starting the app + daemon each day.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from uuid import uuid4

from .config import BrokerKind, Secrets, TradingMode, load_config
from .db.schema import SchemaOutOfDate

PASS, FAIL, SKIP, NEEDS = "PASS", "FAIL", "SKIP", "NEEDS-ME"
_EXAMPLE_TOKENS = {"", "sk-ant-xxxxxxxx"}


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


def _dangerous_switches_off(config, secrets: Secrets) -> Result:
    enabled = []
    if config.features.auto_execute_preapproved_rules:
        enabled.append("autoexecute")
    if config.execution.prefer_bracket_orders:
        enabled.append("brackets")
    if config.llm.fallback_provider is not None:
        enabled.append("llm_fallback")
    if secrets.live_trading_confirm:
        enabled.append("live_confirmation")
    return Result(
        "dangerous switches OFF",
        FAIL if enabled else PASS,
        "all disabled" if not enabled else "enabled=" + ",".join(enabled),
    )


def _app_secret_quality(secrets: Secrets) -> Result:
    ok = len(secrets.app_api_token) >= 32
    return Result(
        "operator login secret quality",
        PASS if ok else FAIL,
        "configured (>=32 characters)" if ok else "APP_API_TOKEN must be >=32 characters",
    )


def _alpaca(secrets: Secrets) -> tuple[Result, Result, Result]:
    if not (secrets.alpaca_api_key and secrets.alpaca_secret_key):
        n = Result("Alpaca paper auth", NEEDS, "set ALPACA keys, then re-run")
        return n, Result("market clock reachable", NEEDS), Result("data bars reachable", NEEDS)
    try:
        from .broker.alpaca import AlpacaBroker, AlpacaClock

        broker = AlpacaBroker.from_credentials(secrets.alpaca_api_key, secrets.alpaca_secret_key, paper=True)
        acct = broker.get_account()
        auth = Result("Alpaca paper auth", PASS, f"equity={acct.equity}")
        clock = AlpacaClock.from_credentials(secrets.alpaca_api_key, secrets.alpaca_secret_key, paper=True)
        clk = Result("market clock reachable", PASS, f"open={clock.is_open()}")
        q = broker.get_quote("AAPL")
        data = Result("data reachable (AAPL quote)", PASS, f"last={q.last}")
        return auth, clk, data
    except Exception as e:
        err = _safe_exception_code(e)
        return (Result("Alpaca paper auth", FAIL, err),
                Result("market clock reachable", FAIL, err),
                Result("data reachable", FAIL, err))


def _db(secrets: Secrets) -> tuple[Result, Result, Result]:
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


def _build_service(config, secrets: Secrets):
    from .bootstrap import build_container

    return build_container(
        config,
        secrets,
        runtime_role="preflight",
    ).service


def _llm_provider_configured(config, secrets: Secrets) -> Result:
    provider_keys = {
        "anthropic": secrets.anthropic_api_key,
        "gemini": secrets.gemini_api_key,
        "groq": secrets.groq_api_key,
    }
    provider = config.llm.provider
    if provider not in provider_keys:
        return Result("configured LLM provider", FAIL, "unsupported provider")
    if not provider_keys[provider]:
        return Result(
            "configured LLM provider",
            NEEDS,
            f"set credentials for provider={provider}",
        )
    return Result("configured LLM provider", PASS, f"provider={provider}")


def _notification_configuration(config, secrets: Secrets) -> Result:
    if not config.features.telegram_notifications:
        return Result("notification configuration", SKIP, "disabled; no message sent")
    configured = bool(secrets.telegram_bot_token and secrets.telegram_chat_id)
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
    secrets = Secrets()
    from .logging import runtime_startup

    with runtime_startup("preflight", secrets):
        return _run(secrets)


def _run(secrets: Secrets) -> int:
    config = load_config("config.yaml")
    results = [
        _config_parses(),
        _paper_only(config),
        _dangerous_switches_off(config, secrets),
        _app_secret_quality(secrets),
    ]
    results.extend(_alpaca(secrets))
    results.extend(_db(secrets))
    if secrets.alpaca_api_key and secrets.alpaca_secret_key:
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
    print("  => " + ("READY" if not failed else "NOT READY — fix FAIL items") + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
