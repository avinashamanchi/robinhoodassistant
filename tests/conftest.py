"""Shared fixtures: risk config, file-backed SQLite, MockBroker, snapshot builder."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient as StarletteTestClient

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import PortfolioSnapshot, Position, Quote
from trading_assistant.config import (
    AppConfig,
    BrokerKind,
    RiskConfig,
    WindowLimitConfig,
    load_config,
)
from trading_assistant.db.models import Base
from trading_assistant.db.session import create_db_engine, make_session_factory
import trading_assistant.security.secrets as secret_module

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_OPERATOR_TOKEN = "test-operator-secret"


@pytest.fixture(autouse=True)
def _forbid_uninjected_native_keychain(monkeypatch):
    """Fail before default backend selection can inspect the real Keychain."""

    import keyring
    from keyring.backends.macOS import Keyring as NativeMacOSKeyring

    def forbidden_backend(*_args, **_kwargs):
        raise AssertionError(
            "tests must inject a fake KeyringBackend or RuntimeSecrets"
        )

    monkeypatch.setattr(
        secret_module,
        "_default_keyring_backend",
        forbidden_backend,
    )
    for operation in (
        "get_keyring",
        "get_password",
        "set_password",
        "delete_password",
    ):
        monkeypatch.setattr(keyring, operation, forbidden_backend)
    for operation in (
        "get_password",
        "set_password",
        "delete_password",
    ):
        monkeypatch.setattr(
            NativeMacOSKeyring,
            operation,
            forbidden_backend,
        )


@pytest.fixture(autouse=True)
def _default_test_clients_to_tls(monkeypatch):
    """Match the strict production server config unless a test opts out."""
    original_init = StarletteTestClient.__init__

    def tls_init(self, *args, **kwargs):
        kwargs.setdefault("base_url", "https://localhost:8020")
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(StarletteTestClient, "__init__", tls_init)


@pytest.fixture
def risk_config() -> RiskConfig:
    """Matches config.yaml defaults; tests override via model_copy to isolate limits."""
    return RiskConfig(
        ticker_allowlist=["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"],
        max_notional_per_order=500,
        max_position_per_ticker=2000,
        max_portfolio_exposure=10000,
        daily_realized_loss_limit=500,
        price_sanity_pct=5.0,
        reject_when_market_closed=True,
        proposal_ttl_minutes=15,
    )


@pytest.fixture
def app_config() -> AppConfig:
    """The committed config.yaml, NORMALIZED to stable test defaults so operational
    config edits (switching to live Alpaca paper, widening the allowlist, raising
    risk caps) can never break the test baseline. Risk caps + allowlist are pinned
    to the same values as the ``risk_config`` fixture."""
    cfg = load_config(REPO_ROOT / "config.yaml")
    return cfg.model_copy(update={
        "trading": cfg.trading.model_copy(update={"broker": BrokerKind.MOCK}),
        "risk": cfg.risk.model_copy(update={
            "reject_when_market_closed": True,
            "ticker_allowlist": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"],
            "max_notional_per_order": 500,
            "max_position_per_ticker": 2000,
        }),
    })


@pytest.fixture
def patch_selected_llm_backend(monkeypatch):
    def _patch(config: AppConfig, constructor) -> None:
        provider = config.llm.provider
        class_name = {
            "anthropic": "AnthropicBackend",
            "gemini": "GeminiBackend",
            "groq": "GroqBackend",
        }[provider]
        module = importlib.import_module(
            f"trading_assistant.llm.{provider}_backend"
        )
        monkeypatch.setattr(module, class_name, constructor)

    return _patch


@pytest.fixture
def with_limit():
    def _with_limit(
        app_config: AppConfig,
        name: str,
        *,
        requests: int,
        window_seconds: int,
    ) -> AppConfig:
        current = getattr(app_config.security.rate_limits, name)
        configured = WindowLimitConfig(
            requests=requests,
            global_requests=current.global_requests,
            window_seconds=window_seconds,
            concurrency=1,
            daily_requests=current.daily_requests,
            global_daily_requests=current.global_daily_requests,
        )
        limits = app_config.security.rate_limits.model_copy(
            update={name: configured}
        )
        security = app_config.security.model_copy(
            update={"rate_limits": limits}
        )
        return app_config.model_copy(update={"security": security})

    return _with_limit


@pytest.fixture
def db_url(tmp_path) -> str:
    """File-backed so a fresh engine on the same URL sees committed rows (A3, A5)."""
    return f"sqlite:///{tmp_path}/test.db"


@pytest.fixture
def engine(db_url):
    eng = create_db_engine(db_url)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def mock_broker() -> MockBroker:
    return MockBroker()


class SpyBroker(MockBroker):
    """MockBroker that records how many orders were actually sent to the broker."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.submit_calls = 0

    def submit_order(self, order):
        self.submit_calls += 1
        return super().submit_order(order)


@pytest.fixture
def make_service(app_config, session_factory):
    """Factory building a TradingService with a SpyBroker (AAPL priced at $100)."""
    from trading_assistant.risk.clock import FakeClock
    from trading_assistant.service import TradingService

    def _make(broker=None, market_open=True, quote_now=None):
        broker = (
            broker
            if broker is not None
            else (
                SpyBroker(now=quote_now)
                if quote_now is not None
                else SpyBroker()
            )
        )
        broker.set_price("AAPL", Decimal("100"))
        return TradingService(
            broker, session_factory, app_config, FakeClock(is_open=market_open)
        )

    return _make


@pytest.fixture
def operator_token() -> str:
    return TEST_OPERATOR_TOKEN


@pytest.fixture
def authenticate_client(operator_token):
    """Log in through the real route and validate the persisted session."""

    def _authenticate(client, token: str | None = None):
        if client.base_url.scheme == "http":
            client.base_url = client.base_url.copy_with(scheme="https")
        login = client.post(
            "/auth/login",
            json={"secret": token or operator_token},
        )
        assert login.status_code == 200, login.text
        session = client.get("/auth/session")
        assert session.status_code == 200, session.text
        assert session.json()["actor"] == "operator:local"
        csrf = session.json()["csrf_token"]
        return client, csrf

    return _authenticate


@pytest.fixture
def authenticated_client(make_service, operator_token, authenticate_client):
    from fastapi.testclient import TestClient

    from trading_assistant.app.main import create_app

    class _StubAgent:
        def chat(self, message, **context):
            return {"reply": "ok", "tool_calls": []}

    service = make_service()
    app = create_app(
        service=service,
        agent=_StubAgent(),
        api_token=operator_token,
        planning=None,
    )
    client = TestClient(app, base_url="https://localhost:8020")
    client.trading_service = service
    return authenticate_client(client)


@pytest.fixture
def make_snapshot():
    def _make(
        prices: dict[str, Decimal] | None = None,
        positions: list[Position] | None = None,
        buying_power: Decimal = Decimal("100000"),
        realized_pnl_today: Decimal = Decimal("0"),
        pending_signed_notional: dict[str, Decimal] | None = None,
        cash: Decimal = Decimal("100000"),
        unrealized_pnl_today: Decimal = Decimal("0"),
        daily_pnl_complete: bool = True,
        account_high_water_mark: Decimal = Decimal("100000"),
        account_equity: Decimal = Decimal("100000"),
        account_complete: bool = True,
        quote_fresh: bool = True,
        market_open: bool = True,
        market_clock_complete: bool = True,
        spread_pct_by_ticker: dict[str, Decimal] | None = None,
        pending_buy_notional_by_ticker: dict[str, Decimal] | None = None,
        reserved_sell_qty_by_ticker: dict[str, Decimal] | None = None,
        broker_reconciled: bool = True,
        active_breakers: frozenset[str] = frozenset(),
    ) -> PortfolioSnapshot:
        prices = prices or {}
        quote_time = datetime.now(timezone.utc)
        quotes = {
            t.upper(): Quote(
                ticker=t.upper(),
                bid=p,
                ask=p,
                last=p,
                prev_close=p,
                as_of=quote_time,
                book_as_of=quote_time,
                trade_as_of=quote_time,
            )
            for t, p in prices.items()
        }
        pos = {p.ticker.upper(): p for p in (positions or [])}
        return PortfolioSnapshot(
            positions=pos,
            quotes=quotes,
            buying_power=buying_power,
            realized_pnl_today=realized_pnl_today,
            cash=cash,
            unrealized_pnl_today=unrealized_pnl_today,
            daily_pnl_complete=daily_pnl_complete,
            account_high_water_mark=account_high_water_mark,
            account_equity=account_equity,
            account_complete=account_complete,
            quote_fresh=quote_fresh,
            market_open=market_open,
            market_clock_complete=market_clock_complete,
            spread_pct_by_ticker=spread_pct_by_ticker or {},
            pending_buy_notional_by_ticker=pending_buy_notional_by_ticker or {},
            reserved_sell_qty_by_ticker=reserved_sell_qty_by_ticker or {},
            broker_reconciled=broker_reconciled,
            active_breakers=active_breakers,
            pending_signed_notional=pending_signed_notional or {},
        )

    return _make
