"""Shared fixtures: risk config, file-backed SQLite, MockBroker, snapshot builder."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import PortfolioSnapshot, Position, Quote
from trading_assistant.config import RiskConfig
from trading_assistant.db.models import create_all
from trading_assistant.db.session import create_db_engine, make_session_factory


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
