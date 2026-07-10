"""Grade analyst calls against realized forward returns, and summarize a track record.

A call is graded once its horizon has elapsed and the realized forward return is
known. This is the honest measure of whether the analyst adds anything — and it
feeds the promotion gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import AnalysisReport, AnalystAction, Grade

HOLD_BAND_PCT = 2.0  # a move within +/- this is "flat" — a correct HOLD


def grade(report: AnalysisReport, forward_return_pct: float) -> Grade:
    if report.action is AnalystAction.BUY:
        correct = forward_return_pct > 0
    elif report.action is AnalystAction.SELL:
        correct = forward_return_pct < 0
    else:  # HOLD
        correct = abs(forward_return_pct) <= HOLD_BAND_PCT
    return Grade(
        correct=correct,
        forward_return_pct=round(forward_return_pct, 4),
        rationale=f"{report.action.value} vs {forward_return_pct:+.2f}% forward",
    )


@dataclass
class Scorecard:
    n_calls: int = 0
    n_correct: int = 0
    n_acted: int = 0                 # buy/sell (non-hold) calls
    weighted_correct: float = 0.0    # sum of confidence on correct calls
    weighted_total: float = 0.0      # sum of confidence on all calls
    acted_return_sum: float = 0.0    # forward return captured on acted calls

    @property
    def accuracy(self) -> float:
        return self.n_correct / self.n_calls if self.n_calls else 0.0

    @property
    def confidence_weighted_accuracy(self) -> float:
        return self.weighted_correct / self.weighted_total if self.weighted_total else 0.0

    @property
    def avg_acted_return(self) -> float:
        return self.acted_return_sum / self.n_acted if self.n_acted else 0.0

    def to_dict(self) -> dict:
        return {
            "n_calls": self.n_calls,
            "n_correct": self.n_correct,
            "accuracy": round(self.accuracy, 4),
            "confidence_weighted_accuracy": round(self.confidence_weighted_accuracy, 4),
            "avg_acted_return_pct": round(self.avg_acted_return, 4),
        }


def build_scorecard(pairs: list[tuple[AnalysisReport, Grade]]) -> Scorecard:
    sc = Scorecard()
    for report, g in pairs:
        sc.n_calls += 1
        sc.weighted_total += report.confidence
        if g.correct:
            sc.n_correct += 1
            sc.weighted_correct += report.confidence
        if report.action is not AnalystAction.HOLD:
            sc.n_acted += 1
            # Return captured: positive if the directional call was right-signed.
            signed = g.forward_return_pct if report.action is AnalystAction.BUY else -g.forward_return_pct
            sc.acted_return_sum += signed
    return sc
