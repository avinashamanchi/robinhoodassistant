"""Deterministic synthetic OHLCV generation for tests and offline development.

No network, fully seeded — the same inputs always produce the same bars, so
signal/engine tests are stable in CI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

_FREQ = {"D": timedelta(days=1), "H": timedelta(hours=1)}


def _index(n: int, start_ts: datetime, freq: str) -> pd.DatetimeIndex:
    step = _FREQ[freq]
    return pd.DatetimeIndex([start_ts + i * step for i in range(n)])


def ohlcv_from_closes(
    closes,
    start_ts: datetime | None = None,
    freq: str = "D",
    wick_frac: float = 0.004,
    volume=1_000_000.0,
) -> pd.DataFrame:
    """Build an OHLCV frame from a close series (open = prior close).

    ``volume`` may be a scalar or a per-bar array.
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    start_ts = start_ts or datetime(2015, 1, 1, tzinfo=timezone.utc)
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) * (1 + wick_frac)
    lows = np.minimum(opens, closes) * (1 - wick_frac)
    vol = np.full(n, volume) if np.isscalar(volume) else np.asarray(volume, dtype=float)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vol},
        index=_index(n, start_ts, freq),
    )
    df.index.name = "ts"
    return df


def make_bars(
    n: int = 300,
    start: float = 100.0,
    drift: float = 0.0004,
    vol: float = 0.01,
    seed: int = 0,
    start_ts: datetime | None = None,
    freq: str = "D",
    volume: float = 1_000_000.0,
) -> pd.DataFrame:
    """Geometric-random-walk bars with a controllable drift and volatility."""
    rng = np.random.default_rng(seed)
    shocks = rng.normal(drift, vol, n)
    closes = start * np.exp(np.cumsum(shocks))
    # Volume spikes on large moves (so breakouts get realistic volume confirmation).
    abs_move = np.abs(shocks) / (vol if vol > 0 else 1.0)
    volumes = volume * (0.6 + 1.1 * abs_move) * rng.lognormal(0.0, 0.25, n)
    return ohlcv_from_closes(closes, start_ts, freq, volume=volumes)


def make_trend(n_base: int, n_move: int, start: float, end: float, **kw) -> pd.DataFrame:
    """Flat base then a linear ramp to ``end`` — deterministic crossovers for tests."""
    base = np.full(n_base, start, dtype=float)
    ramp = np.linspace(start, end, n_move)
    return ohlcv_from_closes(np.concatenate([base, ramp]), **kw)
