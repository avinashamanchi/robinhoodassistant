"""Persistence for analyst reports + graded calls, and DB-backed scorecard.

Ungraded reports (forward return not yet known) simply have no grade row; the
scorecard is built only from graded calls.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import AnalysisReportRow, GradedCallRow
from .models import AnalysisReport, AnalystAction, Grade
from .promotion import can_promote
from .scorecard import Scorecard, build_scorecard, grade


def save_report(session: Session, report: AnalysisReport) -> int:
    row = AnalysisReportRow(
        symbol=report.symbol,
        as_of=report.as_of,
        action=report.action.value,
        confidence=Decimal(str(report.confidence)),
        report_json=report.model_dump_json(),
    )
    session.add(row)
    session.flush()
    return row.id


def grade_report(session: Session, report_id: int, forward_return_pct: float) -> Grade:
    """Grade a stored report against its realized forward return (idempotent)."""
    row = session.get(AnalysisReportRow, report_id)
    if row is None:
        raise KeyError(f"no analysis report {report_id}")
    report = AnalysisReport.model_validate_json(row.report_json)
    g = grade(report, forward_return_pct)
    if row.grade is None:
        session.add(
            GradedCallRow(
                report_id=report_id,
                correct=g.correct,
                forward_return_pct=Decimal(str(g.forward_return_pct)),
            )
        )
        session.flush()
    return g


def build_scorecard_from_db(session: Session) -> Scorecard:
    rows = session.execute(
        select(AnalysisReportRow, GradedCallRow).join(
            GradedCallRow, GradedCallRow.report_id == AnalysisReportRow.id
        )
    ).all()
    pairs = [
        (
            AnalysisReport.model_validate_json(r.report_json),
            Grade(
                correct=g.correct,
                forward_return_pct=float(g.forward_return_pct),
                rationale="",
            ),
        )
        for r, g in rows
    ]
    return build_scorecard(pairs)


def promotion_status(session: Session) -> dict:
    sc = build_scorecard_from_db(session)
    eligible, reason = can_promote(sc)
    return {"eligible": eligible, "reason": reason, "scorecard": sc.to_dict()}
