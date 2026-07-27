"""LLM-in-the-loop backtesting: trigger-mode, budget, cache, grading, spot-check."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading_assistant.analyst.analyst import Analyst
from trading_assistant.analyst.models import AnalysisReport, AnalystAction
from trading_assistant.assets import AssetClass
from trading_assistant.backtest.data import DataSource
from trading_assistant.backtest.llm_runner import (
    AnalystStrategy,
    BudgetExceeded,
    LLMRunConfig,
    ResponseCache,
    estimate_llm_calls,
    run_llm_backtest,
)
from trading_assistant.backtest.synthetic import make_bars
from trading_assistant.signals.models import (
    EventTag,
    EventType,
    MarketFeatures,
    Regime,
)

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
        def create(
            self,
            *,
            system,
            messages,
            tools,
            tool_choice=None,
            request_id,
        ):
            return SimpleNamespace(content=[block])

    return Analyst(B(), max_attempts=2)


def test_trigger_mode_limits_calls():
    source = DataSource({"AAPL": make_bars(300, seed=7)})
    cfg = LLMRunConfig()
    est = estimate_llm_calls(source, "AAPL", cfg)
    assert est["estimated_calls"] >= 1
    res = run_llm_backtest(
        _analyst("buy"),
        source,
        "AAPL",
        run_id="trigger-mode",
        run_config=cfg,
    )
    assert res.llm_calls == est["estimated_calls"]     # estimate matches the run
    assert res.llm_calls < 300                         # far fewer than one/bar


def test_budget_aborts_run():
    source = DataSource({"AAPL": make_bars(300, seed=7)})
    est = estimate_llm_calls(source, "AAPL", LLMRunConfig())
    if est["estimated_calls"] <= 1:
        pytest.skip("not enough triggers to exceed a budget of 1")
    with pytest.raises(BudgetExceeded):
        run_llm_backtest(
            _analyst(),
            source,
            "AAPL",
            run_id="budget-abort",
            run_config=LLMRunConfig(max_llm_calls=1),
        )


def test_grading_feeds_scorecard():
    source = DataSource({"AAPL": make_bars(300, seed=7)})
    res = run_llm_backtest(
        _analyst("buy"),
        source,
        "AAPL",
        run_id="grading-scorecard",
        run_config=LLMRunConfig(),
    )
    assert res.scorecard.n_calls >= 1
    assert res.graded_calls == res.scorecard.n_calls


def test_response_cache():
    cache = ResponseCache()
    feat = MarketFeatures(
        symbol="AAPL", asset_class=AssetClass.EQUITY, as_of=TS,
        last_close=100.0, rsi_14=45.0, regime=Regime.RANGING,
    )
    assert cache.get("backtest:decision-one", feat) is None
    report = AnalysisReport(
        symbol="AAPL", as_of=TS, action=AnalystAction.HOLD, confidence=0.5,
        thesis="t", cited_concepts=["Trend"], regime_note="r",
    )
    cache.put("backtest:decision-one", feat, report)
    assert cache.get("backtest:decision-one", feat) is report
    assert cache.get("backtest:decision-two", feat) is None


def test_spot_check_records_disagreement():
    source = DataSource({"AAPL": make_bars(250, seed=3)})
    cfg = LLMRunConfig(spot_check_every=1)
    res = run_llm_backtest(
        _analyst("buy"),
        source,
        "AAPL",
        run_id="spot-check",
        run_config=cfg,
        full_analyst=_analyst("sell"),
    )
    if res.llm_calls >= 1:
        # cheap says buy, full says sell on every checked call -> all disagree.
        assert res.spot_check_disagreements >= 1


def test_estimate_flags_over_budget():
    source = DataSource({"AAPL": make_bars(200, seed=3)})
    est = estimate_llm_calls(source, "AAPL", LLMRunConfig(max_llm_calls=0))
    assert est["within_budget"] is (est["estimated_calls"] == 0)


def test_strategy_reuses_deterministic_request_id_across_cache_and_replay():
    report = AnalysisReport(
        symbol="AAPL",
        as_of=TS,
        action=AnalystAction.BUY,
        confidence=0.6,
        thesis="thesis",
        cited_concepts=["Trend"],
        regime_note="regime note",
    )

    class RecordingAnalyst:
        def __init__(self):
            self.request_ids = []

        def analyze(self, features, *, request_id):
            self.request_ids.append(request_id)
            return report

    features = MarketFeatures(
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        as_of=TS,
        last_close=100.0,
        regime=Regime.TRENDING_UP,
        events=[EventTag(type=EventType.BREAKOUT, ts=TS)],
    )
    cheap = RecordingAnalyst()
    full = RecordingAnalyst()
    shared_cache = ResponseCache()
    first = AnalystStrategy(
        cheap,
        LLMRunConfig(spot_check_every=1),
        cache=shared_cache,
        run_id="holdout-2022",
        full_analyst=full,
    )

    first.on_bar(features)
    first.on_bar(features)

    canonical = (
        '{"run_id":"holdout-2022","symbol":"AAPL",'
        '"timestamp":"2016-06-01T00:00:00.000000Z"}'
    )
    digest = (
        base64.urlsafe_b64encode(
            hashlib.sha256(canonical.encode("utf-8")).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    expected = f"backtest:{digest}"
    assert cheap.request_ids == [expected]
    assert full.request_ids == [expected]
    assert len(expected) == 52

    replay = RecordingAnalyst()
    AnalystStrategy(
        replay,
        LLMRunConfig(),
        run_id="holdout-2022",
    ).on_bar(features)
    assert replay.request_ids == [expected]

    same_run_cached = RecordingAnalyst()
    AnalystStrategy(
        same_run_cached,
        LLMRunConfig(),
        cache=shared_cache,
        run_id="holdout-2022",
    ).on_bar(features)
    assert same_run_cached.request_ids == []

    different_run = RecordingAnalyst()
    AnalystStrategy(
        different_run,
        LLMRunConfig(),
        cache=shared_cache,
        run_id="holdout-2023",
    ).on_bar(features)
    assert len(different_run.request_ids) == 1
    assert different_run.request_ids[0] != expected


def test_decision_request_id_normalizes_symbol_run_and_equivalent_offset():
    report = AnalysisReport(
        symbol="AAPL",
        as_of=TS,
        action=AnalystAction.HOLD,
        confidence=0.5,
        thesis="hold",
        cited_concepts=["Trend"],
        regime_note="range",
    )

    class RecordingAnalyst:
        def __init__(self):
            self.request_ids = []

        def analyze(self, features, *, request_id):
            self.request_ids.append(request_id)
            return report

    offset_features = MarketFeatures(
        symbol=" aapl ",
        asset_class=AssetClass.EQUITY,
        as_of=datetime(
            2016,
            5,
            31,
            17,
            tzinfo=timezone(timedelta(hours=-7)),
        ),
        events=[EventTag(type=EventType.BREAKOUT, ts=TS)],
    )
    analyst = RecordingAnalyst()
    AnalystStrategy(
        analyst,
        LLMRunConfig(),
        run_id=" holdout-2022 ",
    ).on_bar(offset_features)

    canonical = (
        '{"run_id":"holdout-2022","symbol":"AAPL",'
        '"timestamp":"2016-06-01T00:00:00.000000Z"}'
    )
    expected = "backtest:" + (
        base64.urlsafe_b64encode(
            hashlib.sha256(canonical.encode("utf-8")).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    assert analyst.request_ids == [expected]


@pytest.mark.parametrize("symbol", ["", " ", "\t"])
def test_decision_rejects_blank_symbol_before_provider(symbol):
    class RecordingAnalyst:
        def __init__(self):
            self.request_ids = []

        def analyze(self, features, *, request_id):
            self.request_ids.append(request_id)
            raise AssertionError("provider must not be called")

    features = MarketFeatures(
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        as_of=TS,
        events=[EventTag(type=EventType.BREAKOUT, ts=TS)],
    )
    analyst = RecordingAnalyst()
    strategy = AnalystStrategy(
        analyst,
        LLMRunConfig(),
        run_id="symbol-validation",
    )

    with pytest.raises(ValueError, match="symbol"):
        strategy.on_bar(features)

    assert analyst.request_ids == []


def test_decision_rejects_naive_timestamp_before_provider():
    class RecordingAnalyst:
        def __init__(self):
            self.request_ids = []

        def analyze(self, features, *, request_id):
            self.request_ids.append(request_id)
            raise AssertionError("provider must not be called")

    features = MarketFeatures(
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        as_of=TS.replace(tzinfo=None),
        events=[EventTag(type=EventType.BREAKOUT, ts=TS)],
    )
    analyst = RecordingAnalyst()
    strategy = AnalystStrategy(
        analyst,
        LLMRunConfig(),
        run_id="timestamp-validation",
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        strategy.on_bar(features)

    assert analyst.request_ids == []


def test_strategy_bounds_request_id_and_rejects_blank_run_before_replay():
    class RecordingAnalyst:
        def __init__(self):
            self.request_ids = []

        def analyze(self, features, *, request_id):
            self.request_ids.append(request_id)
            return AnalysisReport(
                symbol=features.symbol,
                as_of=features.as_of,
                action=AnalystAction.HOLD,
                confidence=0.5,
                thesis="hold",
                cited_concepts=["Trend"],
                regime_note="range",
            )

    features = MarketFeatures(
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        as_of=TS,
        events=[EventTag(type=EventType.BREAKOUT, ts=TS)],
    )
    analyst = RecordingAnalyst()
    strategy = AnalystStrategy(
        analyst,
        LLMRunConfig(),
        run_id="r" * 500,
    )
    strategy.on_bar(features)

    assert len(analyst.request_ids) == 1
    assert analyst.request_ids[0].startswith("backtest:")
    assert len(analyst.request_ids[0]) <= 64

    with pytest.raises(ValueError, match="run_id"):
        AnalystStrategy(analyst, LLMRunConfig(), run_id=" ")


def test_run_llm_backtest_rejects_blank_run_id_before_replay(monkeypatch):
    import trading_assistant.backtest.llm_runner as llm_runner

    replay_calls = []
    monkeypatch.setattr(
        llm_runner,
        "run_backtest",
        lambda *_args, **_kwargs: replay_calls.append("replayed"),
    )

    with pytest.raises(ValueError, match="run_id"):
        run_llm_backtest(
            _analyst(),
            object(),
            "AAPL",
            run_id=" ",
            run_config=LLMRunConfig(),
        )

    assert replay_calls == []
