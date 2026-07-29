from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import inspect
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest


_SOFTWARE_KEY = b"s" * 32
_OPERATIONAL_KEY = b"o" * 32
_ATTACKER_SOFTWARE_KEY = b"x" * 32
_ATTACKER_OPERATIONAL_KEY = b"y" * 32
_COMMIT = "a" * 40
_OTHER_COMMIT = "b" * 40
_SHA256_COMMIT = "c" * 64
_SOFTWARE_RUN_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
_OPERATIONAL_RUN_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
_NOW = datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc)
_ACCOUNT_FINGERPRINT = hashlib.sha256(
    b"intended-generated-paper-account"
).hexdigest()
_OTHER_ACCOUNT_FINGERPRINT = hashlib.sha256(
    b"different-generated-paper-account"
).hexdigest()
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


@dataclass
class _TrustedNow:
    value: datetime

    def now(self) -> datetime:
        return self.value


@dataclass
class _RepositoryHead:
    value: str

    def current_head(self) -> str:
        return self.value


def _module():
    return importlib.import_module("trading_assistant.ops.release_status")


def _trust_bundle(
    *,
    now: datetime = _NOW,
    head: str = _COMMIT,
    account_fingerprint: str = _ACCOUNT_FINGERPRINT,
    heartbeat_max_age_seconds: int = 60,
):
    release_status = _module()
    software_signer = release_status.SoftwareEvidenceSigner.from_private_bytes(
        _SOFTWARE_KEY
    )
    operational_signer = (
        release_status.OperationalEvidenceSigner.from_private_bytes(
            _OPERATIONAL_KEY
        )
    )
    clock = _TrustedNow(now)
    repository = _RepositoryHead(head)
    policy = release_status.ReleaseTrustPolicy(
        software_verifier=software_signer.verifier(),
        operational_verifier=operational_signer.verifier(),
        intended_account_fingerprint=account_fingerprint,
        heartbeat_max_age_seconds=heartbeat_max_age_seconds,
        now_provider=clock,
        repository_head_resolver=repository,
    )
    return (
        release_status,
        software_signer,
        operational_signer,
        policy,
        clock,
        repository,
    )


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
    return signer.authenticate(**values)


def _reconciliation(
    release_status,
    domain,
    *,
    generation: int = 17,
    local_records: tuple[str, ...] | None = None,
    broker_records: tuple[str, ...] | None = None,
):
    defaults = {
        release_status.ReconciliationDomain.ORDERS: (
            "client-order-001",
            "client-order-002",
        ),
        release_status.ReconciliationDomain.POSITIONS: (
            "AAPL:10",
            "MSFT:4",
        ),
    }
    local = local_records or defaults[domain]
    broker = broker_records or local
    return release_status.ReconciliationEvidence.collect(
        domain=domain,
        generation=generation,
        local_records=local,
        broker_records=broker,
    )


def _operational_evidence(
    release_status,
    signer,
    **overrides,
):
    values = {
        "run_id": _OPERATIONAL_RUN_ID,
        "commit": _COMMIT,
        "observed_at": _NOW - timedelta(seconds=10),
        "expires_at": _NOW + timedelta(seconds=50),
        "broker_provider": "alpaca",
        "broker_mode": "paper",
        "broker_endpoint": "https://paper-api.alpaca.markets",
        "broker_account_fingerprint": _ACCOUNT_FINGERPRINT,
        "broker_authenticated_at": _NOW - timedelta(seconds=15),
        "orders_reconciliation": _reconciliation(
            release_status,
            release_status.ReconciliationDomain.ORDERS,
        ),
        "positions_reconciliation": _reconciliation(
            release_status,
            release_status.ReconciliationDomain.POSITIONS,
        ),
        "tripped_breaker_scopes": (),
        "heartbeat_source": "daemon",
        "heartbeat_at": _NOW - timedelta(seconds=20),
        "encryption_state": "complete",
    }
    values.update(overrides)
    return signer.authenticate(**values)


def _ready_evidence():
    (
        release_status,
        software_signer,
        operational_signer,
        policy,
        clock,
        repository,
    ) = _trust_bundle()
    software = _software_evidence(software_signer)
    operational = _operational_evidence(
        release_status,
        operational_signer,
    )
    return (
        release_status,
        software_signer,
        operational_signer,
        policy,
        clock,
        repository,
        software,
        operational,
    )


def _evaluate(release_status, policy, software, operational):
    return release_status.evaluate_release_status(
        software=software,
        operational=operational,
        trust_policy=policy,
    )


def _gate(release_status, policy, software, operational):
    return release_status.evaluate_combined_release_gate(
        software=software,
        operational=operational,
        trust_policy=policy,
    )


def test_ready_status_requires_pinned_evidence_current_head_and_paper_account():
    (
        release_status,
        _,
        _,
        policy,
        _,
        _,
        software,
        operational,
    ) = _ready_evidence()

    result = _evaluate(
        release_status,
        policy,
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
    assert _gate(
        release_status,
        policy,
        software,
        operational,
    ).as_dict() == {
        "release_gate": "satisfied",
        "blocking_dimensions": [],
        "execution_authorized": False,
    }


def test_trusted_policy_rejects_self_issued_attacker_evidence():
    (
        release_status,
        _,
        _,
        policy,
        _,
        _,
        _,
        _,
    ) = _ready_evidence()
    attacker_software = (
        release_status.SoftwareEvidenceSigner.from_private_bytes(
            _ATTACKER_SOFTWARE_KEY
        )
    )
    attacker_operational = (
        release_status.OperationalEvidenceSigner.from_private_bytes(
            _ATTACKER_OPERATIONAL_KEY
        )
    )

    result = _evaluate(
        release_status,
        policy,
        _software_evidence(attacker_software),
        _operational_evidence(
            release_status,
            attacker_operational,
        ),
    )

    assert result.software.value == "blocked"
    assert result.operational.value == "blocked"
    assert result.software_detail_codes == (
        "SOFTWARE_EVIDENCE_AUTHENTICATION_FAILED",
    )
    assert result.operational_detail_codes == (
        "OPERATIONAL_EVIDENCE_AUTHENTICATION_FAILED",
    )
    parameters = inspect.signature(
        release_status.evaluate_release_status
    ).parameters
    assert tuple(parameters) == ("software", "operational", "trust_policy")
    assert all(
        unsafe not in parameters
        for unsafe in ("verifier", "candidate_commit", "evaluated_at")
    )


def test_role_specific_key_capabilities_are_distinct_and_not_interchangeable():
    release_status = _module()
    software_signer = release_status.SoftwareEvidenceSigner.from_private_bytes(
        _SOFTWARE_KEY
    )
    operational_signer = (
        release_status.OperationalEvidenceSigner.from_private_bytes(
            _OPERATIONAL_KEY
        )
    )

    assert not hasattr(software_signer, "authenticate_operational")
    assert not hasattr(operational_signer, "authenticate_software")
    assert not hasattr(software_signer, "authenticate_operational_evidence")
    assert not hasattr(operational_signer, "authenticate_software_evidence")
    assert type(software_signer.verifier()) is (
        release_status.SoftwareEvidenceVerifier
    )
    assert type(operational_signer.verifier()) is (
        release_status.OperationalEvidenceVerifier
    )
    with pytest.raises(TypeError):
        release_status.ReleaseTrustPolicy(
            software_verifier=operational_signer.verifier(),
            operational_verifier=software_signer.verifier(),
            intended_account_fingerprint=_ACCOUNT_FINGERPRINT,
            heartbeat_max_age_seconds=60,
            now_provider=_TrustedNow(_NOW),
            repository_head_resolver=_RepositoryHead(_COMMIT),
        )


def test_trust_policy_rejects_same_key_material_for_both_authorities():
    release_status = _module()
    software = release_status.SoftwareEvidenceSigner.from_private_bytes(
        _SOFTWARE_KEY
    )
    operational = (
        release_status.OperationalEvidenceSigner.from_private_bytes(
            _SOFTWARE_KEY
        )
    )

    with pytest.raises(ValueError, match="distinct"):
        release_status.ReleaseTrustPolicy(
            software_verifier=software.verifier(),
            operational_verifier=operational.verifier(),
            intended_account_fingerprint=_ACCOUNT_FINGERPRINT,
            heartbeat_max_age_seconds=60,
            now_provider=_TrustedNow(_NOW),
            repository_head_resolver=_RepositoryHead(_COMMIT),
        )


def test_trust_policy_verifier_keys_are_deeply_immutable():
    (
        release_status,
        _,
        _,
        policy,
        _,
        _,
    ) = _trust_bundle()
    replacement = Ed25519PrivateKey.from_private_bytes(
        _ATTACKER_SOFTWARE_KEY
    ).public_key()

    with pytest.raises(FrozenInstanceError):
        policy.software_verifier._public_key = replacement
    with pytest.raises(FrozenInstanceError):
        policy.operational_verifier._public_key = replacement


def test_gate_reverifies_raw_receipts_on_every_call_and_status_is_not_authority():
    (
        release_status,
        _,
        _,
        policy,
        clock,
        _,
        software,
        operational,
    ) = _ready_evidence()
    status = _evaluate(
        release_status,
        policy,
        software,
        operational,
    )
    assert _gate(
        release_status,
        policy,
        software,
        operational,
    ).status.value == "satisfied"

    clock.value = _NOW + timedelta(minutes=31)

    assert _gate(
        release_status,
        policy,
        software,
        operational,
    ).as_dict() == {
        "release_gate": "blocked",
        "blocking_dimensions": ["software", "operational"],
        "execution_authorized": False,
    }
    with pytest.raises(TypeError):
        release_status.evaluate_combined_release_gate(status)
    with pytest.raises(TypeError, match="software evidence"):
        release_status.evaluate_combined_release_gate(
            software=status,
            operational=operational,
            trust_policy=policy,
        )
    with pytest.raises(FrozenInstanceError):
        policy.heartbeat_max_age_seconds = 3_600


def test_gate_rechecks_current_head_on_every_call():
    (
        release_status,
        _,
        _,
        policy,
        _,
        repository,
        software,
        operational,
    ) = _ready_evidence()
    assert _gate(
        release_status,
        policy,
        software,
        operational,
    ).status.value == "satisfied"

    repository.value = _OTHER_COMMIT

    result = _gate(
        release_status,
        policy,
        software,
        operational,
    )
    assert result.status.value == "blocked"
    assert result.blocking_dimensions == ("software", "operational")


def test_signed_evidence_for_wrong_paper_account_cannot_be_ready():
    (
        release_status,
        _,
        operational_signer,
        policy,
        _,
        _,
        software,
        _,
    ) = _ready_evidence()
    wrong_account = _operational_evidence(
        release_status,
        operational_signer,
        broker_account_fingerprint=_OTHER_ACCOUNT_FINGERPRINT,
    )

    result = _evaluate(
        release_status,
        policy,
        software,
        wrong_account,
    )

    assert result.software.value == "verified"
    assert result.operational.value == "blocked"
    assert result.paper_only is None
    assert "BROKER_ACCOUNT_MISMATCH" in result.operational_detail_codes


@pytest.mark.parametrize(
    "overrides",
    [
        {"broker_provider": "other"},
        {"broker_mode": "live"},
        {"broker_endpoint": "https://api.alpaca.markets"},
    ],
)
def test_nonpaper_target_cannot_be_operationally_ready(overrides):
    (
        release_status,
        _,
        operational_signer,
        policy,
        _,
        _,
        software,
        _,
    ) = _ready_evidence()
    nonpaper = _operational_evidence(
        release_status,
        operational_signer,
        **overrides,
    )

    result = _evaluate(
        release_status,
        policy,
        software,
        nonpaper,
    )

    assert result.software.value == "verified"
    assert result.operational.value == "blocked"
    assert result.paper_only is None
    assert "BROKER_PAPER_IDENTITY_UNPROVEN" in (
        result.operational_detail_codes
    )


def test_trusted_policy_not_receipt_controls_heartbeat_freshness():
    (
        release_status,
        _,
        operational_signer,
        policy,
        _,
        _,
        software,
        _,
    ) = _ready_evidence()
    stale_heartbeat = _operational_evidence(
        release_status,
        operational_signer,
        heartbeat_at=_NOW - timedelta(seconds=61),
    )

    result = _evaluate(
        release_status,
        policy,
        software,
        stale_heartbeat,
    )

    assert not hasattr(stale_heartbeat, "heartbeat_stale_after_seconds")
    assert result.operational.value == "blocked"
    assert "DAEMON_HEARTBEAT_STALE" in result.operational_detail_codes


def test_reconciliation_evidence_is_typed_nonempty_and_domain_bound():
    release_status = _module()
    with pytest.raises(ValueError, match="nonempty"):
        release_status.ReconciliationEvidence.collect(
            domain=release_status.ReconciliationDomain.ORDERS,
            generation=1,
            local_records=(),
            broker_records=(),
        )
    with pytest.raises(ValueError, match="positive"):
        release_status.ReconciliationEvidence.collect(
            domain=release_status.ReconciliationDomain.ORDERS,
            generation=0,
            local_records=("order-1",),
            broker_records=("order-1",),
        )
    with pytest.raises(TypeError):
        release_status.ReconciliationEvidence(
            domain=release_status.ReconciliationDomain.ORDERS,
            generation=1,
            local_count=1,
            broker_count=1,
            local_digest="0" * 64,
            broker_digest="0" * 64,
        )

    orders = _reconciliation(
        release_status,
        release_status.ReconciliationDomain.ORDERS,
        generation=23,
        local_records=("same-record",),
        broker_records=("same-record",),
    )
    positions = _reconciliation(
        release_status,
        release_status.ReconciliationDomain.POSITIONS,
        generation=23,
        local_records=("same-record",),
        broker_records=("same-record",),
    )

    assert orders.local_count == orders.broker_count == 1
    assert positions.local_count == positions.broker_count == 1
    assert orders.local_digest == orders.broker_digest
    assert positions.local_digest == positions.broker_digest
    assert orders.local_digest != positions.local_digest
    assert orders.local_digest != "0" * 64


@pytest.mark.parametrize(
    ("orders_overrides", "positions_overrides", "detail_code"),
    [
        (
            {
                "broker_records": (
                    "client-order-001",
                    "client-order-different",
                )
            },
            {},
            "BROKER_RECONCILIATION_FAILED",
        ),
        (
            {"generation": 17},
            {"generation": 18},
            "RECONCILIATION_GENERATION_MISMATCH",
        ),
    ],
)
def test_typed_reconciliation_mismatch_blocks_operational_readiness(
    orders_overrides,
    positions_overrides,
    detail_code,
):
    (
        release_status,
        _,
        operational_signer,
        policy,
        _,
        _,
        software,
        _,
    ) = _ready_evidence()
    orders = _reconciliation(
        release_status,
        release_status.ReconciliationDomain.ORDERS,
        **orders_overrides,
    )
    positions = _reconciliation(
        release_status,
        release_status.ReconciliationDomain.POSITIONS,
        **positions_overrides,
    )
    operational = _operational_evidence(
        release_status,
        operational_signer,
        orders_reconciliation=orders,
        positions_reconciliation=positions,
    )

    result = _evaluate(
        release_status,
        policy,
        software,
        operational,
    )

    assert result.operational.value == "blocked"
    assert detail_code in result.operational_detail_codes


@pytest.mark.parametrize("length", [39, 41, 63, 65])
def test_signers_reject_noncanonical_git_object_id_lengths(length):
    (
        release_status,
        software_signer,
        operational_signer,
        _,
        _,
        _,
    ) = _trust_bundle()
    invalid = "a" * length

    with pytest.raises(ValueError, match="Git object"):
        _software_evidence(software_signer, commit=invalid)
    with pytest.raises(ValueError, match="Git object"):
        _operational_evidence(
            release_status,
            operational_signer,
            commit=invalid,
        )


def test_sha256_head_is_supported_when_resolver_confirms_it():
    (
        release_status,
        software_signer,
        operational_signer,
        policy,
        _,
        _,
    ) = _trust_bundle(head=_SHA256_COMMIT)
    software = _software_evidence(
        software_signer,
        commit=_SHA256_COMMIT,
    )
    operational = _operational_evidence(
        release_status,
        operational_signer,
        commit=_SHA256_COMMIT,
    )

    result = _evaluate(
        release_status,
        policy,
        software,
        operational,
    )

    assert result.software.value == "verified"
    assert result.operational.value == "ready"
    assert result.candidate_commit == _SHA256_COMMIT


def test_authentic_evidence_for_non_head_commit_is_blocked():
    (
        release_status,
        software_signer,
        operational_signer,
        policy,
        _,
        _,
        _,
        _,
    ) = _ready_evidence()
    old_software = _software_evidence(
        software_signer,
        commit=_OTHER_COMMIT,
    )
    old_operational = _operational_evidence(
        release_status,
        operational_signer,
        commit=_OTHER_COMMIT,
    )

    result = _evaluate(
        release_status,
        policy,
        old_software,
        old_operational,
    )

    assert result.software_detail_codes == ("SOFTWARE_COMMIT_MISMATCH",)
    assert "OPERATIONAL_COMMIT_MISMATCH" in (
        result.operational_detail_codes
    )


def test_invalid_repository_head_blocks_both_dimensions():
    (
        release_status,
        _,
        _,
        policy,
        _,
        repository,
        software,
        operational,
    ) = _ready_evidence()
    repository.value = "f" * 41

    result = _evaluate(
        release_status,
        policy,
        software,
        operational,
    )

    assert result.candidate_commit is None
    assert result.software_detail_codes == ("REPOSITORY_HEAD_UNPROVEN",)
    assert result.operational_detail_codes == ("REPOSITORY_HEAD_UNPROVEN",)


def test_trusted_context_provider_failure_blocks_instead_of_escaping():
    (
        release_status,
        _,
        _,
        policy,
        _,
        _,
        software,
        operational,
    ) = _ready_evidence()

    class FailingNow:
        def now(self):
            raise RuntimeError("generated clock failure")

    unavailable_time = replace(policy, now_provider=FailingNow())

    result = _evaluate(
        release_status,
        unavailable_time,
        software,
        operational,
    )

    assert "TRUSTED_TIME_UNAVAILABLE" in result.software_detail_codes
    assert "TRUSTED_TIME_UNAVAILABLE" in result.operational_detail_codes
    assert result.software.value == "blocked"
    assert result.operational.value == "blocked"


def _resign_software_version(release_status, evidence, version):
    unsigned = replace(evidence, version=version, signature="")
    signature = Ed25519PrivateKey.from_private_bytes(_SOFTWARE_KEY).sign(
        release_status._SOFTWARE_DOMAIN
        + b"\0"
        + release_status._encoded_payload(
            release_status._software_payload(unsigned)
        )
    )
    return replace(unsigned, signature=signature.hex())


def _resign_operational_version(release_status, evidence, version):
    unsigned = replace(evidence, version=version, signature="")
    signature = Ed25519PrivateKey.from_private_bytes(_OPERATIONAL_KEY).sign(
        release_status._OPERATIONAL_DOMAIN
        + b"\0"
        + release_status._encoded_payload(
            release_status._operational_payload(unsigned)
        )
    )
    return replace(unsigned, signature=signature.hex())


def test_unknown_signed_evidence_versions_fail_closed():
    (
        release_status,
        _,
        _,
        policy,
        _,
        _,
        software,
        operational,
    ) = _ready_evidence()
    version_two_software = _resign_software_version(
        release_status,
        software,
        2,
    )
    version_two_operational = _resign_operational_version(
        release_status,
        operational,
        2,
    )

    result = _evaluate(
        release_status,
        policy,
        version_two_software,
        version_two_operational,
    )

    assert result.software_detail_codes == (
        "SOFTWARE_EVIDENCE_VERSION_UNSUPPORTED",
    )
    assert result.operational_detail_codes == (
        "OPERATIONAL_EVIDENCE_VERSION_UNSUPPORTED",
    )


def test_status_dimensions_remain_independent_and_combined_gate_is_separate():
    (
        release_status,
        software_signer,
        operational_signer,
        policy,
        _,
        _,
        software,
        operational,
    ) = _ready_evidence()
    failed_software = _software_evidence(
        software_signer,
        passed_steps=tuple(
            step for step in _REQUIRED_STEPS if step != "full-tests"
        ),
        failed_steps=("full-tests",),
    )
    tripped_operational = _operational_evidence(
        release_status,
        operational_signer,
        tripped_breaker_scopes=("global",),
    )

    software_blocked = _evaluate(
        release_status,
        policy,
        failed_software,
        operational,
    )
    operations_blocked = _evaluate(
        release_status,
        policy,
        software,
        tripped_operational,
    )

    assert (
        software_blocked.software.value,
        software_blocked.operational.value,
    ) == ("blocked", "ready")
    assert (
        operations_blocked.software.value,
        operations_blocked.operational.value,
    ) == ("verified", "blocked")
    assert _gate(
        release_status,
        policy,
        failed_software,
        operational,
    ).as_dict() == {
        "release_gate": "blocked",
        "blocking_dimensions": ["software"],
        "execution_authorized": False,
    }
    assert _gate(
        release_status,
        policy,
        software,
        tripped_operational,
    ).as_dict() == {
        "release_gate": "blocked",
        "blocking_dimensions": ["operational"],
        "execution_authorized": False,
    }


def test_tampering_and_malformed_signatures_fail_closed_without_cross_coupling():
    (
        release_status,
        _,
        _,
        policy,
        _,
        _,
        software,
        operational,
    ) = _ready_evidence()
    tampered_software = replace(software, commit=_OTHER_COMMIT)
    malformed_operational = replace(operational, signature=[])

    result = _evaluate(
        release_status,
        policy,
        tampered_software,
        malformed_operational,
    )

    assert result.software.value == "blocked"
    assert result.operational.value == "blocked"
    assert result.software_run_id is None
    assert result.operational_run_id is None
    assert result.software_detail_codes == (
        "SOFTWARE_EVIDENCE_AUTHENTICATION_FAILED",
    )
    assert result.operational_detail_codes == (
        "OPERATIONAL_EVIDENCE_AUTHENTICATION_FAILED",
    )
    assert result.paper_only is None


def test_vocabulary_never_claims_unproved_trading_authority():
    (
        release_status,
        _,
        _,
        policy,
        _,
        _,
        software,
        operational,
    ) = _ready_evidence()
    result = _evaluate(
        release_status,
        policy,
        software,
        operational,
    )
    rendered = (
        json.dumps(result.as_dict(), sort_keys=True) + " " + result.summary
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
    assert _gate(
        release_status,
        policy,
        software,
        operational,
    ).as_dict()["execution_authorized"] is False
