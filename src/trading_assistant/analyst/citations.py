"""Shared opaque-source citation validation for analyst persistence."""

from __future__ import annotations

from .models import AnalysisReport
from .untrusted import (
    UntrustedSummary,
    validate_summary_for_privileged_use,
)


def validate_source_citations(
    report: AnalysisReport,
    untrusted_summary: UntrustedSummary | None,
) -> None:
    """Validate the report/summary pair at analysis and storage boundaries."""
    if not isinstance(report, AnalysisReport):
        raise ValueError("source citations require an analysis report")
    cited = report.cited_source_refs
    if len(set(cited)) != len(cited):
        raise ValueError("source citations must be unique")
    if untrusted_summary is None:
        if cited:
            raise ValueError(
                "source citations require an untrusted summary"
            )
        return

    validate_summary_for_privileged_use(untrusted_summary)
    allowed = set(untrusted_summary.source_refs)
    if any(ref not in allowed for ref in cited):
        raise ValueError("source citation is not in the summary")
    if untrusted_summary.facts:
        relevant = {
            fact.source_ref for fact in untrusted_summary.facts
        }
        if not cited or not relevant.intersection(cited):
            raise ValueError(
                "source citation is required for summarized facts"
            )


__all__ = ["validate_source_citations"]
