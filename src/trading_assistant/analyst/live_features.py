"""Build MarketFeatures from real bars for the live /analyze + /screen paths.

Equities: Alpaca daily bars (adjusted). Crypto: CoinGecko. SPY provides market
context. Everything is cached to parquet by the underlying loaders, so repeat
calls are cheap. Kept lazy/defensive so a missing key degrades to a clear error
rather than crashing app startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..app.limits import LimitStoreUnavailable
from ..assets import AssetClass
from ..dependencies import RequiredDependencyUnavailable
from ..signals.features import build_features
from ..signals.models import MarketFeatures


def _historical_attempt_gate(
    config,
    *,
    service,
    rate_limiter,
    symbol: str,
    principal: str,
):
    if service is None and rate_limiter is None:
        return None
    if service is None or rate_limiter is None or config is None:
        raise ValueError(
            "scheduled historical reads require config, service, and limiter"
        )
    from ..daemon.backoff import (
        ScheduledMarketDataDenied,
        scheduled_market_data_read,
        trip_scheduled_market_data_breaker,
    )

    def gate(operation):
        try:
            return scheduled_market_data_read(
                operation,
                rate_limiter=rate_limiter,
                limit_config=(
                    config.security.rate_limits.provider_read
                ),
                principal=principal,
            )
        except (ScheduledMarketDataDenied, LimitStoreUnavailable):
            trip_scheduled_market_data_breaker(
                service,
                symbol,
                actor="daemon:historical",
                request_id=f"historical-read:{uuid4().hex}",
                audit_reason=(
                    "daemon scheduled historical market data read"
                ),
            )
            raise RequiredDependencyUnavailable from None

    return gate


def _fetch_equity_df(
    symbol: str,
    secrets,
    years: int = 2,
    *,
    config=None,
    service=None,
    rate_limiter=None,
    client_factory=None,
    cache_dir: str | Path = ".cache/bars",
):
    from ..backtest.data import download_alpaca_bars
    from ..daemon.backoff import ALPACA_MARKET_DATA_PRINCIPAL

    return download_alpaca_bars(
        symbol,
        secrets.alpaca_api_key,
        secrets.alpaca_secret_key,
        timeframe="1Day",
        years=years,
        cache_dir=cache_dir,
        client_factory=client_factory,
        attempt_gate=_historical_attempt_gate(
            config,
            service=service,
            rate_limiter=rate_limiter,
            symbol=symbol,
            principal=ALPACA_MARKET_DATA_PRINCIPAL,
        ),
    )


def _fetch_crypto_df(
    symbol: str,
    days: int = 365,
    *,
    config=None,
    service=None,
    rate_limiter=None,
    http: Any = None,
    cache_dir: str | Path = ".cache/bars",
):
    from ..backtest.coingecko import CoinGeckoClient
    from ..daemon.backoff import COINGECKO_MARKET_DATA_PRINCIPAL

    return CoinGeckoClient(
        http=http,
        cache_dir=cache_dir,
        attempt_gate=_historical_attempt_gate(
            config,
            service=service,
            rate_limiter=rate_limiter,
            symbol=symbol,
            principal=COINGECKO_MARKET_DATA_PRINCIPAL,
        ),
    ).bars(symbol, days=days)


def build_live_feature_provider(
    config,
    secrets,
    *,
    scheduled_service=None,
    rate_limiter=None,
    alpaca_client_factory=None,
    coingecko_http: Any = None,
    cache_dir: str | Path = ".cache/bars",
) -> Callable[[str], MarketFeatures]:
    def provider(symbol: str) -> MarketFeatures:
        ac = AssetClass.for_symbol(symbol)
        try:
            df = (
                _fetch_crypto_df(
                    symbol,
                    config=config,
                    service=scheduled_service,
                    rate_limiter=rate_limiter,
                    http=coingecko_http,
                    cache_dir=cache_dir,
                )
                if ac is AssetClass.CRYPTO
                else _fetch_equity_df(
                    symbol,
                    secrets,
                    config=config,
                    service=scheduled_service,
                    rate_limiter=rate_limiter,
                    client_factory=alpaca_client_factory,
                    cache_dir=cache_dir,
                )
            )
        except RequiredDependencyUnavailable:
            raise
        except Exception:
            raise RequiredDependencyUnavailable from None
        spy_df = None
        try:
            spy_df = _fetch_equity_df(
                "SPY",
                secrets,
                config=config,
                service=scheduled_service,
                rate_limiter=rate_limiter,
                client_factory=alpaca_client_factory,
                cache_dir=cache_dir,
            )
        except Exception:
            spy_df = None
        return build_features(symbol, ac, df, spy_df=spy_df)

    return provider


def build_screen_source(
    universe: list[str],
    secrets,
    *,
    config=None,
    scheduled_service=None,
    rate_limiter=None,
    alpaca_client_factory=None,
    coingecko_http: Any = None,
    cache_dir: str | Path = ".cache/bars",
):
    """Build a DataSource across the universe (+ SPY) from cached bars."""
    from ..backtest.data import DataSource

    requested = set(universe)
    frames = {}
    for sym in requested | {"SPY"}:
        try:
            if AssetClass.for_symbol(sym) is AssetClass.CRYPTO:
                frames[sym] = _fetch_crypto_df(
                    sym,
                    config=config,
                    service=scheduled_service,
                    rate_limiter=rate_limiter,
                    http=coingecko_http,
                    cache_dir=cache_dir,
                )
            else:
                frames[sym] = _fetch_equity_df(
                    sym,
                    secrets,
                    config=config,
                    service=scheduled_service,
                    rate_limiter=rate_limiter,
                    client_factory=alpaca_client_factory,
                    cache_dir=cache_dir,
                )
        except Exception:
            continue
    if not requested.intersection(frames):
        raise RequiredDependencyUnavailable
    return DataSource(frames)
