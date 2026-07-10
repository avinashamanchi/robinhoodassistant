"""Typed signal outputs. All fields that need N bars of history are Optional
(None until enough data). MarketFeatures is the single bundle strategies consume
and the (deferred Phase 6) analyst interprets.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from ..assets import AssetClass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Regime(str, enum.Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"


class EventType(str, enum.Enum):
    GOLDEN_CROSS = "golden_cross"
    DEATH_CROSS = "death_cross"
    BREAKOUT = "breakout"
    BREAKDOWN = "breakdown"
    RSI_OVERSOLD = "rsi_oversold"
    RSI_OVERBOUGHT = "rsi_overbought"
    BB_SQUEEZE = "bb_squeeze"
    GAP_UP = "gap_up"
    GAP_DOWN = "gap_down"


class Bar(_Model):
    ts: datetime            # UTC, bar close time
    open: float
    high: float
    low: float
    close: float
    volume: float


class EventTag(_Model):
    type: EventType
    ts: datetime
    meta: dict = {}


class MacdValue(_Model):
    line: Optional[float] = None
    signal: Optional[float] = None
    hist: Optional[float] = None


class BollingerValue(_Model):
    upper: Optional[float] = None
    mid: Optional[float] = None
    lower: Optional[float] = None
    bandwidth: Optional[float] = None


class MarketContext(_Model):
    spy_regime: Optional[Regime] = None
    spy_realized_vol_20_pct: Optional[float] = None  # percentile rank 0-100


class RelativeStrength(_Model):
    rs_20d: Optional[float] = None   # asset return - SPY return, 20d
    rs_60d: Optional[float] = None


class MarketFeatures(_Model):
    symbol: str
    asset_class: AssetClass
    as_of: datetime                  # == t; computed only from bars with ts <= t

    recent_bars: list[Bar] = []
    last_close: Optional[float] = None
    prev_close: Optional[float] = None

    # trend
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    macd: MacdValue = MacdValue()
    adx_14: Optional[float] = None
    sma50_slope: Optional[float] = None
    price_vs_sma200_pct: Optional[float] = None

    # momentum
    rsi_14: Optional[float] = None
    roc_10: Optional[float] = None

    # volatility
    atr_14: Optional[float] = None
    bollinger: BollingerValue = BollingerValue()
    realized_vol_20: Optional[float] = None

    # volume  (OBV cut per review — redundant with volume_vs_avg20)
    volume: Optional[float] = None
    volume_vs_avg20: Optional[float] = None

    # structure
    support_levels: list[float] = []
    resistance_levels: list[float] = []
    dist_to_52w_high_pct: Optional[float] = None
    dist_to_52w_low_pct: Optional[float] = None
    gap_pct: Optional[float] = None
    consecutive_up_days: int = 0
    consecutive_down_days: int = 0

    # events + regime
    events: list[EventTag] = []
    regime: Optional[Regime] = None

    # added per review
    days_to_next_earnings: Optional[int] = None       # None for crypto
    market_context: MarketContext = MarketContext()
    relative_strength_vs_spy: RelativeStrength = RelativeStrength()
