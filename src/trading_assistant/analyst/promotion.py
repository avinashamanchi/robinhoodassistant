"""An advisory analyst track-record threshold.

Backtest and track-record results never enable execution capability. This
threshold is research advice only; the current release rejects live trading
regardless of its result.
"""

from __future__ import annotations

from .scorecard import Scorecard

MIN_GRADED_CALLS = 50
MIN_ACCURACY = 0.5


def can_promote(scorecard: Scorecard) -> tuple[bool, str]:
    if scorecard.n_calls < MIN_GRADED_CALLS:
        return False, (
            f"only {scorecard.n_calls}/{MIN_GRADED_CALLS} graded calls — keep grading"
        )
    if scorecard.accuracy < MIN_ACCURACY:
        return False, (
            f"accuracy {scorecard.accuracy:.1%} below {MIN_ACCURACY:.0%} bar"
        )
    return True, (
        f"{scorecard.n_calls} graded calls at {scorecard.accuracy:.1%} accuracy — "
        "eligible for MANUAL analyst review; live trading remains unavailable"
    )
