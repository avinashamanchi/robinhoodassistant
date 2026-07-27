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

from ..security.outbound import (
    DEFAULT_MAX_RESPONSE_BYTES,
    OutboundError,
    OutboundPolicy,
    OutboundRequestFailed,
    new_httpx_client,
    read_bounded_json,
)
from .data import cache_path, load_parquet

BASE = "https://api.marketstack.com/v1"
_ORIGIN_POLICY = OutboundPolicy("https://api.marketstack.com")


class MarketStackClient:
    def __init__(
        self,
        api_key: str,
        http: Any = None,
        cache_dir: str | Path = ".cache/bars",
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._cache_dir = cache_dir
        self._max_response_bytes = max_response_bytes

    def _client(self):
        if self._http is None:
            self._http = new_httpx_client(_ORIGIN_POLICY, read_timeout=30.0)
        return self._http

    def _get(self, path: str, params: dict) -> dict:
        url = f"{BASE}{path}"
        request_params = {**params, "access_key": self._api_key}
        _ORIGIN_POLICY.assert_url(url)
        try:
            with self._client().stream("GET", url, params=request_params) as resp:
                _ORIGIN_POLICY.assert_response(resp)
                resp.raise_for_status()
                return read_bounded_json(
                    resp,
                    max_response_bytes=self._max_response_bytes,
                )
        except OutboundError:
            raise
        except Exception:
            raise OutboundRequestFailed() from None

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
