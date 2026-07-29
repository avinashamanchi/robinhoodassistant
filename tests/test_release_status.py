from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json

import pytest


_KEY = b"r" * 32
_COMMIT = "a" * 40
_SOFTWARE_RUN_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
_OPERATIONAL_RUN_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
_NOW = datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc)
_REQUIRED_STEPS = (
    "compile",
    "migration-tests",
    "security-tests",
    "safety-tests",
    "frontend-tests",
    "full-tests",
    "branch-coverage",
    "static-gate",
)


def test_evaluation_verifier_has_no_evidence_issuing_capability():
    release_status = _module()
    signer = release_status.ReleaseEvidenceSigner.from_private_bytes(
        b"r" * 32
    )
    verifier = signer.verifier()

    assert not hasattr(verifier, "authenticate_software")
    assert not hasattr(verifier, "authenticate_operational")


def _module():
    return importlib.import_module("trading_assistant.ops.release_status")


def _software_evidence(signer, **overrides):
    values = {
        "run_id": _SOFTWARE_RUN_ID,
        "commit": _COMMIT,
        "started_at": _NOW - timedelta(minutes=10),
        "finished_at": _NOW - timedelta(minutes=5),
        "expires_at": _NOW + timedelta(minutes=30),
        "required_steps": _REQUIRED_STEPS,
        "passed_steps": _REQUIRED_STEPS,
        "failed_steps": (),
    }
    values.update(overrides)
    return signer.authenticate_software(**values)


def _operational_evidence(signer, **overrides):
    values = {
        "run_id": _OPERATIONAL_RUN_ID,
        "commit": _COMMIT,
        "observed_at": _NOW - timedelta(seconds=10),
        "expires_at": _NOW + timedelta(seconds=50),
        "broker_provider": "alpaca",
        "broker_mode": "paper",
        "broker_endpoint": "https://paper-api.alpaca.markets",
        "broker_account_fingerprint": "b" * 64,
        "broker_authenticated_at": _NOW - timedelta(seconds=15),
        "local_orders_digest": "c" * 64,
        "broker_orders_digest": "c" * 64,
        "local_positions_digest": "d" * 64,
        "broker_positions_digest": "d" * 64,
        "tripped_breaker_scopes": (),
        "heartbeat_source": "daemon",
        "heartbeat_at": _NOW - timedelta(seconds=20),
        "heartbeat_stale_after_seconds": 60,
        "encryption_state": "complete",
    }
    values.update(overrides)
    return signer.authenticate_operational(**values)


def _ready_evidence():
    release_status = _module()
    signer = release_status.ReleaseEvidenceSigner.from_private_bytes(_KEY)
    verifier = signer.verifier()
    software = _software_evidence(signer)
    operational = _operational_evidence(signer)
    return release_status, signer, verifier, software, operational


def _evaluate(
    release_status,
    verifier,
    software,
    operational,
):
    return release_status.evaluate_release_status(
        software=software,
        operational=operational,
        candidate_commit=_COMMIT,
        evaluated_at=_NOW,
        verifier=verifier,
    )


def test_ready_status_requires_authenticated_complete_evidence_and_paper_target():
    release_status, _, verifier, software, operational = _ready_evidence()

    result = _evaluate(
        release_status,
        verifier,
        software,
        operational,
    )

    assert result.as_dict() == {
        "software_status": "verified",
        "operational_status": "ready",
        "software_detail_codes": ["SOFTWARE_VERIFIED"],
        "operational_detail_codes": ["PREFLIGHT_READY"],
        "evidence": {
            "candidate_commit": _COMMIT,
            "software_run_id": _SOFTWARE_RUN_ID,
            "operational_run_id": _OPERATIONAL_RUN_ID,
            "evaluated_at": "2026-07-29T16:00:00Z",
        },
        "paper_only": True,
        "execution_authorized": False,
    }


def test_status_dimensions_are_independent_and_combined_gate_is_separate():
    (
        release_status,
        signer,
        verifier,
        software,
        operational,
    ) = _ready_evidence()
    failed_software = _software_evidence(
        signer,
        passed_steps=tuple(
            step for step in _REQUIRED_STEPS if step != "full-tests"
        ),
        failed_steps=("full-tests",),
    )
    tripped_operations = _operational_evidence(
        signer,
        tripped_breaker_scopes=("global",),
    )

    software_blocked = _evaluate(
        release_status,
        verifier,
        failed_software,
        operational,
    )
    operations_blocked = _evaluate(
        release_status,
        verifier,
        software,
        tripped_operations,
    )
    both_satisfied = _evaluate(
        release_status,
        verifier,
        software,
        operational,
    )

    assert (
        software_blocked.software.value,
        software_blocked.operational.value,
    ) == ("blocked", "ready")
    assert (
        operations_blocked.software.value,
        operations_blocked.operational.value,
    ) == ("verified", "blocked")
    assert release_status.evaluate_combined_release_gate(
        software_blocked
    ).as_dict() == {
        "release_gate": "blocked",
        "blocking_dimensions": ["software"],
        "execution_authorized": False,
    }
    assert release_status.evaluate_combined_release_gate(
        operations_blocked
    ).as_dict() == {
        "release_gate": "blocked",
        "blocking_dimensions": ["operational"],
        "execution_authorized": False,
    }
    assert release_status.evaluate_combined_release_gate(
        both_satisfied
    ).as_dict() == {
        "release_gate": "satisfied",
        "blocking_dimensions": [],
        "execution_authorized": False,
    }


def test_tampered_or_nonpaper_evidence_cannot_mint_readiness_claims():
    (
        release_status,
        signer,
        verifier,
        software,
        operational,
    ) = _ready_evidence()
    tampered_software = replace(software, commit="e" * 40)
    nonpaper_operations = _operational_evidence(
        signer,
        broker_mode="cash",
        broker_endpoint="https://api.alpaca.markets",
    )

    tampered = _evaluate(
        release_status,
        verifier,
        tampered_software,
        operational,
    )
    nonpaper = _evaluate(
        release_status,
        verifier,
        software,
        nonpaper_operations,
    )

    assert tampered.software.value == "blocked"
    assert tampered.operational.value == "ready"
    assert tampered.software_run_id is None
    assert tampered.paper_only is True
    assert tampered.software_detail_codes == (
        "SOFTWARE_EVIDENCE_AUTHENTICATION_FAILED",
    )
    assert nonpaper.software.value == "verified"
    assert nonpaper.operational.value == "blocked"
    assert nonpaper.paper_only is None
    assert "BROKER_PAPER_IDENTITY_UNPROVEN" in (
        nonpaper.operational_detail_codes
    )


@pytest.mark.parametrize(
    ("overrides", "detail_code"),
    [
        (
            {"broker_orders_digest": "e" * 64},
            "BROKER_RECONCILIATION_FAILED",
        ),
        (
            {"tripped_breaker_scopes": ("equity",)},
            "BREAKER_TRIPPED",
        ),
        (
            {"heartbeat_at": _NOW - timedelta(minutes=2)},
            "DAEMON_HEARTBEAT_STALE",
        ),
        (
            {"encryption_state": "migrating"},
            "ENCRYPTION_MIXED",
        ),
    ],
)
def test_operational_controls_block_only_operational_status(
    overrides,
    detail_code,
):
    release_status, signer, verifier, software, _ = _ready_evidence()
    operational = _operational_evidence(signer, **overrides)

    result = _evaluate(
        release_status,
        verifier,
        software,
        operational,
    )

    assert result.software.value == "verified"
    assert result.operational.value == "blocked"
    assert detail_code in result.operational_detail_codes


def test_vocabulary_and_types_never_claim_unproved_trading_authority():
    release_status, _, verifier, software, operational = _ready_evidence()
    results = (
        _evaluate(
            release_status,
            verifier,
            software,
            operational,
        ),
        _evaluate(
            release_status,
            verifier,
            replace(software, signature="0" * 64),
            operational,
        ),
        _evaluate(
            release_status,
            verifier,
            software,
            replace(operational, signature="0" * 64),
        ),
    )

    for result in results:
        rendered = (
            json.dumps(result.as_dict(), sort_keys=True)
            + " "
            + result.summary
        ).lower()
        assert all(
            forbidden not in rendered
            for forbidden in (
                "live",
                "profitable",
                "autonomous",
                "daemon running",
            )
        )
        assert result.as_dict()["execution_authorized"] is False

    with pytest.raises(TypeError):
        release_status.evaluate_release_status(
            software=True,
            operational=operational,
            candidate_commit=_COMMIT,
            evaluated_at=_NOW,
            verifier=verifier,
        )
    assert repr(verifier) == "ReleaseEvidenceVerifier(<public-key>)"


def test_authenticated_partial_software_manifest_is_still_blocked():
    release_status, signer, verifier, _, operational = _ready_evidence()
    partial = _software_evidence(
        signer,
        required_steps=("compile",),
        passed_steps=("compile",),
    )

    result = _evaluate(
        release_status,
        verifier,
        partial,
        operational,
    )

    assert result.software.value == "blocked"
    assert result.operational.value == "ready"
    assert result.software_detail_codes == (
        "SOFTWARE_MANIFEST_MISMATCH",
    )


def test_malformed_signature_values_fail_closed_without_raising():
    release_status, _, verifier, software, operational = _ready_evidence()

    malformed_software = _evaluate(
        release_status,
        verifier,
        replace(software, signature=None),
        operational,
    )
    malformed_operational = _evaluate(
        release_status,
        verifier,
        software,
        replace(operational, signature=[]),
    )

    assert malformed_software.software.value == "blocked"
    assert malformed_software.operational.value == "ready"
    assert malformed_software.software_detail_codes == (
        "SOFTWARE_EVIDENCE_AUTHENTICATION_FAILED",
    )
    assert malformed_operational.software.value == "verified"
    assert malformed_operational.operational.value == "blocked"
    assert malformed_operational.operational_detail_codes == (
        "OPERATIONAL_EVIDENCE_AUTHENTICATION_FAILED",
    )
    assert malformed_operational.paper_only is None
