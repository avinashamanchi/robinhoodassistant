"""Regime classification: ADX (trend strength) + SMA slope (direction) +
realized-vol percentile (stress). All inputs are causal indicators, so the
classification at t uses only data <= t.
"""

from __future__ import annotations

import pandas as pd

from . import indicators as ind
from .models import Regime

ADX_TREND = 25.0
HIGH_VOL_PCTILE = 80.0


def realized_vol_percentile(df: pd.DataFrame, n: int = 20, window: int = 252) -> float | None:
    """Percentile rank (0-100) of the latest realized vol vs its trailing window."""
    vol = ind.realized_vol(df["close"], n).dropna()
    if vol.empty:
        return None
    tail = vol.iloc[-window:]
    latest = vol.iloc[-1]
    return float((tail <= latest).mean() * 100)


def classify(df: pd.DataFrame) -> Regime | None:
    if len(df) < 30:
        return None
    adx = ind.adx(df, 14).iloc[-1]
    slope = ind.slope_pct(ind.sma(df["close"], 50), 5).iloc[-1]
    vol_pct = realized_vol_percentile(df)

    if pd.notna(adx) and adx >= ADX_TREND and pd.notna(slope):
        return Regime.TRENDING_UP if slope > 0 else Regime.TRENDING_DOWN
    if vol_pct is not None and vol_pct >= HIGH_VOL_PCTILE:
        return Regime.HIGH_VOLATILITY
    return Regime.RANGING
