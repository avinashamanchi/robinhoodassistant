"""Authenticated, fail-closed release status classification.

This module classifies evidence only. It never authorizes an order, starts a
service, resets a breaker, or turns a release result into trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import math
import re
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


_OFFICIAL_ALPACA_PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
_SOFTWARE_DOMAIN = b"trading-assistant/release-software-evidence/v1"
_OPERATIONAL_DOMAIN = b"trading-assistant/release-operational-evidence/v1"
_COMMIT = re.compile(r"[0-9a-f]{40,64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SIGNATURE = re.compile(r"[0-9a-f]{128}")
_EVIDENCE_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_SCOPE = re.compile(r"[a-z][a-z0-9:_.-]{0,127}")
_MAX_SOFTWARE_TTL = timedelta(hours=24)
_MAX_OPERATIONAL_TTL = timedelta(minutes=5)
_MAX_BROKER_AUTH_AGE = timedelta(minutes=5)
_REQUIRED_SOFTWARE_STEPS = (
    "compile",
    "migration-tests",
    "security-tests",
    "safety-tests",
    "frontend-tests",
    "full-tests",
    "branch-coverage",
    "static-gate",
)


class SoftwareStatus(StrEnum):
    """Result derived only from authenticated software-run evidence."""

    VERIFIED = "verified"
    BLOCKED = "blocked"


class OperationalStatus(StrEnum):
    """Result derived only from authenticated operational evidence."""

    READY = "ready"
    BLOCKED = "blocked"


class CombinedReleaseGateStatus(StrEnum):
    """Publication gate derived from both independent dimensions."""

    SATISFIED = "satisfied"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SoftwareVerificationEvidence:
    """Signed facts emitted by one bounded deterministic verification run."""

    version: int
    run_id: str
    commit: str
    started_at: datetime
    finished_at: datetime
    expires_at: datetime
    required_steps: tuple[str, ...]
    passed_steps: tuple[str, ...]
    failed_steps: tuple[str, ...]
    signature: str


@dataclass(frozen=True, slots=True)
class OperationalReadinessEvidence:
    """Signed facts from one bounded, read-only paper preflight observation."""

    version: int
    run_id: str
    commit: str
    observed_at: datetime
    expires_at: datetime
    broker_provider: str
    broker_mode: str
    broker_endpoint: str
    broker_account_fingerprint: str
    broker_authenticated_at: datetime
    local_orders_digest: str
    broker_orders_digest: str
    local_positions_digest: str
    broker_positions_digest: str
    tripped_breaker_scopes: tuple[str, ...]
    heartbeat_source: str
    heartbeat_at: datetime
    heartbeat_stale_after_seconds: int
    encryption_state: str
    signature: str


def _canonical_utc(value: datetime, *, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return (
        _canonical_utc(value, name="timestamp")
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_run_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("run_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("run_id must be a canonical UUID") from None
    canonical = str(parsed)
    if value != canonical:
        raise ValueError("run_id must be a canonical UUID")
    return value


def _canonical_commit(value: str, *, name: str = "commit") -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical Git object id")
    return value


def _canonical_digest(value: str, *, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _canonical_names(
    values: tuple[str, ...],
    *,
    name: str,
    pattern: re.Pattern[str],
    allow_empty: bool,
) -> tuple[str, ...]:
    if (
        not isinstance(values, tuple)
        or (not allow_empty and not values)
        or any(
            not isinstance(value, str) or pattern.fullmatch(value) is None
            for value in values
        )
        or len(set(values)) != len(values)
    ):
        raise ValueError(f"{name} must contain unique canonical names")
    return values


def _canonical_endpoint(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("broker_endpoint must be an HTTPS origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("broker_endpoint must be an HTTPS origin")
    return value.rstrip("/")


def _software_payload(
    evidence: SoftwareVerificationEvidence,
) -> dict[str, object]:
    return {
        "version": evidence.version,
        "run_id": evidence.run_id,
        "commit": evidence.commit,
        "started_at": _utc_text(evidence.started_at),
        "finished_at": _utc_text(evidence.finished_at),
        "expires_at": _utc_text(evidence.expires_at),
        "required_steps": list(evidence.required_steps),
        "passed_steps": list(evidence.passed_steps),
        "failed_steps": list(evidence.failed_steps),
    }


def _operational_payload(
    evidence: OperationalReadinessEvidence,
) -> dict[str, object]:
    return {
        "version": evidence.version,
        "run_id": evidence.run_id,
        "commit": evidence.commit,
        "observed_at": _utc_text(evidence.observed_at),
        "expires_at": _utc_text(evidence.expires_at),
        "broker_provider": evidence.broker_provider,
        "broker_mode": evidence.broker_mode,
        "broker_endpoint": evidence.broker_endpoint,
        "broker_account_fingerprint": evidence.broker_account_fingerprint,
        "broker_authenticated_at": _utc_text(
            evidence.broker_authenticated_at
        ),
        "local_orders_digest": evidence.local_orders_digest,
        "broker_orders_digest": evidence.broker_orders_digest,
        "local_positions_digest": evidence.local_positions_digest,
        "broker_positions_digest": evidence.broker_positions_digest,
        "tripped_breaker_scopes": list(evidence.tripped_breaker_scopes),
        "heartbeat_source": evidence.heartbeat_source,
        "heartbeat_at": _utc_text(evidence.heartbeat_at),
        "heartbeat_stale_after_seconds": (
            evidence.heartbeat_stale_after_seconds
        ),
        "encryption_state": evidence.encryption_state,
    }


def _encoded_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


class ReleaseEvidenceSigner:
    """Private collector capability that issues release evidence receipts."""

    __slots__ = ("_private_key",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("release evidence private key is invalid")
        self._private_key = private_key

    @classmethod
    def from_private_bytes(cls, key: bytes) -> "ReleaseEvidenceSigner":
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("release evidence private key must be 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(key))

    def __repr__(self) -> str:
        return "ReleaseEvidenceSigner(<redacted>)"

    def verifier(self) -> "ReleaseEvidenceVerifier":
        """Return the public-only evaluation capability."""
        return ReleaseEvidenceVerifier(self._private_key.public_key())

    def _signature(
        self,
        domain: bytes,
        payload: dict[str, object],
    ) -> str:
        return self._private_key.sign(
            domain + b"\0" + _encoded_payload(payload),
        ).hex()

    def authenticate_software(
        self,
        *,
        run_id: str,
        commit: str,
        started_at: datetime,
        finished_at: datetime,
        expires_at: datetime,
        required_steps: tuple[str, ...],
        passed_steps: tuple[str, ...],
        failed_steps: tuple[str, ...],
    ) -> SoftwareVerificationEvidence:
        """Authenticate structurally valid facts, including failed runs."""
        run_id = _canonical_run_id(run_id)
        commit = _canonical_commit(commit)
        started_at = _canonical_utc(started_at, name="started_at")
        finished_at = _canonical_utc(finished_at, name="finished_at")
        expires_at = _canonical_utc(expires_at, name="expires_at")
        if (
            started_at > finished_at
            or finished_at >= expires_at
            or expires_at - finished_at > _MAX_SOFTWARE_TTL
        ):
            raise ValueError("software evidence timestamps are invalid")
        required_steps = _canonical_names(
            required_steps,
            name="required_steps",
            pattern=_EVIDENCE_NAME,
            allow_empty=False,
        )
        passed_steps = _canonical_names(
            passed_steps,
            name="passed_steps",
            pattern=_EVIDENCE_NAME,
            allow_empty=True,
        )
        failed_steps = _canonical_names(
            failed_steps,
            name="failed_steps",
            pattern=_EVIDENCE_NAME,
            allow_empty=True,
        )
        required = set(required_steps)
        if (
            not set(passed_steps).issubset(required)
            or not set(failed_steps).issubset(required)
            or set(passed_steps).intersection(failed_steps)
        ):
            raise ValueError("software step evidence is inconsistent")
        unsigned = SoftwareVerificationEvidence(
            version=1,
            run_id=run_id,
            commit=commit,
            started_at=started_at,
            finished_at=finished_at,
            expires_at=expires_at,
            required_steps=required_steps,
            passed_steps=passed_steps,
            failed_steps=failed_steps,
            signature="",
        )
        return replace(
            unsigned,
            signature=self._signature(
                _SOFTWARE_DOMAIN,
                _software_payload(unsigned),
            ),
        )

    def authenticate_operational(
        self,
        *,
        run_id: str,
        commit: str,
        observed_at: datetime,
        expires_at: datetime,
        broker_provider: str,
        broker_mode: str,
        broker_endpoint: str,
        broker_account_fingerprint: str,
        broker_authenticated_at: datetime,
        local_orders_digest: str,
        broker_orders_digest: str,
        local_positions_digest: str,
        broker_positions_digest: str,
        tripped_breaker_scopes: tuple[str, ...],
        heartbeat_source: str,
        heartbeat_at: datetime,
        heartbeat_stale_after_seconds: int,
        encryption_state: str,
    ) -> OperationalReadinessEvidence:
        """Authenticate structurally valid observations, ready or blocked."""
        run_id = _canonical_run_id(run_id)
        commit = _canonical_commit(commit)
        observed_at = _canonical_utc(observed_at, name="observed_at")
        expires_at = _canonical_utc(expires_at, name="expires_at")
        broker_authenticated_at = _canonical_utc(
            broker_authenticated_at,
            name="broker_authenticated_at",
        )
        heartbeat_at = _canonical_utc(
            heartbeat_at,
            name="heartbeat_at",
        )
        if (
            observed_at >= expires_at
            or expires_at - observed_at > _MAX_OPERATIONAL_TTL
            or broker_authenticated_at > observed_at
            or observed_at - broker_authenticated_at > _MAX_BROKER_AUTH_AGE
            or heartbeat_at > observed_at
        ):
            raise ValueError("operational evidence timestamps are invalid")
        if (
            not isinstance(broker_provider, str)
            or _EVIDENCE_NAME.fullmatch(broker_provider) is None
            or not isinstance(broker_mode, str)
            or _EVIDENCE_NAME.fullmatch(broker_mode) is None
            or not isinstance(heartbeat_source, str)
            or _EVIDENCE_NAME.fullmatch(heartbeat_source) is None
            or not isinstance(encryption_state, str)
            or _EVIDENCE_NAME.fullmatch(encryption_state) is None
        ):
            raise ValueError("operational evidence labels are invalid")
        broker_endpoint = _canonical_endpoint(broker_endpoint)
        broker_account_fingerprint = _canonical_digest(
            broker_account_fingerprint,
            name="broker_account_fingerprint",
        )
        local_orders_digest = _canonical_digest(
            local_orders_digest,
            name="local_orders_digest",
        )
        broker_orders_digest = _canonical_digest(
            broker_orders_digest,
            name="broker_orders_digest",
        )
        local_positions_digest = _canonical_digest(
            local_positions_digest,
            name="local_positions_digest",
        )
        broker_positions_digest = _canonical_digest(
            broker_positions_digest,
            name="broker_positions_digest",
        )
        tripped_breaker_scopes = _canonical_names(
            tripped_breaker_scopes,
            name="tripped_breaker_scopes",
            pattern=_SCOPE,
            allow_empty=True,
        )
        if (
            isinstance(heartbeat_stale_after_seconds, bool)
            or not isinstance(heartbeat_stale_after_seconds, int)
            or heartbeat_stale_after_seconds <= 0
            or heartbeat_stale_after_seconds > 3_600
        ):
            raise ValueError("heartbeat stale bound is invalid")
        unsigned = OperationalReadinessEvidence(
            version=1,
            run_id=run_id,
            commit=commit,
            observed_at=observed_at,
            expires_at=expires_at,
            broker_provider=broker_provider,
            broker_mode=broker_mode,
            broker_endpoint=broker_endpoint,
            broker_account_fingerprint=broker_account_fingerprint,
            broker_authenticated_at=broker_authenticated_at,
            local_orders_digest=local_orders_digest,
            broker_orders_digest=broker_orders_digest,
            local_positions_digest=local_positions_digest,
            broker_positions_digest=broker_positions_digest,
            tripped_breaker_scopes=tripped_breaker_scopes,
            heartbeat_source=heartbeat_source,
            heartbeat_at=heartbeat_at,
            heartbeat_stale_after_seconds=heartbeat_stale_after_seconds,
            encryption_state=encryption_state,
            signature="",
        )
        payload = _operational_payload(unsigned)
        return replace(
            unsigned,
            signature=self._signature(
                _OPERATIONAL_DOMAIN,
                payload,
            ),
        )


class ReleaseEvidenceVerifier:
    """Public-only capability that authenticates release evidence receipts."""

    __slots__ = ("_public_key",)

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("release evidence public key is invalid")
        self._public_key = public_key

    @classmethod
    def from_public_bytes(cls, key: bytes) -> "ReleaseEvidenceVerifier":
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("release evidence public key must be 32 bytes")
        return cls(Ed25519PublicKey.from_public_bytes(key))

    def __repr__(self) -> str:
        return "ReleaseEvidenceVerifier(<public-key>)"

    def verifies_software(
        self,
        evidence: SoftwareVerificationEvidence,
    ) -> bool:
        if (
            not isinstance(evidence, SoftwareVerificationEvidence)
            or not isinstance(evidence.signature, str)
            or _SIGNATURE.fullmatch(evidence.signature) is None
        ):
            return False
        try:
            self._public_key.verify(
                bytes.fromhex(evidence.signature),
                _SOFTWARE_DOMAIN
                + b"\0"
                + _encoded_payload(_software_payload(evidence)),
            )
        except (InvalidSignature, TypeError, ValueError):
            return False
        return True

    def verifies_operational(
        self,
        evidence: OperationalReadinessEvidence,
    ) -> bool:
        if (
            not isinstance(evidence, OperationalReadinessEvidence)
            or not isinstance(evidence.signature, str)
            or _SIGNATURE.fullmatch(evidence.signature) is None
        ):
            return False
        try:
            self._public_key.verify(
                bytes.fromhex(evidence.signature),
                _OPERATIONAL_DOMAIN
                + b"\0"
                + _encoded_payload(_operational_payload(evidence)),
            )
        except (InvalidSignature, TypeError, ValueError):
            return False
        return True


@dataclass(frozen=True, slots=True)
class ReleaseStatus:
    """Two independently classified dimensions and their evidence references."""

    software: SoftwareStatus
    operational: OperationalStatus
    software_detail_codes: tuple[str, ...]
    operational_detail_codes: tuple[str, ...]
    candidate_commit: str
    software_run_id: str | None
    operational_run_id: str | None
    evaluated_at: datetime
    paper_only: bool | None

    @property
    def summary(self) -> str:
        target = (
            "Alpaca paper target verified"
            if self.paper_only is True
            else "broker target unverified"
        )
        return (
            f"software {self.software.value}; "
            f"operational {self.operational.value}; "
            f"{target}; execution not authorized"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "software_status": self.software.value,
            "operational_status": self.operational.value,
            "software_detail_codes": list(self.software_detail_codes),
            "operational_detail_codes": list(
                self.operational_detail_codes
            ),
            "evidence": {
                "candidate_commit": self.candidate_commit,
                "software_run_id": self.software_run_id,
                "operational_run_id": self.operational_run_id,
                "evaluated_at": _utc_text(self.evaluated_at),
            },
            "paper_only": self.paper_only,
            "execution_authorized": False,
        }


@dataclass(frozen=True, slots=True)
class CombinedReleaseGate:
    """Non-execution publication gate over an evaluated release status."""

    status: CombinedReleaseGateStatus
    blocking_dimensions: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "release_gate": self.status.value,
            "blocking_dimensions": list(self.blocking_dimensions),
            "execution_authorized": False,
        }


def _software_failures(
    evidence: SoftwareVerificationEvidence,
    *,
    candidate_commit: str,
    evaluated_at: datetime,
    verifier: ReleaseEvidenceVerifier,
) -> tuple[tuple[str, ...], bool]:
    authenticated = verifier.verifies_software(evidence)
    if not authenticated:
        return ("SOFTWARE_EVIDENCE_AUTHENTICATION_FAILED",), False
    failures: list[str] = []
    if evidence.commit != candidate_commit:
        failures.append("SOFTWARE_COMMIT_MISMATCH")
    if not (
        evidence.started_at
        <= evidence.finished_at
        <= evaluated_at
        <= evidence.expires_at
    ):
        failures.append("SOFTWARE_EVIDENCE_STALE")
    if evidence.required_steps != _REQUIRED_SOFTWARE_STEPS:
        failures.append("SOFTWARE_MANIFEST_MISMATCH")
    required = set(evidence.required_steps)
    if (
        set(evidence.passed_steps) != required
        or evidence.failed_steps
    ):
        failures.append("SOFTWARE_RUN_INCOMPLETE")
    return tuple(failures), True


def _operational_failures(
    evidence: OperationalReadinessEvidence,
    *,
    candidate_commit: str,
    evaluated_at: datetime,
    verifier: ReleaseEvidenceVerifier,
) -> tuple[tuple[str, ...], bool, bool | None]:
    authenticated = verifier.verifies_operational(evidence)
    if not authenticated:
        return (
            ("OPERATIONAL_EVIDENCE_AUTHENTICATION_FAILED",),
            False,
            None,
        )
    failures: list[str] = []
    if evidence.commit != candidate_commit:
        failures.append("OPERATIONAL_COMMIT_MISMATCH")
    evidence_current = (
        evidence.observed_at <= evaluated_at <= evidence.expires_at
    )
    if not evidence_current:
        failures.append("OPERATIONAL_EVIDENCE_STALE")
    paper_identity = (
        evidence_current
        and evidence.broker_provider == "alpaca"
        and evidence.broker_mode == "paper"
        and evidence.broker_endpoint == _OFFICIAL_ALPACA_PAPER_ENDPOINT
        and _DIGEST.fullmatch(evidence.broker_account_fingerprint) is not None
        and evidence.broker_authenticated_at <= evidence.observed_at
        and (
            evidence.observed_at - evidence.broker_authenticated_at
            <= _MAX_BROKER_AUTH_AGE
        )
    )
    if not paper_identity:
        failures.append("BROKER_PAPER_IDENTITY_UNPROVEN")
    if (
        evidence.local_orders_digest != evidence.broker_orders_digest
        or evidence.local_positions_digest
        != evidence.broker_positions_digest
    ):
        failures.append("BROKER_RECONCILIATION_FAILED")
    if evidence.tripped_breaker_scopes:
        failures.append("BREAKER_TRIPPED")
    heartbeat_age = (evaluated_at - evidence.heartbeat_at).total_seconds()
    if (
        evidence.heartbeat_source != "daemon"
        or not math.isfinite(heartbeat_age)
        or heartbeat_age < 0
        or heartbeat_age > evidence.heartbeat_stale_after_seconds
    ):
        failures.append("DAEMON_HEARTBEAT_STALE")
    if evidence.encryption_state != "complete":
        failures.append("ENCRYPTION_MIXED")
    return tuple(failures), True, True if paper_identity else None


def evaluate_release_status(
    *,
    software: SoftwareVerificationEvidence,
    operational: OperationalReadinessEvidence,
    candidate_commit: str,
    evaluated_at: datetime,
    verifier: ReleaseEvidenceVerifier,
) -> ReleaseStatus:
    """Classify signed software and operational evidence independently."""
    if not isinstance(software, SoftwareVerificationEvidence):
        raise TypeError("software evidence type is invalid")
    if not isinstance(operational, OperationalReadinessEvidence):
        raise TypeError("operational evidence type is invalid")
    if not isinstance(verifier, ReleaseEvidenceVerifier):
        raise TypeError("release evidence verifier is invalid")
    candidate_commit = _canonical_commit(
        candidate_commit,
        name="candidate_commit",
    )
    evaluated_at = _canonical_utc(evaluated_at, name="evaluated_at")

    software_failures, software_authenticated = _software_failures(
        software,
        candidate_commit=candidate_commit,
        evaluated_at=evaluated_at,
        verifier=verifier,
    )
    operational_failures, operational_authenticated, paper_only = (
        _operational_failures(
            operational,
            candidate_commit=candidate_commit,
            evaluated_at=evaluated_at,
            verifier=verifier,
        )
    )
    return ReleaseStatus(
        software=(
            SoftwareStatus.BLOCKED
            if software_failures
            else SoftwareStatus.VERIFIED
        ),
        operational=(
            OperationalStatus.BLOCKED
            if operational_failures
            else OperationalStatus.READY
        ),
        software_detail_codes=(
            software_failures
            if software_failures
            else ("SOFTWARE_VERIFIED",)
        ),
        operational_detail_codes=(
            operational_failures
            if operational_failures
            else ("PREFLIGHT_READY",)
        ),
        candidate_commit=candidate_commit,
        software_run_id=(
            software.run_id if software_authenticated else None
        ),
        operational_run_id=(
            operational.run_id if operational_authenticated else None
        ),
        evaluated_at=evaluated_at,
        paper_only=paper_only,
    )


def evaluate_combined_release_gate(
    status: ReleaseStatus,
) -> CombinedReleaseGate:
    """Combine status dimensions for publication, never order execution."""
    if not isinstance(status, ReleaseStatus):
        raise TypeError("release status type is invalid")
    blocking: list[str] = []
    if status.software is not SoftwareStatus.VERIFIED:
        blocking.append("software")
    if (
        status.operational is not OperationalStatus.READY
        or status.paper_only is not True
    ):
        blocking.append("operational")
    return CombinedReleaseGate(
        status=(
            CombinedReleaseGateStatus.BLOCKED
            if blocking
            else CombinedReleaseGateStatus.SATISFIED
        ),
        blocking_dimensions=tuple(blocking),
    )
