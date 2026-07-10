"""CoinGecko crypto data (no API key required).

Recommended crypto source: free, no auth, historical OHLC + volume. OHLC comes
from /ohlc; daily volume from /market_chart; the two are merged by date into an
OHLCV frame compatible with the backtest engine and cached to parquet.

Maps our crypto symbols (e.g. BTC/USD) to CoinGecko ids (bitcoin). HTTP client is
injectable so parsing is unit-tested without network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .data import cache_path, load_parquet

BASE = "https://api.coingecko.com/api/v3"

SYMBOL_TO_ID = {
    "BTC/USD": "bitcoin",
    "ETH/USD": "ethereum",
    "SOL/USD": "solana",
    "BTC": "bitcoin",
    "ETH": "ethereum",
}


def coin_id(symbol: str) -> str:
    return SYMBOL_TO_ID.get(symbol.upper(), symbol.lower().replace("/usd", ""))


class CoinGeckoClient:
    def __init__(self, http: Any = None, cache_dir: str | Path = ".cache/bars") -> None:
        self._http = http
        self._cache_dir = cache_dir

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=30.0)
        return self._http

    def _get(self, path: str, params: dict) -> Any:
        resp = self._client().get(f"{BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def ohlc(self, symbol: str, days: int = 365) -> list:
        return self._get(
            f"/coins/{coin_id(symbol)}/ohlc", {"vs_currency": "usd", "days": days}
        )

    def volumes(self, symbol: str, days: int = 365) -> list:
        data = self._get(
            f"/coins/{coin_id(symbol)}/market_chart",
            {"vs_currency": "usd", "days": days, "interval": "daily"},
        )
        return data.get("total_volumes", [])

    def bars(self, symbol: str, days: int = 365, use_cache: bool = True) -> pd.DataFrame:
        path = cache_path(self._cache_dir, symbol, "coingecko")
        if use_cache and Path(path).exists():
            return load_parquet(path)
        frame = _merge_ohlc_volume(self.ohlc(symbol, days), self.volumes(symbol, days))
        if not frame.empty:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(path)
        return frame


def _merge_ohlc_volume(ohlc: list, volumes: list) -> pd.DataFrame:
    if not ohlc:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    vol_by_day: dict = {}
    for ts_ms, vol in volumes:
        day = pd.to_datetime(ts_ms, unit="ms", utc=True).normalize()
        vol_by_day[day] = float(vol)
    records = []
    for ts_ms, o, h, l, c in ohlc:
        ts = pd.to_datetime(ts_ms, unit="ms", utc=True)
        records.append(
            {
                "ts": ts,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": vol_by_day.get(ts.normalize(), 0.0),
            }
        )
    df = pd.DataFrame.from_records(records).set_index("ts").sort_index()
    df.index.name = "ts"
    # Collapse any intraday OHLC points to one row per day (CoinGecko returns 4h
    # candles for long ranges); keep the last of each day.
    df = df[~df.index.normalize().duplicated(keep="last")]
    return df


def load_coingecko_source(symbols: list[str], days: int = 365, **kw):
    from .data import DataSource

    client = CoinGeckoClient(**kw)
    return DataSource({s.upper(): client.bars(s, days) for s in symbols})
