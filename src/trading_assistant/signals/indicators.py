"""Technical indicators computed deterministically from OHLCV bars.

Every function is causal — it uses ``rolling`` / ``ewm`` which only look backward,
so an indicator value at row t depends only on rows <= t. This is what makes the
no-lookahead guarantee hold even before the DataView wraps the data.

Inputs are pandas objects indexed by (UTC) timestamp. Functions return full Series
so event detectors can find crossings; feature assembly takes the last value.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    return close.ewm(span=n, adjust=False).mean()


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    signal_line = line.ewm(span=signal, adjust=False).mean()
    return line, signal_line, line - signal_line


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder smoothing.
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out[avg_loss == 0] = 100  # no losses -> maximally overbought
    return out


def roc(close: pd.Series, n: int = 10) -> pd.Series:
    return close.pct_change(n) * 100


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False).mean()


def bollinger(
    close: pd.Series, n: int = 20, k: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(n).mean()
    std = close.rolling(n).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    bandwidth = (upper - lower) / mid
    return upper, mid, lower, bandwidth


def realized_vol(close: pd.Series, n: int = 20, annualize: bool = True) -> pd.Series:
    log_ret = np.log(close / close.shift(1))
    vol = log_ret.rolling(n).std(ddof=0)
    if annualize:
        vol = vol * np.sqrt(252)
    return vol * 100  # percent


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = true_range(df)
    atr_n = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_n
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def volume_vs_avg(volume: pd.Series, n: int = 20) -> pd.Series:
    return volume / volume.rolling(n).mean()


def slope_pct(series: pd.Series, lookback: int = 5) -> pd.Series:
    """Percent change of a series over ``lookback`` bars (e.g. SMA slope)."""
    past = series.shift(lookback)
    return (series - past) / past * 100
