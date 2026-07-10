"""Build MarketFeatures from real bars for the live /analyze + /screen paths.

Equities: Alpaca daily bars (adjusted). Crypto: CoinGecko. SPY provides market
context. Everything is cached to parquet by the underlying loaders, so repeat
calls are cheap. Kept lazy/defensive so a missing key degrades to a clear error
rather than crashing app startup.
"""

from __future__ import annotations

from typing import Callable, Optional

from ..assets import AssetClass
from ..signals.features import build_features
from ..signals.models import MarketFeatures


def _fetch_equity_df(symbol: str, secrets, years: int = 2):
    from ..backtest.data import download_alpaca_bars

    return download_alpaca_bars(
        symbol, secrets.alpaca_api_key, secrets.alpaca_secret_key,
        timeframe="1Day", years=years,
    )


def _fetch_crypto_df(symbol: str, days: int = 365):
    from ..backtest.coingecko import CoinGeckoClient

    return CoinGeckoClient().bars(symbol, days=days)


def build_live_feature_provider(config, secrets) -> Callable[[str], MarketFeatures]:
    def provider(symbol: str) -> MarketFeatures:
        ac = AssetClass.for_symbol(symbol)
        df = _fetch_crypto_df(symbol) if ac is AssetClass.CRYPTO else _fetch_equity_df(symbol, secrets)
        spy_df = None
        try:
            spy_df = _fetch_equity_df("SPY", secrets)
        except Exception:
            spy_df = None
        return build_features(symbol, ac, df, spy_df=spy_df)

    return provider


def build_screen_source(universe: list[str], secrets):
    """Build a DataSource across the universe (+ SPY) from cached bars."""
    from ..backtest.coingecko import CoinGeckoClient
    from ..backtest.data import DataSource, download_alpaca_bars

    frames = {}
    for sym in set(universe) | {"SPY"}:
        try:
            if AssetClass.for_symbol(sym) is AssetClass.CRYPTO:
                frames[sym] = CoinGeckoClient().bars(sym)
            else:
                frames[sym] = download_alpaca_bars(
                    sym, secrets.alpaca_api_key, secrets.alpaca_secret_key,
                    timeframe="1Day", years=2,
                )
        except Exception:
            continue
    return DataSource(frames)
