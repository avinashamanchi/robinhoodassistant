"""Assemble a MarketFeatures bundle from a bounded bars frame.

The caller (backtest engine / live snapshot) passes ``df`` containing ONLY bars
with ts <= t (via a DataView). SPY context is passed the same way, so
market_context and relative_strength cannot see future SPY data — the single
place lookahead is most likely to sneak in.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import pandas as pd

from ..assets import AssetClass
from . import indicators as ind
from . import regime as rg
from . import structure as st
from .events import detect_events
from .models import (
    Bar,
    BollingerValue,
    MacdValue,
    MarketContext,
    MarketFeatures,
    RelativeStrength,
)


def _f(value) -> Optional[float]:
    """Coerce a pandas/NumPy scalar to float, or None if missing/NaN."""
    if value is None or pd.isna(value):
        return None
    return float(value)


def _return_pct(close: pd.Series, n: int) -> Optional[float]:
    if len(close) < n + 1:
        return None
    past = close.iloc[-n - 1]
    if past == 0:
        return None
    return float((close.iloc[-1] / past - 1) * 100)


def _market_context(spy_df: Optional[pd.DataFrame]) -> MarketContext:
    if spy_df is None or len(spy_df) < 30:
        return MarketContext()
    return MarketContext(
        spy_regime=rg.classify(spy_df),
        spy_realized_vol_20_pct=rg.realized_vol_percentile(spy_df),
    )


def _relative_strength(
    close: pd.Series, spy_df: Optional[pd.DataFrame]
) -> RelativeStrength:
    if spy_df is None:
        return RelativeStrength()
    spy_close = spy_df["close"]
    rs = RelativeStrength()
    for attr, n in (("rs_20d", 20), ("rs_60d", 60)):
        a = _return_pct(close, n)
        b = _return_pct(spy_close, n)
        if a is not None and b is not None:
            setattr(rs, attr, round(a - b, 4))
    return rs


def build_features(
    symbol: str,
    asset_class: AssetClass,
    df: pd.DataFrame,
    *,
    spy_df: Optional[pd.DataFrame] = None,
    earnings_date: Optional[date] = None,
    as_of: Optional[datetime] = None,
    recent_bars: int = 20,
) -> MarketFeatures:
    close = df["close"]
    as_of = as_of or df.index[-1].to_pydatetime()

    macd_line, macd_sig, macd_hist = ind.macd(close)
    bb_u, bb_m, bb_l, bb_bw = ind.bollinger(close, 20)
    support, resistance = st.swing_levels(df)
    dist_hi, dist_lo = st.distance_to_52w(df)
    up_days, down_days = st.consecutive_days(df)

    days_to_earnings = None
    if earnings_date is not None and asset_class is not AssetClass.CRYPTO:
        days_to_earnings = (earnings_date - as_of.date()).days

    bars = [
        Bar(
            ts=idx.to_pydatetime(),
            open=float(r.open),
            high=float(r.high),
            low=float(r.low),
            close=float(r.close),
            volume=float(r.volume),
        )
        for idx, r in df.tail(recent_bars).iterrows()
    ]

    return MarketFeatures(
        symbol=symbol,
        asset_class=asset_class,
        as_of=as_of,
        recent_bars=bars,
        last_close=_f(close.iloc[-1]),
        prev_close=_f(close.iloc[-2]) if len(close) >= 2 else None,
        sma_20=_f(ind.sma(close, 20).iloc[-1]),
        sma_50=_f(ind.sma(close, 50).iloc[-1]),
        sma_200=_f(ind.sma(close, 200).iloc[-1]),
        ema_20=_f(ind.ema(close, 20).iloc[-1]),
        ema_50=_f(ind.ema(close, 50).iloc[-1]),
        ema_200=_f(ind.ema(close, 200).iloc[-1]),
        macd=MacdValue(
            line=_f(macd_line.iloc[-1]),
            signal=_f(macd_sig.iloc[-1]),
            hist=_f(macd_hist.iloc[-1]),
        ),
        adx_14=_f(ind.adx(df, 14).iloc[-1]),
        sma50_slope=_f(ind.slope_pct(ind.sma(close, 50), 5).iloc[-1]),
        price_vs_sma200_pct=(
            _f((close.iloc[-1] / ind.sma(close, 200).iloc[-1] - 1) * 100)
            if _f(ind.sma(close, 200).iloc[-1])
            else None
        ),
        rsi_14=_f(ind.rsi(close, 14).iloc[-1]),
        roc_10=_f(ind.roc(close, 10).iloc[-1]),
        atr_14=_f(ind.atr(df, 14).iloc[-1]),
        bollinger=BollingerValue(
            upper=_f(bb_u.iloc[-1]),
            mid=_f(bb_m.iloc[-1]),
            lower=_f(bb_l.iloc[-1]),
            bandwidth=_f(bb_bw.iloc[-1]),
        ),
        realized_vol_20=_f(ind.realized_vol(close, 20).iloc[-1]),
        volume=_f(df["volume"].iloc[-1]),
        volume_vs_avg20=_f(ind.volume_vs_avg(df["volume"], 20).iloc[-1]),
        support_levels=support,
        resistance_levels=resistance,
        dist_to_52w_high_pct=dist_hi,
        dist_to_52w_low_pct=dist_lo,
        gap_pct=st.last_gap_pct(df),
        consecutive_up_days=up_days,
        consecutive_down_days=down_days,
        events=detect_events(df),
        regime=rg.classify(df),
        days_to_next_earnings=days_to_earnings,
        market_context=_market_context(spy_df),
        relative_strength_vs_spy=_relative_strength(close, spy_df),
    )
