"""Analyst accuracy + calibration report (C4) — no sugarcoating.

Runs the analyst in trigger-mode over a window, grades every call against realized
forward returns, and reports: hit rate, calibration (do 0.8-confidence calls win
~80%?), Brier score, per-regime breakdown, and analyst vs buy-and-hold on the same
window. States plainly whether the analyst shows edge. Promotes NOTHING.
"""

from __future__ import annotations

from typing import Optional

from ..config import BacktestConfig
from ..backtest.engine import run_backtest
from ..backtest.llm_runner import AnalystStrategy, LLMRunConfig
from ..backtest.metrics import compute_metrics
from ..strategies.buy_and_hold import BuyAndHold
from .scorecard import grade


def analyst_accuracy(
    source, symbols, analyst, run_config: LLMRunConfig, *,
    start=None, end=None, spy_symbol: str = "SPY",
) -> dict:
    graded = []  # (confidence, correct, regime, action)
    analyst_ret, bnh_ret = [], []
    calls = 0
    for sym in symbols:
        strat = AnalystStrategy(analyst, run_config)
        res = run_backtest(strat, source, sym, backtest_config=BacktestConfig(),
                           spy_symbol=spy_symbol, start=start, end=end)
        calls += strat.calls
        full = source.full(sym)
        pos = {ts.to_pydatetime(): i for i, ts in enumerate(full.index)}
        closes = full["close"].to_numpy()
        for features, report in strat.reports:
            i = pos.get(features.as_of)
            if i is None or i + run_config.horizon_bars >= len(closes) or closes[i] == 0:
                continue
            fwd = (closes[i + run_config.horizon_bars] / closes[i] - 1) * 100
            g = grade(report, float(fwd))
            regime = features.regime.value if features.regime else "n/a"
            graded.append((report.confidence, g.correct, regime, report.action.value))
        analyst_ret.append(compute_metrics(res).total_return_pct)
        bnh = compute_metrics(run_backtest(BuyAndHold(), source, sym,
                              backtest_config=BacktestConfig(), start=start, end=end))
        bnh_ret.append(bnh.total_return_pct)

    n = len(graded)
    hit = sum(c for _, c, _, _ in graded) / n if n else 0.0
    brier = sum((conf - (1.0 if c else 0.0)) ** 2 for conf, c, _, _ in graded) / n if n else None

    buckets = {"0.0-0.5": [], "0.5-0.7": [], "0.7-1.0": []}
    for conf, c, _, _ in graded:
        key = "0.0-0.5" if conf < 0.5 else ("0.5-0.7" if conf < 0.7 else "0.7-1.0")
        buckets[key].append(c)
    calibration = {k: {"n": len(v), "win_rate": round(sum(v) / len(v), 3) if v else None}
                   for k, v in buckets.items()}

    by_regime: dict = {}
    for _, c, rg, _ in graded:
        by_regime.setdefault(rg, []).append(c)
    per_regime = {k: {"n": len(v), "hit_rate": round(sum(v) / len(v), 3)} for k, v in by_regime.items()}

    a_mean = sum(analyst_ret) / len(analyst_ret) if analyst_ret else 0.0
    b_mean = sum(bnh_ret) / len(bnh_ret) if bnh_ret else 0.0
    edge = a_mean > b_mean and hit > 0.5 and n >= 50
    return {
        "llm_calls": calls, "graded_calls": n, "hit_rate": round(hit, 3),
        "brier": round(brier, 4) if brier is not None else None,
        "calibration": calibration, "per_regime": per_regime,
        "analyst_avg_return_pct": round(a_mean, 2), "buy_hold_avg_return_pct": round(b_mean, 2),
        "shows_edge": edge,
        "verdict": (
            "Analyst shows edge on this window (beats B&H, >50% hit, >=50 calls) — "
            "but promotion is still MY manual decision."
            if edge else
            "No demonstrated edge yet (need >=50 graded calls, >50% hit, and B&H "
            "outperformance). Keep grading via shadow mode."
        ),
    }
