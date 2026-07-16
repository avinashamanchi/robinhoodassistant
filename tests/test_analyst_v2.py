"""Analyst v2: RANGING suppression, confidence neutralization, version-tagged scorecard."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from trading_assistant.analyst.analyst import Analyst
from trading_assistant.analyst.models import AnalysisReport, AnalystAction, PlanAction
from trading_assistant.analyst.store import (
    build_scorecard_from_db,
    grade_report,
    save_report,
)
from trading_assistant.assets import AssetClass
from trading_assistant.backtest.llm_runner import AnalystStrategy
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)

_REPORT_INPUT = {"action": "buy", "confidence": 0.9, "thesis": "t",
                 "cited_concepts": ["Trend"], "regime_note": "r"}
_PLAN_INPUT = {
    "action": "buy", "confidence": 0.9, "thesis": "t", "cited_concepts": ["Trend"],
    "regime_note": "r",
    "scenarios": [{"name": "bear", "price_target": 90, "horizon_days": 30, "probability": 0.2},
                  {"name": "base", "price_target": 110, "horizon_days": 30, "probability": 0.5},
                  {"name": "bull", "price_target": 130, "horizon_days": 30, "probability": 0.3}],
    "invalidation": {"price_level": 88, "rationale": "r"},
    "entry_plan": {"type": "single", "tranches": [{"price_level": 99, "fraction": 1.0}]},
    "exit_plan": {"targets": [{"price_level": 120, "fraction_to_sell": 1.0}], "stop": 92},
}


def _backend(tool, inp):
    block = SimpleNamespace(type="tool_use", name=tool, id="t", input=dict(inp))

    class B:
        def create(self, *, system, messages, tools, tool_choice=None):
            return SimpleNamespace(content=[block])

    return B()


def _feat(regime):
    return MarketFeatures(symbol="AAPL", asset_class=AssetClass.EQUITY, as_of=TS,
                          last_close=100.0, regime=regime)


# ── RANGING suppression ─────────────────────────────────────────
def test_report_forced_hold_in_ranging():
    a = Analyst(_backend("submit_analysis", _REPORT_INPUT), suppress_ranging=True)
    assert a.analyze(_feat(Regime.RANGING)).action is AnalystAction.HOLD


def test_report_untouched_outside_ranging():
    a = Analyst(_backend("submit_analysis", _REPORT_INPUT), suppress_ranging=True)
    assert a.analyze(_feat(Regime.TRENDING_UP)).action is AnalystAction.BUY


def test_plan_forced_no_trade_in_ranging():
    a = Analyst(_backend("submit_plan", _PLAN_INPUT), suppress_ranging=True)
    assert a.analyze_plan(_feat(Regime.RANGING)).action is PlanAction.NO_TRADE


def test_suppression_off_leaves_buy():
    a = Analyst(_backend("submit_analysis", _REPORT_INPUT), suppress_ranging=False)
    assert a.analyze(_feat(Regime.RANGING)).action is AnalystAction.BUY


# ── confidence neutralized ──────────────────────────────────────
def test_signal_size_ignores_confidence():
    report = AnalysisReport(symbol="AAPL", as_of=TS, action=AnalystAction.BUY,
                            confidence=0.95, thesis="t", cited_concepts=["Trend"], regime_note="r")
    assert AnalystStrategy._to_signal(report).size_hint == 1.0    # not 0.95


# ── version-tagged scorecard ────────────────────────────────────
def test_scorecard_filters_by_version(session_factory):
    r = AnalysisReport(symbol="AAPL", as_of=TS, action=AnalystAction.BUY, confidence=0.6,
                       thesis="t", cited_concepts=["Trend"], regime_note="r")
    with session_factory() as s:
        for v in ("v1", "v1", "v2"):
            rid = save_report(s, r, version=v)
            grade_report(s, rid, 4.0)
        s.commit()
    with session_factory() as s:
        assert build_scorecard_from_db(s, version="v2").n_calls == 1   # v1 grades excluded
        assert build_scorecard_from_db(s, version="v1").n_calls == 2
        assert build_scorecard_from_db(s).n_calls == 3                 # unfiltered = all
