"""The promotion gate.

Backtest/track-record results NEVER auto-enable anything (Phase 7 guardrail #2).
Promoting the analyst toward live is a manual config change by a human — and this
gate is an ADDITIONAL requirement on top of that: at least 50 graded calls with an
accuracy bar. It returns advice; it cannot flip any switch itself.
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
        "eligible for MANUAL promotion (still requires the live double-lock)"
    )
