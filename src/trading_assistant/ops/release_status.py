"""Pure, fail-closed release status classification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SoftwareStatus(StrEnum):
    """Result of deterministic software verification."""

    VERIFIED = "verified"
    BLOCKED = "blocked"


class OperationalStatus(StrEnum):
    """Result of fresh operational evidence."""

    READY = "ready"
    BLOCKED = "blocked"


class PreflightEvidence(StrEnum):
    """The bounded operational evidence classes accepted by the evaluator."""

    READY = "ready"
    BREAKER_TRIPPED = "breaker_tripped"
    BROKER_TRUTH_UNKNOWN = "broker_truth_unknown"
    DAEMON_STALE = "daemon_stale"
    ENCRYPTION_MIXED = "encryption_mixed"


_PREFLIGHT_DETAIL = {
    PreflightEvidence.READY: "PREFLIGHT_READY",
    PreflightEvidence.BREAKER_TRIPPED: "BREAKER_TRIPPED",
    PreflightEvidence.BROKER_TRUTH_UNKNOWN: "BROKER_TRUTH_UNKNOWN",
    PreflightEvidence.DAEMON_STALE: "DAEMON_STALE",
    PreflightEvidence.ENCRYPTION_MIXED: "ENCRYPTION_MIXED",
}


@dataclass(frozen=True, slots=True)
class ReleaseStatus:
    """Two independent status dimensions with a non-execution boundary."""

    software: SoftwareStatus
    operational: OperationalStatus
    detail_codes: tuple[str, ...]

    @property
    def summary(self) -> str:
        return (
            f"software {self.software.value}; "
            f"operational {self.operational.value}; "
            "Alpaca paper only; execution not authorized"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "software_status": self.software.value,
            "operational_status": self.operational.value,
            "detail_codes": list(self.detail_codes),
            "paper_only": True,
            "execution_authorized": False,
        }


def evaluate_release_status(
    *,
    tests_passed: bool,
    preflight: PreflightEvidence,
) -> ReleaseStatus:
    """Classify software and operations without granting trading authority."""
    if type(tests_passed) is not bool:
        raise TypeError("tests_passed must be a boolean")
    if not isinstance(preflight, PreflightEvidence):
        raise TypeError("preflight must be classified evidence")

    software = (
        SoftwareStatus.VERIFIED
        if tests_passed and preflight is not PreflightEvidence.ENCRYPTION_MIXED
        else SoftwareStatus.BLOCKED
    )
    operational = (
        OperationalStatus.READY
        if (
            software is SoftwareStatus.VERIFIED
            and preflight is PreflightEvidence.READY
        )
        else OperationalStatus.BLOCKED
    )
    test_detail = (
        "SOFTWARE_TESTS_PASSED"
        if tests_passed
        else "SOFTWARE_TESTS_FAILED"
    )
    return ReleaseStatus(
        software=software,
        operational=operational,
        detail_codes=(test_detail, _PREFLIGHT_DETAIL[preflight]),
    )
