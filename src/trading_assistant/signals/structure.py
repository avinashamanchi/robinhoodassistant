"""Price-structure signals: support/resistance, 52-week distance, gaps, streaks.

Pivots require ``right`` confirming bars after them, so a pivot is only reported
once those bars exist — near the right edge (the most recent ``right`` bars) no
pivot is claimed. That is correct and lookahead-free: at time t we genuinely
don't yet know whether t is a swing point.
"""

from __future__ import annotations

import pandas as pd


def swing_levels(
    df: pd.DataFrame, left: int = 3, right: int = 3, max_levels: int = 5
) -> tuple[list[float], list[float]]:
    """Return (support_levels, resistance_levels) from recent confirmed pivots."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    resistance: list[float] = []
    support: list[float] = []
    for i in range(left, n - right):
        window_hi = highs[i - left : i + right + 1]
        window_lo = lows[i - left : i + right + 1]
        if highs[i] == window_hi.max():
            resistance.append(float(highs[i]))
        if lows[i] == window_lo.min():
            support.append(float(lows[i]))
    # Most recent, de-duplicated, capped.
    def _recent(levels: list[float]) -> list[float]:
        seen: list[float] = []
        for lv in reversed(levels):
            if lv not in seen:
                seen.append(lv)
            if len(seen) >= max_levels:
                break
        return sorted(seen)

    return _recent(support), _recent(resistance)


def distance_to_52w(df: pd.DataFrame, window: int = 252) -> tuple[float | None, float | None]:
    """(% below trailing high, % above trailing low), as of the last bar."""
    if len(df) < 2:
        return None, None
    tail = df.tail(window)
    hi = float(tail["high"].max())
    lo = float(tail["low"].min())
    last = float(df["close"].iloc[-1])
    dist_high = (last - hi) / hi * 100 if hi else None
    dist_low = (last - lo) / lo * 100 if lo else None
    return dist_high, dist_low


def last_gap_pct(df: pd.DataFrame) -> float | None:
    """Gap of the last bar: (open - prior close) / prior close, in percent."""
    if len(df) < 2:
        return None
    prev_close = float(df["close"].iloc[-2])
    open_ = float(df["open"].iloc[-1])
    if prev_close == 0:
        return None
    return (open_ - prev_close) / prev_close * 100


def consecutive_days(df: pd.DataFrame) -> tuple[int, int]:
    """(consecutive up days, consecutive down days) ending at the last bar."""
    diffs = df["close"].diff().dropna()
    up = down = 0
    for d in reversed(diffs.to_list()):
        if d > 0 and down == 0:
            up += 1
        elif d < 0 and up == 0:
            down += 1
        else:
            break
    return up, down
