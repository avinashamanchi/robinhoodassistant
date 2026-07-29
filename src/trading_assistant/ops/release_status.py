"""Authenticated, fail-closed release status classification.

The public status objects in this module are display-only data. Publication
gates always accept the two raw signed receipts and reevaluate them against one
immutable trust policy. Nothing here authorizes an order, starts a service,
resets a breaker, or turns release evidence into trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


_OFFICIAL_ALPACA_PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
_SOFTWARE_DOMAIN = b"trading-assistant/release-software-evidence/v1"
_OPERATIONAL_DOMAIN = b"trading-assistant/release-operational-evidence/v1"
_RECONCILIATION_DOMAIN = b"trading-assistant/reconciliation-manifest/v1"
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SIGNATURE = re.compile(r"[0-9a-f]{128}")
_EVIDENCE_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_SCOPE = re.compile(r"[a-z][a-z0-9:_.-]{0,127}")
_MAX_SOFTWARE_TTL = timedelta(hours=24)
_MAX_OPERATIONAL_TTL = timedelta(minutes=5)
_MAX_BROKER_AUTH_AGE = timedelta(minutes=5)
_MAX_TRUSTED_HEARTBEAT_AGE_SECONDS = 3_600
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
    """Result derived only from trusted software-run evidence."""

    VERIFIED = "verified"
    BLOCKED = "blocked"


class OperationalStatus(StrEnum):
    """Result derived only from trusted operational evidence."""

    READY = "ready"
    BLOCKED = "blocked"


class CombinedReleaseGateStatus(StrEnum):
    """Publication gate derived from both independent dimensions."""

    SATISFIED = "satisfied"
    BLOCKED = "blocked"


class ReconciliationDomain(StrEnum):
    """The two broker-truth domains required by operational readiness."""

    ORDERS = "orders"
    POSITIONS = "positions"


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
        raise ValueError(f"{name} must be an exact Git object id")
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


def _canonical_reconciliation_records(
    values: tuple[str, ...],
    *,
    name: str,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be a tuple")
    canonical: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"{name} contains an invalid record identity")
        canonical.append(value)
    if len(set(canonical)) != len(canonical):
        raise ValueError(f"{name} contains duplicate record identities")
    return tuple(sorted(canonical))


def _encoded_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _reconciliation_digest(
    *,
    domain: ReconciliationDomain,
    generation: int,
    records: tuple[str, ...],
) -> str:
    payload = _encoded_payload(
        {
            "domain": domain.value,
            "generation": generation,
            "records": list(records),
        }
    )
    return hashlib.sha256(
        _RECONCILIATION_DOMAIN + b"\0" + payload
    ).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class ReconciliationEvidence:
    """Opaque typed summary built only from collector manifests."""

    domain: ReconciliationDomain
    generation: int
    local_count: int
    broker_count: int
    local_digest: str
    broker_digest: str

    @classmethod
    def collect(
        cls,
        *,
        domain: ReconciliationDomain,
        generation: int,
        local_records: tuple[str, ...],
        broker_records: tuple[str, ...],
    ) -> "ReconciliationEvidence":
        if type(domain) is not ReconciliationDomain:
            raise TypeError("reconciliation domain is invalid")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            raise ValueError("reconciliation generation must be positive")
        local = _canonical_reconciliation_records(
            local_records,
            name="local_records",
        )
        broker = _canonical_reconciliation_records(
            broker_records,
            name="broker_records",
        )
        evidence = object.__new__(cls)
        object.__setattr__(evidence, "domain", domain)
        object.__setattr__(evidence, "generation", generation)
        object.__setattr__(evidence, "local_count", len(local))
        object.__setattr__(evidence, "broker_count", len(broker))
        object.__setattr__(
            evidence,
            "local_digest",
            _reconciliation_digest(
                domain=domain,
                generation=generation,
                records=local,
            ),
        )
        object.__setattr__(
            evidence,
            "broker_digest",
            _reconciliation_digest(
                domain=domain,
                generation=generation,
                records=broker,
            ),
        )
        return evidence


def _validate_reconciliation_evidence(
    evidence: ReconciliationEvidence,
    *,
    expected_domain: ReconciliationDomain,
) -> None:
    if type(evidence) is not ReconciliationEvidence:
        raise ValueError("reconciliation evidence type is invalid")
    if evidence.domain is not expected_domain:
        raise ValueError("reconciliation evidence domain is invalid")
    if (
        isinstance(evidence.generation, bool)
        or not isinstance(evidence.generation, int)
        or evidence.generation <= 0
    ):
        raise ValueError("reconciliation generation is invalid")
    for name, value in (
        ("local_count", evidence.local_count),
        ("broker_count", evidence.broker_count),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ValueError(f"reconciliation {name} is invalid")
    _canonical_digest(
        evidence.local_digest,
        name="reconciliation local_digest",
    )
    _canonical_digest(
        evidence.broker_digest,
        name="reconciliation broker_digest",
    )


def _reconciliation_payload(
    evidence: ReconciliationEvidence,
) -> dict[str, object]:
    return {
        "domain": evidence.domain.value,
        "generation": evidence.generation,
        "local_count": evidence.local_count,
        "broker_count": evidence.broker_count,
        "local_digest": evidence.local_digest,
        "broker_digest": evidence.broker_digest,
    }


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
    orders_reconciliation: ReconciliationEvidence
    positions_reconciliation: ReconciliationEvidence
    tripped_breaker_scopes: tuple[str, ...]
    heartbeat_source: str
    heartbeat_at: datetime
    encryption_state: str
    signature: str


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
        "orders_reconciliation": _reconciliation_payload(
            evidence.orders_reconciliation
        ),
        "positions_reconciliation": _reconciliation_payload(
            evidence.positions_reconciliation
        ),
        "tripped_breaker_scopes": list(evidence.tripped_breaker_scopes),
        "heartbeat_source": evidence.heartbeat_source,
        "heartbeat_at": _utc_text(evidence.heartbeat_at),
        "encryption_state": evidence.encryption_state,
    }


def _validate_software_structure(
    evidence: SoftwareVerificationEvidence,
) -> None:
    if type(evidence) is not SoftwareVerificationEvidence:
        raise ValueError("software evidence type is invalid")
    if type(evidence.version) is not int or evidence.version != 1:
        raise ValueError("software evidence version is unsupported")
    _canonical_run_id(evidence.run_id)
    _canonical_commit(evidence.commit)
    started_at = _canonical_utc(evidence.started_at, name="started_at")
    finished_at = _canonical_utc(evidence.finished_at, name="finished_at")
    expires_at = _canonical_utc(evidence.expires_at, name="expires_at")
    if (
        started_at > finished_at
        or finished_at >= expires_at
        or expires_at - finished_at > _MAX_SOFTWARE_TTL
    ):
        raise ValueError("software evidence timestamps are invalid")
    required_steps = _canonical_names(
        evidence.required_steps,
        name="required_steps",
        pattern=_EVIDENCE_NAME,
        allow_empty=False,
    )
    passed_steps = _canonical_names(
        evidence.passed_steps,
        name="passed_steps",
        pattern=_EVIDENCE_NAME,
        allow_empty=True,
    )
    failed_steps = _canonical_names(
        evidence.failed_steps,
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


def _validate_operational_structure(
    evidence: OperationalReadinessEvidence,
) -> None:
    if type(evidence) is not OperationalReadinessEvidence:
        raise ValueError("operational evidence type is invalid")
    if type(evidence.version) is not int or evidence.version != 1:
        raise ValueError("operational evidence version is unsupported")
    _canonical_run_id(evidence.run_id)
    _canonical_commit(evidence.commit)
    observed_at = _canonical_utc(evidence.observed_at, name="observed_at")
    expires_at = _canonical_utc(evidence.expires_at, name="expires_at")
    broker_authenticated_at = _canonical_utc(
        evidence.broker_authenticated_at,
        name="broker_authenticated_at",
    )
    heartbeat_at = _canonical_utc(
        evidence.heartbeat_at,
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
        not isinstance(evidence.broker_provider, str)
        or _EVIDENCE_NAME.fullmatch(evidence.broker_provider) is None
        or not isinstance(evidence.broker_mode, str)
        or _EVIDENCE_NAME.fullmatch(evidence.broker_mode) is None
        or not isinstance(evidence.heartbeat_source, str)
        or _EVIDENCE_NAME.fullmatch(evidence.heartbeat_source) is None
        or not isinstance(evidence.encryption_state, str)
        or _EVIDENCE_NAME.fullmatch(evidence.encryption_state) is None
    ):
        raise ValueError("operational evidence labels are invalid")
    _canonical_endpoint(evidence.broker_endpoint)
    _canonical_digest(
        evidence.broker_account_fingerprint,
        name="broker_account_fingerprint",
    )
    _validate_reconciliation_evidence(
        evidence.orders_reconciliation,
        expected_domain=ReconciliationDomain.ORDERS,
    )
    _validate_reconciliation_evidence(
        evidence.positions_reconciliation,
        expected_domain=ReconciliationDomain.POSITIONS,
    )
    _canonical_names(
        evidence.tripped_breaker_scopes,
        name="tripped_breaker_scopes",
        pattern=_SCOPE,
        allow_empty=True,
    )


def _sign(
    private_key: Ed25519PrivateKey,
    *,
    domain: bytes,
    payload: dict[str, object],
) -> str:
    return private_key.sign(
        domain + b"\0" + _encoded_payload(payload),
    ).hex()


def _verify(
    public_key: Ed25519PublicKey,
    *,
    domain: bytes,
    payload: dict[str, object],
    signature: object,
) -> bool:
    if (
        not isinstance(signature, str)
        or _SIGNATURE.fullmatch(signature) is None
    ):
        return False
    try:
        public_key.verify(
            bytes.fromhex(signature),
            domain + b"\0" + _encoded_payload(payload),
        )
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


@dataclass(frozen=True, slots=True, repr=False)
class SoftwareEvidenceSigner:
    """Private capability that can issue software evidence only."""

    _private_key: Ed25519PrivateKey

    def __post_init__(self) -> None:
        if not isinstance(self._private_key, Ed25519PrivateKey):
            raise TypeError("software evidence private key is invalid")

    @classmethod
    def from_private_bytes(cls, key: bytes) -> "SoftwareEvidenceSigner":
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("software evidence private key must be 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(key))

    def __repr__(self) -> str:
        return "SoftwareEvidenceSigner(<redacted>)"

    def verifier(self) -> "SoftwareEvidenceVerifier":
        return SoftwareEvidenceVerifier(self._private_key.public_key())

    def authenticate(
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
        unsigned = SoftwareVerificationEvidence(
            version=1,
            run_id=_canonical_run_id(run_id),
            commit=_canonical_commit(commit),
            started_at=_canonical_utc(started_at, name="started_at"),
            finished_at=_canonical_utc(finished_at, name="finished_at"),
            expires_at=_canonical_utc(expires_at, name="expires_at"),
            required_steps=required_steps,
            passed_steps=passed_steps,
            failed_steps=failed_steps,
            signature="",
        )
        _validate_software_structure(unsigned)
        return replace(
            unsigned,
            signature=_sign(
                self._private_key,
                domain=_SOFTWARE_DOMAIN,
                payload=_software_payload(unsigned),
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class OperationalEvidenceSigner:
    """Private capability that can issue operational evidence only."""

    _private_key: Ed25519PrivateKey

    def __post_init__(self) -> None:
        if not isinstance(self._private_key, Ed25519PrivateKey):
            raise TypeError("operational evidence private key is invalid")

    @classmethod
    def from_private_bytes(
        cls,
        key: bytes,
    ) -> "OperationalEvidenceSigner":
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("operational evidence private key must be 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(key))

    def __repr__(self) -> str:
        return "OperationalEvidenceSigner(<redacted>)"

    def verifier(self) -> "OperationalEvidenceVerifier":
        return OperationalEvidenceVerifier(self._private_key.public_key())

    def authenticate(
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
        orders_reconciliation: ReconciliationEvidence,
        positions_reconciliation: ReconciliationEvidence,
        tripped_breaker_scopes: tuple[str, ...],
        heartbeat_source: str,
        heartbeat_at: datetime,
        encryption_state: str,
    ) -> OperationalReadinessEvidence:
        unsigned = OperationalReadinessEvidence(
            version=1,
            run_id=_canonical_run_id(run_id),
            commit=_canonical_commit(commit),
            observed_at=_canonical_utc(observed_at, name="observed_at"),
            expires_at=_canonical_utc(expires_at, name="expires_at"),
            broker_provider=broker_provider,
            broker_mode=broker_mode,
            broker_endpoint=_canonical_endpoint(broker_endpoint),
            broker_account_fingerprint=_canonical_digest(
                broker_account_fingerprint,
                name="broker_account_fingerprint",
            ),
            broker_authenticated_at=_canonical_utc(
                broker_authenticated_at,
                name="broker_authenticated_at",
            ),
            orders_reconciliation=orders_reconciliation,
            positions_reconciliation=positions_reconciliation,
            tripped_breaker_scopes=tripped_breaker_scopes,
            heartbeat_source=heartbeat_source,
            heartbeat_at=_canonical_utc(
                heartbeat_at,
                name="heartbeat_at",
            ),
            encryption_state=encryption_state,
            signature="",
        )
        _validate_operational_structure(unsigned)
        return replace(
            unsigned,
            signature=_sign(
                self._private_key,
                domain=_OPERATIONAL_DOMAIN,
                payload=_operational_payload(unsigned),
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class SoftwareEvidenceVerifier:
    """Public capability that authenticates software receipts only."""

    _public_key: Ed25519PublicKey

    def __post_init__(self) -> None:
        if not isinstance(self._public_key, Ed25519PublicKey):
            raise TypeError("software evidence public key is invalid")

    @classmethod
    def from_public_bytes(cls, key: bytes) -> "SoftwareEvidenceVerifier":
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("software evidence public key must be 32 bytes")
        return cls(Ed25519PublicKey.from_public_bytes(key))

    def __repr__(self) -> str:
        return "SoftwareEvidenceVerifier(<public-key>)"

    def _public_bytes(self) -> bytes:
        return self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    def verifies(self, evidence: SoftwareVerificationEvidence) -> bool:
        try:
            _validate_software_structure(evidence)
            payload = _software_payload(evidence)
        except (AttributeError, TypeError, ValueError):
            return False
        return _verify(
            self._public_key,
            domain=_SOFTWARE_DOMAIN,
            payload=payload,
            signature=evidence.signature,
        )


@dataclass(frozen=True, slots=True, repr=False)
class OperationalEvidenceVerifier:
    """Public capability that authenticates operational receipts only."""

    _public_key: Ed25519PublicKey

    def __post_init__(self) -> None:
        if not isinstance(self._public_key, Ed25519PublicKey):
            raise TypeError("operational evidence public key is invalid")

    @classmethod
    def from_public_bytes(
        cls,
        key: bytes,
    ) -> "OperationalEvidenceVerifier":
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("operational evidence public key must be 32 bytes")
        return cls(Ed25519PublicKey.from_public_bytes(key))

    def __repr__(self) -> str:
        return "OperationalEvidenceVerifier(<public-key>)"

    def _public_bytes(self) -> bytes:
        return self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    def verifies(self, evidence: OperationalReadinessEvidence) -> bool:
        try:
            _validate_operational_structure(evidence)
            payload = _operational_payload(evidence)
        except (AttributeError, TypeError, ValueError):
            return False
        return _verify(
            self._public_key,
            domain=_OPERATIONAL_DOMAIN,
            payload=payload,
            signature=evidence.signature,
        )


class TrustedNowProvider(Protocol):
    """Trusted clock boundary used by every classification and gate call."""

    def now(self) -> datetime: ...


class RepositoryHeadResolver(Protocol):
    """Trusted repository boundary that returns the exact current HEAD."""

    def current_head(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ReleaseTrustPolicy:
    """Immutable trust roots and policy inputs for one release environment."""

    software_verifier: SoftwareEvidenceVerifier
    operational_verifier: OperationalEvidenceVerifier
    intended_account_fingerprint: str
    heartbeat_max_age_seconds: int
    now_provider: TrustedNowProvider
    repository_head_resolver: RepositoryHeadResolver

    def __post_init__(self) -> None:
        if type(self.software_verifier) is not SoftwareEvidenceVerifier:
            raise TypeError("trusted software verifier type is invalid")
        if type(self.operational_verifier) is not OperationalEvidenceVerifier:
            raise TypeError("trusted operational verifier type is invalid")
        if (
            self.software_verifier._public_bytes()
            == self.operational_verifier._public_bytes()
        ):
            raise ValueError("release evidence authorities must be distinct")
        _canonical_digest(
            self.intended_account_fingerprint,
            name="intended_account_fingerprint",
        )
        if (
            isinstance(self.heartbeat_max_age_seconds, bool)
            or not isinstance(self.heartbeat_max_age_seconds, int)
            or self.heartbeat_max_age_seconds <= 0
            or self.heartbeat_max_age_seconds
            > _MAX_TRUSTED_HEARTBEAT_AGE_SECONDS
        ):
            raise ValueError("trusted heartbeat max age is invalid")
        if not callable(getattr(self.now_provider, "now", None)):
            raise TypeError("trusted now provider is invalid")
        if not callable(
            getattr(
                self.repository_head_resolver,
                "current_head",
                None,
            )
        ):
            raise TypeError("repository HEAD resolver is invalid")


@dataclass(frozen=True, slots=True)
class ReleaseStatus:
    """Display-only independent classifications and evidence references."""

    software: SoftwareStatus
    operational: OperationalStatus
    software_detail_codes: tuple[str, ...]
    operational_detail_codes: tuple[str, ...]
    candidate_commit: str | None
    software_run_id: str | None
    operational_run_id: str | None
    evaluated_at: datetime | None
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
                "evaluated_at": (
                    _utc_text(self.evaluated_at)
                    if self.evaluated_at is not None
                    else None
                ),
            },
            "paper_only": self.paper_only,
            "execution_authorized": False,
        }


@dataclass(frozen=True, slots=True)
class CombinedReleaseGate:
    """Non-execution publication gate over freshly reevaluated receipts."""

    status: CombinedReleaseGateStatus
    blocking_dimensions: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "release_gate": self.status.value,
            "blocking_dimensions": list(self.blocking_dimensions),
            "execution_authorized": False,
        }


def _trusted_now(
    trust_policy: ReleaseTrustPolicy,
) -> tuple[datetime | None, str | None]:
    try:
        return (
            _canonical_utc(
                trust_policy.now_provider.now(),
                name="trusted_now",
            ),
            None,
        )
    except Exception:
        return None, "TRUSTED_TIME_UNAVAILABLE"


def _trusted_head(
    trust_policy: ReleaseTrustPolicy,
) -> tuple[str | None, str | None]:
    try:
        return (
            _canonical_commit(
                trust_policy.repository_head_resolver.current_head(),
                name="repository HEAD",
            ),
            None,
        )
    except Exception:
        return None, "REPOSITORY_HEAD_UNPROVEN"


def _software_failures(
    evidence: SoftwareVerificationEvidence,
    *,
    candidate_commit: str | None,
    head_error: str | None,
    evaluated_at: datetime | None,
    time_error: str | None,
    verifier: SoftwareEvidenceVerifier,
) -> tuple[tuple[str, ...], bool]:
    if type(evidence.version) is not int or evidence.version != 1:
        return ("SOFTWARE_EVIDENCE_VERSION_UNSUPPORTED",), False
    authenticated = verifier.verifies(evidence)
    if not authenticated:
        return ("SOFTWARE_EVIDENCE_AUTHENTICATION_FAILED",), False
    failures: list[str] = []
    if head_error is not None or candidate_commit is None:
        failures.append("REPOSITORY_HEAD_UNPROVEN")
    elif evidence.commit != candidate_commit:
        failures.append("SOFTWARE_COMMIT_MISMATCH")
    if time_error is not None or evaluated_at is None:
        failures.append("TRUSTED_TIME_UNAVAILABLE")
    elif not (
        evidence.started_at
        <= evidence.finished_at
        <= evaluated_at
        < evidence.expires_at
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
    candidate_commit: str | None,
    head_error: str | None,
    evaluated_at: datetime | None,
    time_error: str | None,
    trust_policy: ReleaseTrustPolicy,
) -> tuple[tuple[str, ...], bool, bool | None]:
    if type(evidence.version) is not int or evidence.version != 1:
        return (
            ("OPERATIONAL_EVIDENCE_VERSION_UNSUPPORTED",),
            False,
            None,
        )
    authenticated = trust_policy.operational_verifier.verifies(evidence)
    if not authenticated:
        return (
            ("OPERATIONAL_EVIDENCE_AUTHENTICATION_FAILED",),
            False,
            None,
        )
    failures: list[str] = []
    commit_current = head_error is None and candidate_commit is not None
    if not commit_current:
        failures.append("REPOSITORY_HEAD_UNPROVEN")
    elif evidence.commit != candidate_commit:
        failures.append("OPERATIONAL_COMMIT_MISMATCH")
        commit_current = False
    evidence_current = (
        time_error is None
        and evaluated_at is not None
        and evidence.observed_at <= evaluated_at < evidence.expires_at
    )
    if time_error is not None or evaluated_at is None:
        failures.append("TRUSTED_TIME_UNAVAILABLE")
    elif not evidence_current:
        failures.append("OPERATIONAL_EVIDENCE_STALE")
    official_paper_target = (
        evidence.broker_provider == "alpaca"
        and evidence.broker_mode == "paper"
        and evidence.broker_endpoint == _OFFICIAL_ALPACA_PAPER_ENDPOINT
    )
    if not official_paper_target:
        failures.append("BROKER_PAPER_IDENTITY_UNPROVEN")
    account_matches = (
        evidence.broker_account_fingerprint
        == trust_policy.intended_account_fingerprint
    )
    if not account_matches:
        failures.append("BROKER_ACCOUNT_MISMATCH")
    orders = evidence.orders_reconciliation
    positions = evidence.positions_reconciliation
    if orders.generation != positions.generation:
        failures.append("RECONCILIATION_GENERATION_MISMATCH")
    if (
        orders.local_count != orders.broker_count
        or positions.local_count != positions.broker_count
        or orders.local_digest != orders.broker_digest
        or positions.local_digest != positions.broker_digest
    ):
        failures.append("BROKER_RECONCILIATION_FAILED")
    if evidence.tripped_breaker_scopes:
        failures.append("BREAKER_TRIPPED")
    heartbeat_age = (
        (evaluated_at - evidence.heartbeat_at).total_seconds()
        if evaluated_at is not None
        else math.inf
    )
    if (
        evidence.heartbeat_source != "daemon"
        or not math.isfinite(heartbeat_age)
        or heartbeat_age < 0
        or heartbeat_age > trust_policy.heartbeat_max_age_seconds
    ):
        failures.append("DAEMON_HEARTBEAT_STALE")
    if evidence.encryption_state != "complete":
        failures.append("ENCRYPTION_MIXED")
    paper_only = (
        True
        if (
            commit_current
            and evidence_current
            and official_paper_target
            and account_matches
        )
        else None
    )
    return tuple(failures), True, paper_only


def evaluate_release_status(
    *,
    software: SoftwareVerificationEvidence,
    operational: OperationalReadinessEvidence,
    trust_policy: ReleaseTrustPolicy,
) -> ReleaseStatus:
    """Reverify and independently classify both receipts against trusted state."""
    if type(software) is not SoftwareVerificationEvidence:
        raise TypeError("software evidence type is invalid")
    if type(operational) is not OperationalReadinessEvidence:
        raise TypeError("operational evidence type is invalid")
    if type(trust_policy) is not ReleaseTrustPolicy:
        raise TypeError("release trust policy type is invalid")

    evaluated_at, time_error = _trusted_now(trust_policy)
    candidate_commit, head_error = _trusted_head(trust_policy)
    software_failures, software_authenticated = _software_failures(
        software,
        candidate_commit=candidate_commit,
        head_error=head_error,
        evaluated_at=evaluated_at,
        time_error=time_error,
        verifier=trust_policy.software_verifier,
    )
    operational_failures, operational_authenticated, paper_only = (
        _operational_failures(
            operational,
            candidate_commit=candidate_commit,
            head_error=head_error,
            evaluated_at=evaluated_at,
            time_error=time_error,
            trust_policy=trust_policy,
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
    *,
    software: SoftwareVerificationEvidence,
    operational: OperationalReadinessEvidence,
    trust_policy: ReleaseTrustPolicy,
) -> CombinedReleaseGate:
    """Reverify raw receipts and combine dimensions for publication only."""
    status = evaluate_release_status(
        software=software,
        operational=operational,
        trust_policy=trust_policy,
    )
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
