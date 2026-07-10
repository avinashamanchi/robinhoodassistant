"""Morning preflight: verify every subsystem before starting the daemon.

    python -m trading_assistant.preflight

Prints a PASS/FAIL/SKIP/NEEDS-ME table and exits non-zero on any FAIL. Run this
before starting the app + daemon each day.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .config import Secrets, TradingMode, load_config

PASS, FAIL, SKIP, NEEDS = "PASS", "FAIL", "SKIP", "NEEDS-ME"
_EXAMPLE_TOKENS = {"", "sk-ant-xxxxxxxx"}


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


def _config_parses() -> Result:
    try:
        load_config("config.yaml")
        return Result("config.yaml parses (extra=forbid)", PASS)
    except Exception as e:
        return Result("config.yaml parses", FAIL, f"{type(e).__name__}: {e}")


def _env_present(secrets: Secrets) -> Result:
    missing = []
    if not secrets.app_api_token or len(secrets.app_api_token) < 32:
        missing.append("APP_API_TOKEN (>=32 hex; openssl rand -hex 32)")
    if not (secrets.gemini_api_key or secrets.groq_api_key or secrets.anthropic_api_key):
        missing.append("an LLM key (GEMINI/GROQ/ANTHROPIC)")
    if not (secrets.alpaca_api_key and secrets.alpaca_secret_key):
        missing.append("ALPACA_API_KEY/SECRET")
    return Result("required .env values present", FAIL if missing else PASS, ", ".join(missing))


def _live_off(config, secrets: Secrets) -> Result:
    ok = config.trading.mode is TradingMode.PAPER and not secrets.live_trading_confirm
    return Result("live trading OFF (double-lock)", PASS if ok else FAIL,
                  f"mode={config.trading.mode.value} confirm_set={bool(secrets.live_trading_confirm)}")


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
        err = f"{type(e).__name__}: {e}"
        return (Result("Alpaca paper auth", FAIL, err),
                Result("market clock reachable", FAIL, err),
                Result("data reachable", FAIL, err))


def _db(secrets: Secrets) -> tuple[Result, Result]:
    try:
        from sqlalchemy import text

        from .db.models import create_all
        from .db.session import create_db_engine

        engine = create_db_engine(secrets.database_url)
        create_all(engine)
        with engine.connect() as c:
            mode = c.execute(text("PRAGMA journal_mode")).scalar()
        wal = Result("DB WAL mode", PASS if str(mode).lower() == "wal" else FAIL, f"journal_mode={mode}")
        # Kill-switch state
        from sqlalchemy.orm import Session

        from .db.models import KillSwitchState
        with Session(engine) as s:
            tripped = [r.asset_class for r in s.query(KillSwitchState).filter_by(tripped=True).all()]
        ks = Result("kill switches", PASS if not tripped else FAIL,
                    "all clear" if not tripped else f"TRIPPED: {tripped} (reset before trading)")
        return wal, ks
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        return Result("DB WAL mode", FAIL, err), Result("kill switches", FAIL, err)


def _llm(config, secrets: Secrets) -> Result:
    if not (secrets.gemini_api_key or secrets.groq_api_key or secrets.anthropic_api_key):
        return Result("LLM provider ping", NEEDS, "set an LLM key, then re-run")
    try:
        from .llm.factory import build_llm_backend

        backend = build_llm_backend(config, secrets)
        resp = backend.create(system="Reply with the single word OK.",
                              messages=[{"role": "user", "content": "ping"}], tools=[])
        text = "".join(getattr(b, "text", "") for b in getattr(resp, "content", []))
        return Result(f"LLM ping ({config.llm.provider}->{config.llm.fallback_provider})", PASS, text.strip()[:20])
    except Exception as e:
        return Result("LLM provider ping", FAIL, f"{type(e).__name__}: {e}")


def _robinhood(config, secrets: Secrets) -> Result:
    rh = config.external_accounts.robinhood if config.external_accounts else None
    if not (rh and rh.enabled):
        return Result("Robinhood (read-only)", SKIP, "disabled")
    try:
        from .external_accounts.robinhood import RobinhoodSource

        RobinhoodSource(secrets.rh_username, secrets.rh_password, secrets.rh_totp_secret, rh.token_path).get_account_summary()
        return Result("Robinhood (read-only) login", PASS)
    except Exception as e:
        return Result("Robinhood login", FAIL, f"{type(e).__name__}")


def _telegram(config, secrets: Secrets) -> Result:
    if not config.features.telegram_notifications:
        return Result("Telegram", SKIP, "disabled")
    from .notifications.telegram import TelegramNotifier

    ok = TelegramNotifier(True, secrets.telegram_bot_token, secrets.telegram_chat_id).send("preflight test ✅")
    return Result("Telegram test message", PASS if ok else FAIL)


def run() -> int:
    config = load_config("config.yaml")
    secrets = Secrets()
    results = [_config_parses(), _env_present(secrets), _live_off(config, secrets)]
    results.extend(_alpaca(secrets))
    results.extend(_db(secrets))
    results.append(_llm(config, secrets))
    results.append(_robinhood(config, secrets))
    results.append(_telegram(config, secrets))

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
