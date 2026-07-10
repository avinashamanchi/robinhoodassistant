"""Deterministic screener scoring + ranking."""

from __future__ import annotations

from datetime import datetime, timezone

from trading_assistant.analyst import screener
from trading_assistant.assets import AssetClass
from trading_assistant.backtest.data import DataSource
from trading_assistant.backtest.synthetic import make_bars
from trading_assistant.signals.models import EventTag, EventType, MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)


def _feat(events, regime):
    return MarketFeatures(symbol="X", asset_class=AssetClass.EQUITY, as_of=TS,
                          regime=regime, events=[EventTag(type=e, ts=TS) for e in events])


def test_breakout_scores_high_in_uptrend():
    up = screener.score_features(_feat([EventType.BREAKOUT], Regime.TRENDING_UP))[0]
    rng = screener.score_features(_feat([EventType.BREAKOUT], Regime.RANGING))[0]
    assert up > rng > 0


def test_oversold_in_downtrend_scores_negative():
    score, _ = screener.score_features(_feat([EventType.RSI_OVERSOLD], Regime.TRENDING_DOWN))
    assert score < 0                       # falling knife, penalized


def test_rank_is_deterministic():
    feats = {
        "AAA": _feat([EventType.BREAKOUT], Regime.TRENDING_UP),
        "BBB": _feat([EventType.RSI_OVERSOLD], Regime.TRENDING_DOWN),
        "CCC": _feat([EventType.GOLDEN_CROSS], Regime.TRENDING_UP),
    }
    r1 = screener.rank(feats, top_n=3)
    r2 = screener.rank(feats, top_n=3)
    assert [x["symbol"] for x in r1] == [x["symbol"] for x in r2]
    assert r1[0]["score"] >= r1[-1]["score"]           # sorted desc
    assert r1[-1]["symbol"] == "BBB"                   # the negative one is last


def test_screen_source_runs_over_universe():
    source = DataSource({s: make_bars(300, seed=i) for i, s in enumerate(["AAA", "BBB", "SPY"])})
    rows = screener.screen_source(source, ["AAA", "BBB"], spy_symbol="SPY", top_n=5)
    assert {r["symbol"] for r in rows} == {"AAA", "BBB"}
