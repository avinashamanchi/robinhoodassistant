"""Grading analyst calls, the scorecard, the 50-call promotion gate, persistence."""

from __future__ import annotations

from datetime import datetime, timezone

from trading_assistant.analyst.models import AnalysisReport, AnalystAction
from trading_assistant.analyst.promotion import can_promote
from trading_assistant.analyst.scorecard import build_scorecard, grade
from trading_assistant.analyst.store import (
    build_scorecard_from_db,
    grade_report,
    promotion_status,
    save_report,
)

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)


def _report(action="buy", confidence=0.6) -> AnalysisReport:
    return AnalysisReport(
        symbol="AAPL", as_of=TS, action=AnalystAction(action), confidence=confidence,
        thesis="t", cited_concepts=["Trend"], regime_note="trending up",
    )


# ── grading ─────────────────────────────────────────────────────
def test_grade_directional():
    assert grade(_report("buy"), 5.0).correct is True
    assert grade(_report("buy"), -3.0).correct is False
    assert grade(_report("sell"), -4.0).correct is True
    assert grade(_report("sell"), 2.0).correct is False


def test_grade_hold_uses_band():
    assert grade(_report("hold"), 1.0).correct is True     # within +/-2%
    assert grade(_report("hold"), 5.0).correct is False


def test_scorecard_accuracy():
    pairs = [
        (_report("buy"), grade(_report("buy"), 4.0)),   # correct
        (_report("buy"), grade(_report("buy"), -4.0)),  # wrong
        (_report("sell"), grade(_report("sell"), -3.0)),  # correct
    ]
    sc = build_scorecard(pairs)
    assert sc.n_calls == 3 and sc.n_correct == 2
    assert abs(sc.accuracy - 2 / 3) < 1e-9
    assert sc.n_acted == 3


# ── promotion gate ──────────────────────────────────────────────
def test_promotion_needs_50_calls():
    few = build_scorecard([(_report("buy"), grade(_report("buy"), 4.0))] * 10)
    ok, reason = can_promote(few)
    assert ok is False and "10/50" in reason


def test_promotion_eligible_at_threshold():
    fifty = build_scorecard([(_report("buy"), grade(_report("buy"), 4.0))] * 50)
    ok, reason = can_promote(fifty)
    assert ok is True and "eligible for MANUAL promotion" in reason


# ── persistence ─────────────────────────────────────────────────
def test_store_grade_and_promotion_status(session_factory):
    with session_factory() as s:
        for i in range(50):
            rid = save_report(s, _report("buy"))
            grade_report(s, rid, forward_return_pct=4.0)  # all correct
        s.commit()

    with session_factory() as s:
        sc = build_scorecard_from_db(s)
        assert sc.n_calls == 50 and sc.accuracy == 1.0
        status = promotion_status(s)
        assert status["eligible"] is True


def test_grade_report_is_idempotent(session_factory):
    with session_factory() as s:
        rid = save_report(s, _report("buy"))
        grade_report(s, rid, 4.0)
        grade_report(s, rid, 4.0)  # second grade must not double-count
        s.commit()
    with session_factory() as s:
        assert build_scorecard_from_db(s).n_calls == 1
