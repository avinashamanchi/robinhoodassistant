"""Named event detectors. Each returns typed, timestamped EventTags evaluated at
the last available bar (ts <= t). No detector reads beyond the last row.
"""

from __future__ import annotations

import pandas as pd

from . import indicators as ind
from .models import EventTag, EventType

GAP_THRESHOLD_PCT = 2.0
BREAKOUT_VOL_MULT = 1.5
BREAKOUT_LOOKBACK = 20
RSI_LOW = 30.0
RSI_HIGH = 70.0
SQUEEZE_LOOKBACK = 120


def _rsi_divergence(close: pd.Series, rsi: pd.Series, window: int = 20) -> bool:
    """Bullish divergence: price lower low while RSI higher low over the window."""
    if len(close) < window + 1:
        return False
    c = close.iloc[-window:]
    r = rsi.iloc[-window:]
    price_lower_low = c.iloc[-1] <= c.min() * 1.001
    rsi_higher_low = r.iloc[-1] > r.min() * 1.02
    return bool(price_lower_low and rsi_higher_low)


def detect_events(df: pd.DataFrame) -> list[EventTag]:
    if len(df) < 2:
        return []
    ts = df.index[-1].to_pydatetime()
    events: list[EventTag] = []

    close = df["close"]
    sma50 = ind.sma(close, 50)
    sma200 = ind.sma(close, 200)
    rsi = ind.rsi(close, 14)
    _, _, _, bandwidth = ind.bollinger(close, 20)
    vol_mult = ind.volume_vs_avg(df["volume"], 20)

    # Golden / death cross (50 vs 200).
    if pd.notna(sma50.iloc[-1]) and pd.notna(sma200.iloc[-1]) and pd.notna(sma50.iloc[-2]):
        if sma50.iloc[-1] > sma200.iloc[-1] and sma50.iloc[-2] <= sma200.iloc[-2]:
            events.append(EventTag(type=EventType.GOLDEN_CROSS, ts=ts))
        elif sma50.iloc[-1] < sma200.iloc[-1] and sma50.iloc[-2] >= sma200.iloc[-2]:
            events.append(EventTag(type=EventType.DEATH_CROSS, ts=ts))

    # Breakout / breakdown vs prior range, volume-confirmed.
    if len(df) > BREAKOUT_LOOKBACK + 1:
        prior = df.iloc[-(BREAKOUT_LOOKBACK + 1) : -1]
        prior_high = float(prior["high"].max())
        prior_low = float(prior["low"].min())
        last_close = float(close.iloc[-1])
        confirmed = bool(pd.notna(vol_mult.iloc[-1]) and vol_mult.iloc[-1] > BREAKOUT_VOL_MULT)
        if last_close > prior_high and confirmed:
            events.append(
                EventTag(type=EventType.BREAKOUT, ts=ts, meta={"level": prior_high})
            )
        elif last_close < prior_low and confirmed:
            events.append(
                EventTag(type=EventType.BREAKDOWN, ts=ts, meta={"level": prior_low})
            )

    # RSI extremes (with divergence flag on oversold).
    if pd.notna(rsi.iloc[-1]):
        if rsi.iloc[-1] < RSI_LOW:
            events.append(
                EventTag(
                    type=EventType.RSI_OVERSOLD,
                    ts=ts,
                    meta={"rsi": round(float(rsi.iloc[-1]), 2),
                          "divergence": _rsi_divergence(close, rsi)},
                )
            )
        elif rsi.iloc[-1] > RSI_HIGH:
            events.append(
                EventTag(type=EventType.RSI_OVERBOUGHT, ts=ts,
                         meta={"rsi": round(float(rsi.iloc[-1]), 2)})
            )

    # Bollinger squeeze: bandwidth in the bottom decile of its recent history.
    bw_hist = bandwidth.dropna().iloc[-SQUEEZE_LOOKBACK:]
    if len(bw_hist) >= 20 and pd.notna(bandwidth.iloc[-1]):
        if bandwidth.iloc[-1] <= bw_hist.quantile(0.10):
            events.append(EventTag(type=EventType.BB_SQUEEZE, ts=ts))

    # Gaps.
    prev_close = float(close.iloc[-2])
    if prev_close:
        gap = (float(df["open"].iloc[-1]) - prev_close) / prev_close * 100
        if gap > GAP_THRESHOLD_PCT:
            events.append(EventTag(type=EventType.GAP_UP, ts=ts, meta={"gap_pct": round(gap, 2)}))
        elif gap < -GAP_THRESHOLD_PCT:
            events.append(EventTag(type=EventType.GAP_DOWN, ts=ts, meta={"gap_pct": round(gap, 2)}))

    return events
