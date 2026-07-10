"""Historical data access with a structural no-lookahead guarantee.

A ``DataView`` is pinned to a simulated time t. It can only ever return rows with
timestamp <= t — there is no method that yields future rows, and asking for a
timestamp beyond t raises ``LookaheadError``. This makes lookahead bias a type
error, not a discipline (Phase 7 guardrail #4).

SPY market-context bars go through the SAME view, so market context cannot leak
future data either.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd


class LookaheadError(Exception):
    """Raised when code requests data at a timestamp after the view's current t."""


class DataSource:
    """Holds full per-symbol OHLCV history and mints time-bounded DataViews."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = {sym: df.sort_index() for sym, df in frames.items()}

    @property
    def symbols(self) -> list[str]:
        return list(self._frames)

    def full(self, symbol: str) -> pd.DataFrame:
        return self._frames[symbol]

    def timeline(self, symbols: Iterable[str] | None = None) -> list[datetime]:
        """Sorted union of all bar timestamps across the given symbols."""
        syms = list(symbols) if symbols is not None else self.symbols
        idx = pd.DatetimeIndex([])
        for s in syms:
            idx = idx.union(self._frames[s].index)
        return [ts.to_pydatetime() for ts in idx]

    def view(self, t: datetime) -> "DataView":
        return DataView(self._frames, t)


class DataView:
    def __init__(self, frames: dict[str, pd.DataFrame], t: datetime) -> None:
        self._frames = frames
        self._t = t

    @property
    def t(self) -> datetime:
        return self._t

    def history(self, symbol: str, lookback: int | None = None) -> pd.DataFrame:
        """Bars with ts <= t (optionally only the last ``lookback`` of them)."""
        df = self._frames[symbol]
        visible = df.loc[df.index <= self._t]
        return visible.tail(lookback) if lookback else visible

    def current_bar(self, symbol: str) -> pd.Series | None:
        """The bar exactly at t, if the symbol trades at t; else None."""
        df = self._frames[symbol]
        exact = df.loc[df.index == self._t]
        return exact.iloc[-1] if len(exact) else None

    def last_close(self, symbol: str) -> float | None:
        hist = self.history(symbol, 1)
        return float(hist["close"].iloc[-1]) if len(hist) else None

    def get_at(self, symbol: str, ts: datetime) -> pd.Series:
        """Explicit point lookup — raises if ts is in the future relative to t."""
        if ts > self._t:
            raise LookaheadError(
                f"requested {symbol} @ {ts} but view is pinned at {self._t}"
            )
        df = self._frames[symbol]
        rows = df.loc[df.index == ts]
        if not len(rows):
            raise KeyError(f"no {symbol} bar at {ts}")
        return rows.iloc[-1]


# ── Alpaca-backed parquet cache (real data; synthetic used in CI) ───────
def load_parquet(path: str | Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.set_index("ts")
    return df.sort_index()


def cache_path(cache_dir: str | Path, symbol: str, timeframe: str) -> Path:
    safe = symbol.replace("/", "_")
    return Path(cache_dir) / f"{safe}_{timeframe}.parquet"


def download_alpaca_bars(
    symbol: str,
    api_key: str,
    secret_key: str,
    timeframe: str = "1Day",
    years: int = 5,
    cache_dir: str | Path = ".cache/bars",
) -> pd.DataFrame:
    """Download corporate-action-adjusted bars and cache to parquet.

    Kept dependency-light and lazy: only imported/exercised when real credentials
    are supplied. CI never calls this — it uses ``backtest.synthetic``.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    path = cache_path(cache_dir, symbol, timeframe)
    if path.exists():
        return load_parquet(path)

    client = StockHistoricalDataClient(api_key, secret_key)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    start = start.replace(year=start.year - years)
    tf = TimeFrame.Day if timeframe == "1Day" else TimeFrame.Hour
    req = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=tf, start=start, adjustment="all"
    )
    bars = client.get_stock_bars(req).df
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol, level="symbol")
    bars = bars.rename_axis("ts")[["open", "high", "low", "close", "volume"]]
    path.parent.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(path)
    return bars
