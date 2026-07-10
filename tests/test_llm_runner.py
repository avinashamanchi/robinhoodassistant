"""LLM-in-the-loop backtesting: trigger-mode, budget, cache, grading, spot-check."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trading_assistant.analyst.analyst import Analyst
from trading_assistant.analyst.models import AnalysisReport, AnalystAction
from trading_assistant.assets import AssetClass
from trading_assistant.backtest.data import DataSource
from trading_assistant.backtest.llm_runner import (
    BudgetExceeded,
    LLMRunConfig,
    ResponseCache,
    estimate_llm_calls,
    run_llm_backtest,
)
from trading_assistant.backtest.synthetic import make_bars
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2016, 6, 1, tzinfo=timezone.utc)


def _analyst(action="buy") -> Analyst:
    inp = {
        "action": action,
        "confidence": 0.6,
        "thesis": "thesis",
        "cited_concepts": ["Trend"],
        "regime_note": "regime note",
    }
    block = SimpleNamespace(type="tool_use", name="submit_analysis", id="t", input=inp)

    class B:
        def create(self, *, system, messages, tools):
            return SimpleNamespace(content=[block])

    return Analyst(B())


def test_trigger_mode_limits_calls():
    source = DataSource({"AAPL": make_bars(300, seed=7)})
    cfg = LLMRunConfig()
    est = estimate_llm_calls(source, "AAPL", cfg)
    assert est["estimated_calls"] >= 1
    res = run_llm_backtest(_analyst("buy"), source, "AAPL", run_config=cfg)
    assert res.llm_calls == est["estimated_calls"]     # estimate matches the run
    assert res.llm_calls < 300                         # far fewer than one/bar


def test_budget_aborts_run():
    source = DataSource({"AAPL": make_bars(300, seed=7)})
    est = estimate_llm_calls(source, "AAPL", LLMRunConfig())
    if est["estimated_calls"] <= 1:
        pytest.skip("not enough triggers to exceed a budget of 1")
    with pytest.raises(BudgetExceeded):
        run_llm_backtest(
            _analyst(), source, "AAPL", run_config=LLMRunConfig(max_llm_calls=1)
        )


def test_grading_feeds_scorecard():
    source = DataSource({"AAPL": make_bars(300, seed=7)})
    res = run_llm_backtest(_analyst("buy"), source, "AAPL", run_config=LLMRunConfig())
    assert res.scorecard.n_calls >= 1
    assert res.graded_calls == res.scorecard.n_calls


def test_response_cache():
    cache = ResponseCache()
    feat = MarketFeatures(
        symbol="AAPL", asset_class=AssetClass.EQUITY, as_of=TS,
        last_close=100.0, rsi_14=45.0, regime=Regime.RANGING,
    )
    assert cache.get(feat) is None
    report = AnalysisReport(
        symbol="AAPL", as_of=TS, action=AnalystAction.HOLD, confidence=0.5,
        thesis="t", cited_concepts=["Trend"], regime_note="r",
    )
    cache.put(feat, report)
    assert cache.get(feat) is report


def test_spot_check_records_disagreement():
    source = DataSource({"AAPL": make_bars(250, seed=3)})
    cfg = LLMRunConfig(spot_check_every=1)
    res = run_llm_backtest(
        _analyst("buy"), source, "AAPL", run_config=cfg, full_analyst=_analyst("sell")
    )
    if res.llm_calls >= 1:
        # cheap says buy, full says sell on every checked call -> all disagree.
        assert res.spot_check_disagreements >= 1


def test_estimate_flags_over_budget():
    source = DataSource({"AAPL": make_bars(200, seed=3)})
    est = estimate_llm_calls(source, "AAPL", LLMRunConfig(max_llm_calls=0))
    assert est["within_budget"] is (est["estimated_calls"] == 0)
