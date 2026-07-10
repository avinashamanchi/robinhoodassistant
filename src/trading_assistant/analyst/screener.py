"""Deterministic screener — "what should I even look at".

Runs the signal library across a universe and scores each symbol by its recent
events weighted for regime fit (a BREAKOUT in TRENDING_UP scores high; an
RSI_OVERSOLD in TRENDING_DOWN scores negative — a falling knife). No LLM call, no
cost. Screening more names than we're allowed to trade is fine; the order
allowlist still gates actual orders.
"""

from __future__ import annotations

from typing import Optional

from ..assets import AssetClass
from ..signals.features import build_features
from ..signals.models import EventType, MarketFeatures, Regime

# Base score per event (trend-following positive, deterioration negative).
_EVENT_BASE = {
    EventType.GOLDEN_CROSS: 2.0,
    EventType.DEATH_CROSS: -2.0,
    EventType.BREAKOUT: 2.0,
    EventType.BREAKDOWN: -2.0,
    EventType.RSI_OVERSOLD: 1.0,
    EventType.RSI_OVERBOUGHT: -1.0,
    EventType.BB_SQUEEZE: 0.5,
    EventType.GAP_UP: 0.5,
    EventType.GAP_DOWN: -0.5,
}

# Regime multiplier per event: how much to trust the signal in this regime.
_TREND_EVENTS = {EventType.GOLDEN_CROSS, EventType.BREAKOUT, EventType.BREAKDOWN, EventType.DEATH_CROSS}
_REVERSION_EVENTS = {EventType.RSI_OVERSOLD, EventType.RSI_OVERBOUGHT}


def _regime_fit(event: EventType, regime: Optional[Regime]) -> float:
    if regime is None:
        return 1.0
    if event in _TREND_EVENTS:
        return {Regime.TRENDING_UP: 1.5, Regime.TRENDING_DOWN: 1.5,
                Regime.RANGING: 0.4, Regime.HIGH_VOLATILITY: 0.7}.get(regime, 1.0)
    if event in _REVERSION_EVENTS:
        # Mean-reversion works in ranges; oversold in a downtrend is a falling knife.
        return {Regime.RANGING: 1.5, Regime.TRENDING_DOWN: -2.0,
                Regime.TRENDING_UP: 0.5, Regime.HIGH_VOLATILITY: 0.8}.get(regime, 1.0)
    return 1.0


def score_features(features: MarketFeatures) -> tuple[float, list[str]]:
    score = 0.0
    evidence: list[str] = []
    regime = features.regime
    for e in features.events:
        base = _EVENT_BASE.get(e.type, 0.0)
        contrib = base * _regime_fit(e.type, regime)
        if contrib != 0:
            score += contrib
            evidence.append(
                f"{e.type.value} in {regime.value if regime else 'n/a'}: {contrib:+.1f}"
            )
    return round(score, 3), evidence


def rank(features_by_symbol: dict[str, MarketFeatures], top_n: int = 10) -> list[dict]:
    rows = []
    for sym, f in features_by_symbol.items():
        score, evidence = score_features(f)
        rows.append({
            "symbol": sym,
            "score": score,
            "regime": f.regime.value if f.regime else None,
            "evidence": evidence,
        })
    # Deterministic: sort by score desc, then symbol for ties.
    rows.sort(key=lambda r: (-r["score"], r["symbol"]))
    return rows[:top_n]


def screen_source(source, symbols: list[str], *, spy_symbol: Optional[str] = None,
                  top_n: int = 10) -> list[dict]:
    features_by_symbol: dict[str, MarketFeatures] = {}
    for sym in symbols:
        if sym not in source.symbols:
            continue
        spy_df = source.full(spy_symbol) if spy_symbol and spy_symbol in source.symbols else None
        features_by_symbol[sym] = build_features(
            sym, AssetClass.for_symbol(sym), source.full(sym), spy_df=spy_df
        )
    return rank(features_by_symbol, top_n)
