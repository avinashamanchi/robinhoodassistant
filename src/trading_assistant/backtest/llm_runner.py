"""LLM-in-the-loop backtesting (Phase 7 Part C10, unblocked by Phase 6).

Running the analyst on every bar of a multi-year backtest would cost a fortune, so:

* **trigger_mode** — the analyst is invoked ONLY on bars where signal events fire
  (golden cross, breakout, …); deterministic HOLD otherwise.
* **response cache** — keyed on stable decision identity plus features hash;
  identical features in the same run never pay twice.
* **hard budget** — ``max_llm_calls`` aborts the run if exceeded; a pre-run
  ``estimate_llm_calls`` prints the expected count/cost for confirmation.
* **cheap model + spot-check** — run on a cheap model, optionally re-run every Nth
  decision on the full model and record disagreements.

Decisions still use only data <= t (the analyst reads bounded MarketFeatures).
Grading uses realized forward returns after the fact — that is evaluation, not
lookahead, and feeds the scorecard so you can finally ask: does the analyst beat
buy-and-hold on the holdout?
"""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from ..analyst.analyst import Analyst
from ..analyst.models import AnalysisReport, AnalystAction
from ..analyst.scorecard import Scorecard, build_scorecard, grade
from ..config import BacktestConfig
from ..identity import canonical_symbol, canonical_utc_timestamp
from ..signals.models import EventType, MarketFeatures
from ..strategies.base import Signal, SignalAction, Strategy, hold
from .data import DataSource
from .engine import BacktestResult, run_backtest
from .metrics import Metrics, compute_metrics


class BudgetExceeded(Exception):
    """Raised when a run exceeds its max_llm_calls budget — aborts the run."""


@dataclass
class LLMRunConfig:
    trigger_events: Optional[set[EventType]] = None  # None => any event triggers
    max_llm_calls: int = 200
    cost_per_call_usd: float = 0.01
    horizon_bars: int = 5            # forward window for grading
    cheap_model: str = "claude-haiku-4-5"
    full_model: str = "claude-sonnet-4-6"
    spot_check_every: int = 0        # 0 => off


def _triggered(features: MarketFeatures, config: LLMRunConfig) -> bool:
    if not features.events:
        return False
    if config.trigger_events is None:
        return True
    return any(e.type in config.trigger_events for e in features.events)


def _features_hash(features: MarketFeatures) -> str:
    payload = features.model_dump(
        mode="json",
        exclude={"recent_bars"},
    )
    try:
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except ValueError:
        raise ValueError(
            "MarketFeatures prompt payload must contain finite JSON"
        ) from None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalized_text(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be non-empty")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValueError(f"{field} must be non-empty")
    return normalized


def _require_run_id(run_id: str) -> str:
    return _normalized_text("run_id", run_id)


def _normalized_symbol(symbol: str) -> str:
    return canonical_symbol(symbol)


def _decision_request_id(run_id: str, features: MarketFeatures) -> str:
    material = json.dumps(
        {
            "run_id": _require_run_id(run_id),
            "symbol": _normalized_symbol(features.symbol),
            "timestamp": canonical_utc_timestamp(
                features.as_of,
                field="features.as_of",
            ),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = base64.urlsafe_b64encode(
        hashlib.sha256(material.encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")
    return f"backtest:{digest}"


class ResponseCache:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], AnalysisReport] = {}

    @staticmethod
    def _key(
        decision_id: str,
        features: MarketFeatures,
    ) -> tuple[str, str]:
        return (
            _normalized_text("decision_id", decision_id),
            _features_hash(features),
        )

    def get(
        self,
        decision_id: str,
        features: MarketFeatures,
    ) -> Optional[AnalysisReport]:
        return self._store.get(self._key(decision_id, features))

    def put(
        self,
        decision_id: str,
        features: MarketFeatures,
        report: AnalysisReport,
    ) -> None:
        self._store[self._key(decision_id, features)] = report


class AnalystStrategy(Strategy):
    """Adapts an Analyst into a Strategy the replay engine can drive."""

    name = "llm_analyst"

    def __init__(
        self,
        analyst: Analyst,
        config: LLMRunConfig,
        cache: Optional[ResponseCache] = None,
        full_analyst: Optional[Analyst] = None,
        *,
        run_id: str,
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        self.analyst = analyst
        self.full_analyst = full_analyst
        self.config = config
        self.run_id = _require_run_id(run_id)
        self.cancel_check = cancel_check
        self.cache = cache or ResponseCache()
        self.calls = 0
        self.spot_checks = 0
        self.spot_check_disagreements = 0
        self.reports: list[tuple[MarketFeatures, AnalysisReport]] = []

    def on_bar(self, features: MarketFeatures) -> Signal:
        if not _triggered(features, self.config):
            return hold("no trigger event")

        request_id = _decision_request_id(self.run_id, features)
        cached = self.cache.get(request_id, features)
        if cached is not None:
            return self._to_signal(cached)

        if self.calls >= self.config.max_llm_calls:
            raise BudgetExceeded(
                f"exceeded max_llm_calls={self.config.max_llm_calls}"
            )
        if self.cancel_check is not None:
            self.cancel_check()
        # Count the ATTEMPT against the budget before making it — a failed call
        # still hits the provider, so the cap must be fail-closed (security).
        self.calls += 1
        try:
            report = self.analyst.analyze(
                features,
                request_id=request_id,
            )
        except Exception:  # a malformed LLM response must not abort the whole run
            return hold("analyst error; skipped")
        self.cache.put(request_id, features, report)
        self.reports.append((features, report))
        self._maybe_spot_check(features, report, request_id)
        return self._to_signal(report)

    def _maybe_spot_check(
        self,
        features: MarketFeatures,
        cheap: AnalysisReport,
        request_id: str,
    ) -> None:
        if (
            self.full_analyst is None
            or self.config.spot_check_every <= 0
            or self.calls % self.config.spot_check_every != 0
        ):
            return
        if self.cancel_check is not None:
            self.cancel_check()
        self.spot_checks += 1
        full = self.full_analyst.analyze(
            features,
            request_id=request_id,
        )
        if full.action is not cheap.action:
            self.spot_check_disagreements += 1

    @staticmethod
    def _to_signal(report: AnalysisReport) -> Signal:
        action = {
            AnalystAction.BUY: SignalAction.BUY,
            AnalystAction.SELL: SignalAction.SELL,
            AnalystAction.HOLD: SignalAction.HOLD,
        }[report.action]
        # Sizing is deterministic elsewhere; here confidence just scales the signal.
        # v2: confidence is NEUTRALIZED — emitted and graded, but it never sizes or
        # weights anything until calibration proves out. Fixed size.
        return Signal(action, size_hint=1.0, reason=report.thesis[:80])


@dataclass
class LLMBacktestResult:
    symbol: str
    metrics: Metrics
    scorecard: Scorecard
    llm_calls: int
    estimated_cost_usd: float
    graded_calls: int
    spot_check_disagreements: int
    engine_result: BacktestResult = field(repr=False, default=None)


# ── pre-run cost estimate ───────────────────────────────────────
class _TriggerCounter(Strategy):
    name = "trigger_counter"

    def __init__(self, config: LLMRunConfig) -> None:
        self.config = config
        self.count = 0

    def on_bar(self, features: MarketFeatures) -> Signal:
        if _triggered(features, self.config):
            self.count += 1
        return hold()


def estimate_llm_calls(
    source: DataSource,
    symbol: str,
    run_config: LLMRunConfig,
    *,
    backtest_config: Optional[BacktestConfig] = None,
    spy_symbol: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> dict:
    """Count trigger bars (no LLM) and price the run for confirmation."""
    counter = _TriggerCounter(run_config)
    run_backtest(
        counter,
        source,
        symbol,
        backtest_config=backtest_config or BacktestConfig(),
        spy_symbol=spy_symbol,
        start=start,
        end=end,
    )
    return {
        "estimated_calls": counter.count,
        "within_budget": counter.count <= run_config.max_llm_calls,
        "estimated_cost_usd": round(counter.count * run_config.cost_per_call_usd, 4),
    }


# ── grading the run's calls ─────────────────────────────────────
def grade_pairs(
    source: DataSource,
    symbol: str,
    reports: list[tuple[MarketFeatures, AnalysisReport]],
    horizon_bars: int,
) -> list:
    """Return (report, grade) pairs — used for scorecard + calibration."""
    full = source.full(symbol)
    pos = {ts.to_pydatetime(): i for i, ts in enumerate(full.index)}
    closes = full["close"].to_numpy()
    pairs = []
    for features, report in reports:
        i = pos.get(features.as_of)
        if i is None or i + horizon_bars >= len(closes) or closes[i] == 0:
            continue
        fwd = (closes[i + horizon_bars] / closes[i] - 1) * 100
        pairs.append((report, grade(report, float(fwd))))
    return pairs


def _grade_reports(source, symbol, reports, horizon_bars) -> Scorecard:
    return build_scorecard(grade_pairs(source, symbol, reports, horizon_bars))


def run_llm_backtest(
    analyst: Analyst,
    source: DataSource,
    symbol: str,
    *,
    run_id: str,
    run_config: LLMRunConfig,
    backtest_config: Optional[BacktestConfig] = None,
    spy_symbol: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    full_analyst: Optional[Analyst] = None,
    cancel_check: Callable[[], None] | None = None,
) -> LLMBacktestResult:
    strategy = AnalystStrategy(
        analyst,
        run_config,
        full_analyst=full_analyst,
        run_id=run_id,
        cancel_check=cancel_check,
    )
    engine_result = run_backtest(
        strategy,
        source,
        symbol,
        backtest_config=backtest_config or BacktestConfig(),
        spy_symbol=spy_symbol,
        start=start,
        end=end,
        cancel_check=cancel_check,
    )
    scorecard = _grade_reports(source, symbol, strategy.reports, run_config.horizon_bars)
    return LLMBacktestResult(
        symbol=symbol.upper(),
        metrics=compute_metrics(engine_result),
        scorecard=scorecard,
        llm_calls=strategy.calls,
        estimated_cost_usd=round(strategy.calls * run_config.cost_per_call_usd, 4),
        graded_calls=scorecard.n_calls,
        spot_check_disagreements=strategy.spot_check_disagreements,
        engine_result=engine_result,
    )
