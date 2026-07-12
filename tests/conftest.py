"""Shared fixtures: risk config, file-backed SQLite, MockBroker, snapshot builder."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import PortfolioSnapshot, Position, Quote
from trading_assistant.config import AppConfig, BrokerKind, RiskConfig, load_config
from trading_assistant.db.models import create_all
from trading_assistant.db.session import create_db_engine, make_session_factory

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    """The committed config.yaml, NORMALIZED to stable test defaults (mock broker,
    reject-when-closed on) so operational config edits (e.g. switching to live
    Alpaca paper) can never break the test baseline."""
    cfg = load_config(REPO_ROOT / "config.yaml")
    return cfg.model_copy(update={
        "trading": cfg.trading.model_copy(update={"broker": BrokerKind.MOCK}),
        "risk": cfg.risk.model_copy(update={"reject_when_market_closed": True}),
    })


@pytest.fixture
def db_url(tmp_path) -> str:
    """File-backed so a fresh engine on the same URL sees committed rows (A3, A5)."""
    return f"sqlite:///{tmp_path}/test.db"


@pytest.fixture
def engine(db_url):
    eng = create_db_engine(db_url)
    create_all(eng)
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

    def _make(broker=None, market_open=True):
        broker = broker if broker is not None else SpyBroker()
        broker.set_price("AAPL", Decimal("100"))
        return TradingService(
            broker, session_factory, app_config, FakeClock(is_open=market_open)
        )

    return _make


@pytest.fixture
def make_snapshot():
    def _make(
        prices: dict[str, Decimal] | None = None,
        positions: list[Position] | None = None,
        buying_power: Decimal = Decimal("100000"),
        realized_pnl_today: Decimal = Decimal("0"),
    ) -> PortfolioSnapshot:
        prices = prices or {}
        quotes = {
            t.upper(): Quote(ticker=t.upper(), bid=p, ask=p, last=p, prev_close=p)
            for t, p in prices.items()
        }
        pos = {p.ticker.upper(): p for p in (positions or [])}
        return PortfolioSnapshot(
            positions=pos,
            quotes=quotes,
            buying_power=buying_power,
            realized_pnl_today=realized_pnl_today,
        )

    return _make
