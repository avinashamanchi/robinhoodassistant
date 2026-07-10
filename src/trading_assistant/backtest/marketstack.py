"""MarketStack equities data (EOD, splits, dividends, tickers, exchanges).

The free tier allows only ~100 requests/month, so EOD bars are cached to parquet
and reused indefinitely — one request per symbol backfills years of daily bars.
Uses adjusted prices when available (corporate-action aware).

The HTTP client is injectable so parsing is unit-tested without network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .data import cache_path, load_parquet

BASE = "https://api.marketstack.com/v1"


class MarketStackClient:
    def __init__(
        self, api_key: str, http: Any = None, cache_dir: str | Path = ".cache/bars"
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._cache_dir = cache_dir

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=30.0)
        return self._http

    def _get(self, path: str, params: dict) -> dict:
        resp = self._client().get(
            f"{BASE}{path}", params={**params, "access_key": self._api_key}
        )
        resp.raise_for_status()
        return resp.json()

    # ── EOD bars -> OHLCV frame (cached) ───────────────────────
    def eod(self, symbol: str, limit: int = 1000, use_cache: bool = True) -> pd.DataFrame:
        path = cache_path(self._cache_dir, symbol, "marketstack_eod")
        if use_cache and Path(path).exists():
            return load_parquet(path)

        payload = self._get("/eod", {"symbols": symbol.upper(), "limit": limit})
        rows = payload.get("data", [])
        frame = _rows_to_ohlcv(rows)
        if not frame.empty:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(path)
        return frame

    # ── other read endpoints ───────────────────────────────────
    def splits(self, symbol: str) -> list[dict]:
        return self._get("/splits", {"symbols": symbol.upper()}).get("data", [])

    def dividends(self, symbol: str) -> list[dict]:
        return self._get("/dividends", {"symbols": symbol.upper()}).get("data", [])

    def tickers(self, limit: int = 100) -> list[dict]:
        return self._get("/tickers", {"limit": limit}).get("data", [])

    def exchanges(self, limit: int = 100) -> list[dict]:
        return self._get("/exchanges", {"limit": limit}).get("data", [])


def _rows_to_ohlcv(rows: list[dict]) -> pd.DataFrame:
    """Adjusted OHLCV (falls back to raw), indexed by UTC date, ascending."""
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    records = []
    for r in rows:
        records.append(
            {
                "ts": pd.to_datetime(r["date"], utc=True),
                "open": float(r.get("adj_open") or r.get("open") or 0),
                "high": float(r.get("adj_high") or r.get("high") or 0),
                "low": float(r.get("adj_low") or r.get("low") or 0),
                "close": float(r.get("adj_close") or r.get("close") or 0),
                "volume": float(r.get("adj_volume") or r.get("volume") or 0),
            }
        )
    df = pd.DataFrame.from_records(records).set_index("ts").sort_index()
    df.index.name = "ts"
    return df


def load_marketstack_source(symbols: list[str], api_key: str, **kw):
    """Build a DataSource from MarketStack EOD bars (cached per symbol)."""
    from .data import DataSource

    client = MarketStackClient(api_key, **kw)
    return DataSource({s.upper(): client.eod(s) for s in symbols})
