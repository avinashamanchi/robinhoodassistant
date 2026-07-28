"""Signed, session-bound candidate drafts and crash-safe queueing.

The model can draft candidates but cannot mutate trading state. Only the
explicit HTTP queue boundary consumes a signed nonce, refreshes complete broker
truth, runs deterministic risk, and persists a human-reviewable proposal or a
non-preapproved active standing rule. This module has no execution, approval,
submission, cancellation, or auto-execution path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Callable, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError

from trading_assistant.assets import AssetClass
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
)
from trading_assistant.db.models import (
    AuditEvent,
    CandidateNonce,
    CandidateQueueReceipt,
    Order,
    OrderStateMachine,
    Proposal,
    RiskEvent,
    Rule,
    RuleGroup,
    utcnow,
)
from trading_assistant.db.lifecycle_proofs import (
    augment_lifecycle_detail,
    latest_lifecycle_event,
    lifecycle_proof_matches,
    order_lifecycle_snapshot,
)
from trading_assistant.dependencies import RequiredDependencyUnavailable
from trading_assistant.identity import canonical_request_id
from trading_assistant.risk.breakers import trip_in_session
from trading_assistant.rules.models import RuleAction, RuleCommand, RuleState
from trading_assistant.security.secrets import (
    RuntimeSecrets,
    validate_base64_key,
)
from trading_assistant.security.sensitive_fields import (
    persist_sensitive,
)


_CANONICAL_DECIMAL = re.compile(
    r"(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{0,5}[1-9])?\Z"
)
_TICKER = re.compile(r"[A-Z][A-Z0-9./-]{0,14}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,64}\Z")
_UNPADDED_BASE64URL = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_MAX_ENVELOPE_TTL = timedelta(minutes=5)
_SIGNING_DOMAIN = b"trading-assistant/candidate-signing/v1"
_SESSION_DOMAIN = b"trading-assistant/candidate-session-binding/v1"
_METADATA_DOMAIN = b"trading-assistant/candidate-metadata/v1"


class CandidateError(RuntimeError):
    """Stable, non-sensitive candidate boundary failure."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int = 409,
    ) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _canonical_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("decimal must be a canonical positive string")
    if isinstance(value, Decimal):
        if not value.is_finite() or value <= 0:
            raise ValueError("decimal must be finite and positive")
        raw = format(value.normalize(), "f")
    elif isinstance(value, str):
        raw = value
    else:
        raise ValueError("decimal must be a canonical string or Decimal")
    if not _CANONICAL_DECIMAL.fullmatch(raw):
        raise ValueError("decimal must use canonical fixed-point form")
    parsed = Decimal(raw)
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("decimal must be finite and positive")
    return parsed


CandidateDecimal = Annotated[
    Decimal,
    BeforeValidator(_canonical_decimal),
    Field(gt=0, max_digits=20, decimal_places=6),
]


def _aware_utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("timestamp must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return value.astimezone(timezone.utc)


def _decode_unpadded(value: str) -> bytes:
    if not isinstance(value, str) or not _UNPADDED_BASE64URL.fullmatch(value):
        raise ValueError("value must be canonical unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, binascii.Error):
        raise ValueError("value must be canonical unpadded base64url") from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != 32 or not hmac.compare_digest(value, canonical):
        raise ValueError("value must be canonical unpadded base64url")
    return decoded


def _encode_unpadded(value: bytes) -> str:
    if len(value) != 32:
        raise ValueError("base64url material must be exactly 32 bytes")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class OrderCandidate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )

    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9./-]{0,14}$")
    side: Literal["buy", "sell"]
    quantity: CandidateDecimal | None = None
    notional: CandidateDecimal | None = None
    order_type: Literal["market", "limit"]
    limit_price: CandidateDecimal | None = None
    reference_price: CandidateDecimal
    quote_as_of: datetime
    thesis: str = Field(min_length=1, max_length=2_000)

    @field_validator("quote_as_of")
    @classmethod
    def validate_quote_time(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def validate_order_shape(self) -> "OrderCandidate":
        if (self.quantity is None) == (self.notional is None):
            raise ValueError("exactly one of quantity or notional is required")
        if (self.order_type == "limit") != (self.limit_price is not None):
            raise ValueError(
                "limit_price must be present only for limit orders"
            )
        return self


class RuleConditionCandidate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )

    comparator: Literal["price_below", "price_above"]
    trigger_price: CandidateDecimal


class RuleActionCandidate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )

    side: Literal["buy", "sell"]
    quantity: CandidateDecimal | None = None
    notional: CandidateDecimal | None = None
    order_type: Literal["market", "limit"]
    limit_price: CandidateDecimal | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> "RuleActionCandidate":
        if (self.quantity is None) == (self.notional is None):
            raise ValueError("exactly one of quantity or notional is required")
        if (self.order_type == "limit") != (self.limit_price is not None):
            raise ValueError(
                "limit_price must be present only for limit orders"
            )
        return self


class RuleCandidate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        frozen=True,
    )

    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9./-]{0,14}$")
    condition: RuleConditionCandidate
    action: RuleActionCandidate
    reference_price: CandidateDecimal
    quote_as_of: datetime
    thesis: str = Field(min_length=1, max_length=2_000)

    @field_validator("quote_as_of")
    @classmethod
    def validate_quote_time(cls, value: datetime) -> datetime:
        return _aware_utc(value)


CandidatePayload = OrderCandidate | RuleCandidate


class SignedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    kind: Literal["order", "rule"]
    actor: str = Field(min_length=1, max_length=128)
    session_binding: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    payload: CandidatePayload
    signature: str

    @field_validator("actor")
    @classmethod
    def validate_actor(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("actor must be canonical")
        return value

    @field_validator("session_binding", "nonce", "signature")
    @classmethod
    def validate_base64url(cls, value: str) -> str:
        _decode_unpadded(value)
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def validate_envelope(self) -> "SignedCandidate":
        if self.kind == "order" and not isinstance(
            self.payload, OrderCandidate
        ):
            raise ValueError("order envelope requires an order payload")
        if self.kind == "rule" and not isinstance(
            self.payload, RuleCandidate
        ):
            raise ValueError("rule envelope requires a rule payload")
        ttl = self.expires_at - self.issued_at
        if ttl <= timedelta(0) or ttl > _MAX_ENVELOPE_TTL:
            raise ValueError("candidate TTL must be within five minutes")
        if self.payload.quote_as_of > self.issued_at:
            raise ValueError("candidate quote cannot postdate issuance")
        return self


class AgentReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reply: str = Field(max_length=20_000)
    candidates: tuple[SignedCandidate, ...] = Field(
        default_factory=tuple,
        max_length=4,
    )


class CandidateQueueResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["proposed", "rejected", "queued"]
    target_id: int
    executed: Literal[False] = False


def _json_value(value):
    if isinstance(value, Decimal):
        if not value.is_finite() or value <= 0:
            raise ValueError("noncanonical decimal")
        rendered = format(value.normalize(), "f")
        if not _CANONICAL_DECIMAL.fullmatch(rendered):
            raise ValueError("noncanonical decimal")
        return rendered
    if isinstance(value, datetime):
        return _aware_utc(value).isoformat(timespec="microseconds")
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical JSON value {type(value)!r}")


def _canonical_json(value) -> bytes:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _subkey(root: bytes, domain: bytes) -> bytes:
    return hmac.new(root, domain, hashlib.sha256).digest()


class CandidateSigner:
    """HMAC signer with independent signing/session/metadata subkeys."""

    def __init__(
        self,
        key: bytes,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("candidate signing key must be 32 bytes")
        self._signing_key = _subkey(key, _SIGNING_DOMAIN)
        self._session_key = _subkey(key, _SESSION_DOMAIN)
        self._metadata_key = _subkey(key, _METADATA_DOMAIN)
        self.now = now

    @classmethod
    def from_runtime_secrets(
        cls,
        secrets_value: RuntimeSecrets,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> "CandidateSigner":
        key = validate_base64_key(
            "candidate_signing_key",
            secrets_value.candidate_signing_key,
        )
        try:
            return cls(bytes(key), now=now)
        finally:
            for index in range(len(key)):
                key[index] = 0

    def session_binding(
        self,
        *,
        actor: str,
        session_id: int,
        authenticated_at: datetime,
    ) -> str:
        actor = actor.strip()
        if not actor or not isinstance(session_id, int) or session_id <= 0:
            raise ValueError("session identity is invalid")
        material = _canonical_json(
            {
                "actor": actor,
                "authentication_epoch": _aware_utc(authenticated_at),
                "session_id": session_id,
            }
        )
        return _encode_unpadded(
            hmac.new(self._session_key, material, hashlib.sha256).digest()
        )

    def metadata_hash(self, purpose: str, value: str) -> str:
        if not purpose or not isinstance(value, str) or not value:
            raise ValueError("metadata hash input is invalid")
        return hmac.new(
            self._metadata_key,
            purpose.encode("ascii") + b"\x00" + value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def nonce_hash(nonce: str) -> str:
        return hashlib.sha256(_decode_unpadded(nonce)).hexdigest()

    @staticmethod
    def candidate_hash(candidate: SignedCandidate) -> str:
        return hashlib.sha256(
            _canonical_json(candidate.model_dump(mode="python"))
        ).hexdigest()

    def issue(
        self,
        *,
        kind: Literal["order", "rule"],
        payload: CandidatePayload,
        actor: str,
        session_binding: str,
        issued_at: datetime | None = None,
        ttl: timedelta = _MAX_ENVELOPE_TTL,
    ) -> SignedCandidate:
        issued_at = _aware_utc(issued_at or self.now())
        if ttl <= timedelta(0) or ttl > _MAX_ENVELOPE_TTL:
            raise ValueError("candidate TTL must be within five minutes")
        _decode_unpadded(session_binding)
        unsigned = {
            "version": 1,
            "kind": kind,
            "actor": actor,
            "session_binding": session_binding,
            "issued_at": issued_at,
            "expires_at": issued_at + ttl,
            "nonce": _encode_unpadded(secrets.token_bytes(32)),
            "payload": payload,
        }
        signature = _encode_unpadded(
            hmac.new(
                self._signing_key,
                _canonical_json(unsigned),
                hashlib.sha256,
            ).digest()
        )
        return SignedCandidate.model_validate(
            {**unsigned, "signature": signature}
        )

    def verify(
        self,
        candidate: SignedCandidate,
        *,
        expected_kind: Literal["order", "rule"],
        actor: str,
        session_binding: str,
        now: datetime | None = None,
        max_quote_age_seconds: float = 60,
        validate_time: bool = True,
    ) -> SignedCandidate:
        candidate = SignedCandidate.model_validate(
            candidate.model_dump(mode="python")
        )
        unsigned = candidate.model_dump(
            mode="python",
            exclude={"signature"},
        )
        expected = _encode_unpadded(
            hmac.new(
                self._signing_key,
                _canonical_json(unsigned),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(candidate.signature, expected):
            raise CandidateError("candidate_signature_invalid")
        if candidate.kind != expected_kind:
            raise CandidateError("candidate_kind_mismatch")
        if not hmac.compare_digest(candidate.actor, actor):
            raise CandidateError("candidate_actor_mismatch", status_code=403)
        if not hmac.compare_digest(
            candidate.session_binding,
            session_binding,
        ):
            raise CandidateError("candidate_session_mismatch", status_code=403)
        if validate_time:
            observed = _aware_utc(now or self.now())
            if candidate.issued_at > observed:
                raise CandidateError("candidate_issued_in_future")
            if observed >= candidate.expires_at:
                raise CandidateError("candidate_expired")
            age = observed - candidate.payload.quote_as_of
            if (
                age < timedelta(0)
                or age.total_seconds() > float(max_quote_age_seconds)
            ):
                raise CandidateError("candidate_quote_stale")
        return candidate


class _DraftOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1, max_length=32)
    side: Literal["buy", "sell"]
    quantity: CandidateDecimal | None = None
    notional: CandidateDecimal | None = None
    order_type: Literal["market", "limit"]
    limit_price: CandidateDecimal | None = None
    thesis: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_shape(self):
        if (self.quantity is None) == (self.notional is None):
            raise ValueError("exactly one size form is required")
        if (self.order_type == "limit") != (self.limit_price is not None):
            raise ValueError("limit price shape is invalid")
        return self


class _DraftRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1, max_length=32)
    condition: RuleConditionCandidate
    action: RuleActionCandidate
    thesis: str = Field(min_length=1, max_length=2_000)


class CandidateDraftService:
    """Read-only draft tools. The only provider operation is a quote read."""

    def __init__(
        self,
        service,
        signer: CandidateSigner,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.service = service
        self.signer = signer
        self.now = now

    def _ticker_and_config(self, raw: str):
        ticker = raw.strip().upper()
        if not _TICKER.fullmatch(ticker):
            raise CandidateError(
                "candidate_symbol_invalid",
                status_code=422,
            )
        asset_class = AssetClass.for_symbol(ticker)
        risk_config = (
            self.service.config.crypto_risk or self.service.config.risk
            if asset_class is AssetClass.CRYPTO
            else self.service.config.risk
        )
        if ticker not in set(risk_config.ticker_allowlist):
            raise CandidateError(
                "candidate_symbol_denied",
                status_code=403,
            )
        return ticker, asset_class, risk_config

    def _quote(
        self,
        ticker: str,
        *,
        risk_config,
    ) -> tuple[Quote, datetime, datetime]:
        try:
            quote = self.service.broker.get_quote(ticker)
        except Exception:
            raise CandidateError(
                "candidate_dependency_unavailable",
                status_code=503,
            ) from None
        if (
            not isinstance(quote, Quote)
            or quote.ticker.upper() != ticker
            or not quote.is_valid
        ):
            raise CandidateError(
                "candidate_quote_invalid",
                status_code=503,
            )
        times = (quote.book_as_of, quote.trade_as_of)
        if any(
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
            for value in times
        ):
            raise CandidateError(
                "candidate_quote_invalid",
                status_code=503,
            )
        issued_at = _aware_utc(self.now())
        quote_as_of = min(_aware_utc(value) for value in times)
        age = issued_at - quote_as_of
        if (
            age < timedelta(0)
            or age.total_seconds() > risk_config.max_quote_age_seconds
        ):
            raise CandidateError(
                "candidate_quote_stale",
                status_code=503,
            )
        return quote, quote_as_of, issued_at

    @staticmethod
    def _request(
        payload: OrderCandidate | RuleCandidate,
        idempotency_key: str,
    ) -> OrderRequest:
        action = (
            payload.action if isinstance(payload, RuleCandidate) else payload
        )
        return OrderRequest(
            ticker=payload.ticker,
            side=OrderSide(action.side),
            order_type=OrderType(action.order_type),
            idempotency_key=idempotency_key,
            qty=action.quantity,
            notional=action.notional,
            limit_price=action.limit_price,
        )

    @staticmethod
    def _static_cap(
        payload: OrderCandidate | RuleCandidate,
        quote: Quote,
        risk_config,
    ) -> None:
        request = CandidateDraftService._request(payload, "draft-static-cap")
        if request.risk_notional(quote) > Decimal(
            str(risk_config.max_notional_per_order)
        ):
            raise CandidateError(
                "candidate_static_cap_exceeded",
                status_code=403,
            )

    def draft_order(
        self,
        tool_input: dict,
        *,
        actor: str,
        session_binding: str,
    ) -> SignedCandidate:
        draft = _DraftOrderInput.model_validate(tool_input)
        ticker, _asset_class, risk_config = self._ticker_and_config(
            draft.ticker
        )
        quote, quote_as_of, issued_at = self._quote(
            ticker,
            risk_config=risk_config,
        )
        payload = OrderCandidate.model_validate(
            {
                **draft.model_dump(mode="python", exclude={"ticker"}),
                "ticker": ticker,
                "reference_price": quote.last,
                "quote_as_of": quote_as_of,
            }
        )
        self._static_cap(payload, quote, risk_config)
        return self.signer.issue(
            kind="order",
            payload=payload,
            actor=actor,
            session_binding=session_binding,
            issued_at=issued_at,
        )

    def draft_rule(
        self,
        tool_input: dict,
        *,
        actor: str,
        session_binding: str,
    ) -> SignedCandidate:
        draft = _DraftRuleInput.model_validate(tool_input)
        ticker, _asset_class, risk_config = self._ticker_and_config(
            draft.ticker
        )
        quote, quote_as_of, issued_at = self._quote(
            ticker,
            risk_config=risk_config,
        )
        payload = RuleCandidate.model_validate(
            {
                **draft.model_dump(mode="python", exclude={"ticker"}),
                "ticker": ticker,
                "reference_price": quote.last,
                "quote_as_of": quote_as_of,
            }
        )
        self._static_cap(payload, quote, risk_config)
        return self.signer.issue(
            kind="rule",
            payload=payload,
            actor=actor,
            session_binding=session_binding,
            issued_at=issued_at,
        )


class CandidateNonceStore:
    """Atomic one-shot nonce storage for callers outside the queue receipt flow."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def consume_once(
        self,
        candidate: SignedCandidate,
        *,
        request_id: str,
        now: datetime | None = None,
    ) -> None:
        observed = _aware_utc(now or utcnow())
        nonce_hash = CandidateSigner.nonce_hash(candidate.nonce)
        try:
            with self.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                session.add(
                    CandidateNonce(
                        nonce_hash=nonce_hash,
                        actor=candidate.actor,
                        kind=candidate.kind,
                        expires_at=candidate.expires_at,
                        consumed_at=observed,
                        request_id=canonical_request_id(request_id),
                    )
                )
                session.commit()
        except IntegrityError:
            raise CandidateError("candidate_replayed") from None


class CandidateQueueService:
    """Crash-safe queue coordinator; never executes or pre-approves a rule."""

    def __init__(
        self,
        service,
        signer: CandidateSigner,
        *,
        now: Callable[[], datetime] = utcnow,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.service = service
        self.signer = signer
        self.now = now
        self.crash_hook = crash_hook
        self.submission_barrier = service.submission_barrier

    def _crash(self, stage: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(stage)

    def _identity(
        self,
        *,
        actor: str,
        session_binding: str,
        kind: str,
        idempotency_key: str,
        reason: str,
        candidate: SignedCandidate,
    ) -> dict[str, str]:
        return {
            "session_binding_hash": self.signer.metadata_hash(
                "session", session_binding
            ),
            "actor_hash": self.signer.metadata_hash("actor", actor),
            "kind": kind,
            "idempotency_key_hash": self.signer.metadata_hash(
                "idempotency", idempotency_key
            ),
            "candidate_hash": self.signer.candidate_hash(candidate),
            "reason_hash": self.signer.metadata_hash("reason", reason),
            "nonce_hash": self.signer.nonce_hash(candidate.nonce),
        }

    def _find_receipt(self, identity) -> CandidateQueueReceipt | None:
        with self.service.session_factory() as session:
            return session.scalar(
                select(CandidateQueueReceipt).where(
                    CandidateQueueReceipt.session_binding_hash
                    == identity["session_binding_hash"],
                    CandidateQueueReceipt.kind == identity["kind"],
                    CandidateQueueReceipt.idempotency_key_hash
                    == identity["idempotency_key_hash"],
                )
            )

    @staticmethod
    def _same_candidate(receipt, identity) -> bool:
        return (
            hmac.compare_digest(
                receipt.candidate_hash,
                identity["candidate_hash"],
            )
            and hmac.compare_digest(
                receipt.nonce_hash,
                identity["nonce_hash"],
            )
            and hmac.compare_digest(
                receipt.actor_hash,
                identity["actor_hash"],
            )
            and hmac.compare_digest(
                receipt.reason_hash,
                identity["reason_hash"],
            )
        )

    def _reserve(
        self,
        candidate: SignedCandidate,
        identity,
        *,
        request_id: str,
        now: datetime,
    ) -> tuple[CandidateQueueReceipt, bool]:
        with self.service.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            existing = session.scalar(
                select(CandidateQueueReceipt).where(
                    CandidateQueueReceipt.session_binding_hash
                    == identity["session_binding_hash"],
                    CandidateQueueReceipt.kind == identity["kind"],
                    CandidateQueueReceipt.idempotency_key_hash
                    == identity["idempotency_key_hash"],
                )
            )
            if existing is not None:
                if not self._same_candidate(existing, identity):
                    session.rollback()
                    raise CandidateError("idempotency_conflict")
                session.commit()
                return existing, False
            if (
                session.get(CandidateNonce, identity["nonce_hash"])
                is not None
            ):
                session.rollback()
                raise CandidateError("candidate_replayed")
            receipt = CandidateQueueReceipt(
                **identity,
                state="reserved",
                request_id=request_id,
                created_at=now,
                updated_at=now,
            )
            session.add(receipt)
            session.add(
                CandidateNonce(
                    nonce_hash=identity["nonce_hash"],
                    actor=candidate.actor,
                    kind=candidate.kind,
                    expires_at=candidate.expires_at,
                    consumed_at=now,
                    request_id=request_id,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise CandidateError("candidate_replayed") from None
            return receipt, True

    def _target_keys(
        self,
        identity,
    ) -> tuple[str, str]:
        material = (
            identity["nonce_hash"] + ":" + identity["candidate_hash"]
        )
        order_suffix = self.signer.metadata_hash(
            "order-target",
            material,
        )[:40]
        rule_suffix = self.signer.metadata_hash(
            "rule-target",
            material,
        )[:40]
        return (
            f"candidate-order-{order_suffix}",
            f"candidate-rule-{rule_suffix}",
        )

    def _result_from_receipt(
        self,
        receipt: CandidateQueueReceipt,
    ) -> CandidateQueueResult:
        if receipt.outcome_code in {"proposed", "rejected", "queued"}:
            if receipt.target_id is None or receipt.http_status != 201:
                raise CandidateError("candidate_receipt_inconsistent")
            return CandidateQueueResult(
                status=receipt.outcome_code,
                target_id=receipt.target_id,
            )
        if receipt.outcome_code:
            if receipt.http_status is None:
                raise CandidateError("candidate_receipt_inconsistent")
            raise CandidateError(
                receipt.outcome_code,
                status_code=receipt.http_status,
            )
        raise CandidateError("candidate_receipt_inconsistent")

    def _validate_target(
        self,
        session,
        receipt: CandidateQueueReceipt,
        identity,
        candidate: SignedCandidate,
        *,
        require_initial_lifecycle: bool,
    ) -> None:
        if receipt.target_id is None:
            raise CandidateError("candidate_receipt_inconsistent")
        order_key, group_key = self._target_keys(identity)
        if candidate.kind == "order":
            target = session.get(Order, receipt.target_id)
            expected = self._request_from_payload(
                candidate.payload,
                order_key,
            )
            try:
                initial_status = OrderStatus(receipt.outcome_code)
                current_status = (
                    OrderStatus(target.status)
                    if target is not None
                    else None
                )
            except ValueError:
                initial_status = None
                current_status = None
            if (
                target is None
                or target.idempotency_key != order_key
                or target.ticker != expected.ticker
                or target.side != expected.side.value
                or target.order_type != expected.order_type.value
                or target.qty != expected.qty
                or target.notional != expected.notional
                or target.limit_price != expected.limit_price
                or receipt.outcome_code not in {"proposed", "rejected"}
                or initial_status is None
                or current_status is None
                or not OrderStateMachine.is_reachable(
                    initial_status,
                    current_status,
                )
            ):
                raise CandidateError("candidate_receipt_inconsistent")
            if (
                not self._proposal_is_canonical(
                    session,
                    target,
                    receipt,
                )
                or not self._order_lifecycle_is_consistent(
                    session,
                    target,
                    initial_status,
                )
            ):
                raise CandidateError("candidate_receipt_inconsistent")
            return
        assert isinstance(candidate.payload, RuleCandidate)
        command = self._rule_command(candidate.payload, group_key)
        expected_payload = command.model_dump(mode="json")
        expected_condition = json.dumps(
            expected_payload["condition"],
            separators=(",", ":"),
            sort_keys=True,
        )
        expected_action = json.dumps(
            expected_payload["action"],
            separators=(",", ":"),
            sort_keys=True,
        )
        group = session.scalar(
            select(RuleGroup).where(RuleGroup.group_key == group_key)
        )
        rules = (
            list(
                session.scalars(
                    select(Rule)
                    .where(Rule.group_id == group.id)
                    .order_by(Rule.id)
                )
            )
            if group is not None
            else []
        )
        rule = rules[0] if len(rules) == 1 else None
        if (
            group is None
            or rule is None
            or rule.id != receipt.target_id
            or rule.payload_version != 1
            or rule.ticker != command.ticker
            or rule.kind != command.kind.value
            or rule.condition_json != expected_condition
            or rule.action_json != expected_action
            or rule.plan_id is not None
            or rule.fraction != command.fraction
            or rule.hwm != command.high_water_mark
            or rule.deadline is not None
            or rule.pre_approved != command.pre_approved
            or rule.activation != command.activation
            or (
                rule.terminal_on_trigger
                != command.terminal_on_trigger
            )
            or receipt.outcome_code != "queued"
            or not self._rule_lifecycle_is_consistent(
                session,
                group,
                rule,
                command,
                require_initial_lifecycle=require_initial_lifecycle,
            )
        ):
            raise CandidateError("candidate_receipt_inconsistent")

    def _proposal_is_canonical(
        self,
        session,
        order: Order,
        receipt: CandidateQueueReceipt,
    ) -> bool:
        proposals = list(
            session.scalars(
                select(Proposal).where(Proposal.order_id == order.id)
            )
        )
        if len(proposals) != 1:
            return False
        proposal = proposals[0]
        ttl_minutes = self._risk_config(order.ticker).proposal_ttl_minutes
        if (
            proposal.source_rule_group_id is not None
            or proposal.source_rule_id is not None
            or proposal.plan_generation != 0
            or proposal.ttl_minutes != ttl_minutes
            or proposal.created_at != order.created_at
            or proposal.expires_at
            != proposal.created_at + timedelta(minutes=ttl_minutes)
        ):
            return False
        origin = latest_lifecycle_event(
            session,
            self.service.session_factory,
            target_type="order",
            target_id=order.id,
            action="candidate.order.queue",
        )
        current = order_lifecycle_snapshot(session, order.id)
        if origin is None or current is None:
            return False
        event, detail = origin
        proof = detail.get("lifecycle_proof")
        reason_hash = detail.get("candidate_reason_hash")
        return bool(
            event.actor
            and hmac.compare_digest(
                self.signer.metadata_hash("actor", event.actor),
                receipt.actor_hash,
            )
            and event.request_id == receipt.request_id
            and event.result_code == receipt.outcome_code
            and isinstance(reason_hash, str)
            and hmac.compare_digest(reason_hash, receipt.reason_hash)
            and isinstance(proof, dict)
            and proof.get("target_type") == "order"
            and proof.get("target_id") == order.id
            and isinstance(proof.get("snapshot"), dict)
            and proof["snapshot"].get("proposals")
            == current.get("proposals")
        )

    def _order_lifecycle_is_consistent(
        self,
        session,
        order: Order,
        initial_status: OrderStatus,
    ) -> bool:
        try:
            current_status = OrderStatus(order.status)
        except ValueError:
            return False
        return bool(
            OrderStateMachine.is_reachable(
                initial_status,
                current_status,
            )
            and lifecycle_proof_matches(
                session,
                self.service.session_factory,
                target_type="order",
                target_id=order.id,
            )
        )

    def _linked_rule_order_is_canonical(
        self,
        session,
        group: RuleGroup,
        rule: Rule,
        command: RuleCommand,
        group_state: RuleState,
    ) -> bool:
        proposals = list(
            session.scalars(
                select(Proposal)
                .where(
                    or_(
                        Proposal.source_rule_group_id == group.id,
                        Proposal.source_rule_id == rule.id,
                    )
                )
                .order_by(Proposal.id)
            )
        )
        if len(proposals) != 1:
            return False
        proposal = proposals[0]
        order = session.get(Order, proposal.order_id)
        if order is None:
            return False
        expected = command.action
        ttl_minutes = self._risk_config(
            command.ticker
        ).proposal_ttl_minutes
        expected_initial = (
            OrderStatus.REJECTED
            if group_state is RuleState.FAILED
            else OrderStatus.PROPOSED
        )
        try:
            current_status = OrderStatus(order.status)
        except ValueError:
            return False
        origin = latest_lifecycle_event(
            session,
            self.service.session_factory,
            target_type="order",
            target_id=order.id,
            action="order.propose",
        )
        current = order_lifecycle_snapshot(session, order.id)
        if origin is None or current is None:
            return False
        _event, detail = origin
        proof = detail.get("lifecycle_proof")
        return bool(
            proposal.source_rule_group_id == group.id
            and proposal.source_rule_id == rule.id
            and proposal.plan_generation == 0
            and proposal.ttl_minutes == ttl_minutes
            and proposal.expires_at
            == proposal.created_at + timedelta(minutes=ttl_minutes)
            and order.idempotency_key
            == f"rule-group-{group.id}-rule-{rule.id}"
            and order.ticker == command.ticker
            and order.side == expected.side
            and order.order_type == expected.order_type
            and order.qty == expected.qty
            and order.notional == expected.notional
            and order.limit_price == expected.limit_price
            and OrderStateMachine.is_reachable(
                expected_initial,
                current_status,
            )
            and detail.get("source") == "conditional_rule"
            and detail.get("rule_id") == rule.id
            and detail.get("rule_group_id") == group.id
            and isinstance(proof, dict)
            and isinstance(proof.get("snapshot"), dict)
            and proof["snapshot"].get("proposals")
            == current.get("proposals")
            and lifecycle_proof_matches(
                session,
                self.service.session_factory,
                target_type="order",
                target_id=order.id,
            )
        )

    def _rule_lifecycle_is_consistent(
        self,
        session,
        group: RuleGroup,
        rule: Rule,
        command: RuleCommand,
        *,
        require_initial_lifecycle: bool,
    ) -> bool:
        del require_initial_lifecycle
        try:
            group_state = RuleState(group.state)
            rule_state = RuleState(rule.state)
        except ValueError:
            return False
        if (
            (group.lease_owner is None)
            != (group.lease_expires_at is None)
            or group.version < 0
        ):
            return False
        if group.lease_owner is not None:
            owner = group.lease_owner.strip()
            if (
                not owner
                or owner != group.lease_owner
                or group.lease_expires_at <= group.updated_at
            ):
                return False
        if group_state is RuleState.ACTIVE:
            state_shape = bool(
                rule_state is RuleState.ACTIVE
                and group.terminal_rule_id is None
                and not group.reconciliation_required
            )
        elif group_state in {RuleState.TRIGGERED, RuleState.FAILED}:
            state_shape = bool(
                rule_state is group_state
                and group.terminal_rule_id == rule.id
                and group.lease_owner is None
                and group.lease_expires_at is None
            )
        elif group_state is RuleState.CANCELED:
            state_shape = bool(
                rule_state is RuleState.CANCELED
                and group.terminal_rule_id is None
                and not group.reconciliation_required
                and group.lease_owner is None
                and group.lease_expires_at is None
            )
        else:
            state_shape = False
        if not state_shape:
            return False
        if not (
            lifecycle_proof_matches(
                session,
                self.service.session_factory,
                target_type="rule_group",
                target_id=group.id,
            )
            and lifecycle_proof_matches(
                session,
                self.service.session_factory,
                target_type="rule",
                target_id=rule.id,
            )
        ):
            return False
        if (
            group_state in {RuleState.TRIGGERED, RuleState.FAILED}
            or group.reconciliation_required
        ):
            return self._linked_rule_order_is_canonical(
                session,
                group,
                rule,
                command,
                group_state,
            )
        return True

    def _complete(
        self,
        receipt_id: int,
        identity,
        candidate: SignedCandidate,
    ) -> CandidateQueueResult:
        with self.service.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            receipt = session.get(CandidateQueueReceipt, receipt_id)
            if receipt is None:
                session.rollback()
                raise CandidateError("candidate_receipt_missing")
            if receipt.state == "completed":
                if receipt.target_id is not None:
                    self._validate_target(
                        session,
                        receipt,
                        identity,
                        candidate,
                        require_initial_lifecycle=False,
                    )
                session.commit()
                return self._result_from_receipt(receipt)
            if (
                receipt.state != "target_persisted"
                or receipt.target_id is None
                or not receipt.outcome_code
                or receipt.http_status != 201
            ):
                session.rollback()
                raise CandidateError("candidate_receipt_inconsistent")
            self._validate_target(
                session,
                receipt,
                identity,
                candidate,
                require_initial_lifecycle=True,
            )
            receipt.state = "completed"
            receipt.completed_at = _aware_utc(self.now())
            receipt.updated_at = receipt.completed_at
            session.commit()
            return self._result_from_receipt(receipt)

    def _complete_terminal(
        self,
        receipt_id: int,
        code: str,
        *,
        status_code: int,
    ) -> None:
        with self.service.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            receipt = session.get(CandidateQueueReceipt, receipt_id)
            if receipt is None:
                session.rollback()
                raise CandidateError("candidate_receipt_missing")
            if receipt.state == "completed":
                session.commit()
                self._result_from_receipt(receipt)
                return
            if receipt.state != "reserved":
                session.rollback()
                raise CandidateError("candidate_receipt_inconsistent")
            observed = _aware_utc(self.now())
            receipt.state = "completed"
            receipt.outcome_code = code
            receipt.http_status = status_code
            receipt.completed_at = observed
            receipt.updated_at = observed
            session.commit()
        raise CandidateError(code, status_code=status_code)

    def _recover_target(
        self,
        receipt: CandidateQueueReceipt,
        identity,
        candidate: SignedCandidate,
    ) -> CandidateQueueResult | None:
        if receipt.state == "completed":
            if receipt.target_id is None:
                return self._result_from_receipt(receipt)
            return self._complete(
                receipt.id,
                identity,
                candidate,
            )
        if receipt.state == "target_persisted":
            return self._complete(
                receipt.id,
                identity,
                candidate,
            )
        if receipt.state == "reserved":
            return None
        raise CandidateError("candidate_receipt_inconsistent")

    @staticmethod
    def _request_from_payload(
        payload: CandidatePayload,
        idempotency_key: str,
    ) -> OrderRequest:
        action = (
            payload.action if isinstance(payload, RuleCandidate) else payload
        )
        return OrderRequest(
            ticker=payload.ticker,
            side=OrderSide(action.side),
            order_type=OrderType(action.order_type),
            idempotency_key=idempotency_key,
            qty=action.quantity,
            notional=action.notional,
            limit_price=action.limit_price,
        )

    def _risk_config(self, ticker: str):
        asset_class = AssetClass.for_symbol(ticker)
        return (
            self.service.config.crypto_risk or self.service.config.risk
            if asset_class is AssetClass.CRYPTO
            else self.service.config.risk
        )

    def _validate_static(self, payload: CandidatePayload) -> None:
        config = self._risk_config(payload.ticker)
        if payload.ticker not in set(config.ticker_allowlist):
            raise CandidateError(
                "candidate_symbol_denied",
                status_code=403,
            )
        request = self._request_from_payload(payload, "queue-static")
        if request.estimated_notional(payload.reference_price) > Decimal(
            str(config.max_notional_per_order)
        ):
            raise CandidateError(
                "candidate_static_cap_exceeded",
                status_code=403,
            )

    def _persist_order(
        self,
        receipt_id: int,
        identity,
        request: OrderRequest,
        risk,
        *,
        actor: str,
        reason: str,
        request_id: str,
        now: datetime,
    ) -> None:
        with self.service.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            receipt = session.get(CandidateQueueReceipt, receipt_id)
            if receipt is None or receipt.state != "reserved":
                session.rollback()
                raise CandidateError("candidate_receipt_inconsistent")
            existing = session.scalar(
                select(Order).where(
                    Order.idempotency_key == request.idempotency_key
                )
            )
            if existing is not None:
                session.rollback()
                raise CandidateError("candidate_target_conflict")
            order = Order(
                idempotency_key=request.idempotency_key,
                ticker=request.ticker,
                side=request.side.value,
                order_type=request.order_type.value,
                qty=request.qty,
                notional=request.notional,
                limit_price=request.limit_price,
                status=(
                    OrderStatus.REJECTED.value
                    if risk.rejected
                    else OrderStatus.PROPOSED.value
                ),
                created_at=now,
                updated_at=now,
            )
            persist_sensitive(
                session,
                order,
                {"approval_reason": "approval pending"},
            )
            session.flush()
            config = self._risk_config(request.ticker)
            ttl = config.proposal_ttl_minutes
            persist_sensitive(
                session,
                Proposal(
                    order_id=order.id,
                    ttl_minutes=ttl,
                    created_at=now,
                    expires_at=now + timedelta(minutes=ttl),
                ),
                {"reasoning": reason},
            )
            for risk_reason in risk.reasons:
                persist_sensitive(
                    session,
                    RiskEvent(
                        order_id=order.id,
                        event_type="rejection",
                    ),
                    {"reason": risk_reason},
                )
            for warning in risk.warnings:
                persist_sensitive(
                    session,
                    RiskEvent(
                        order_id=order.id,
                        event_type="warning",
                    ),
                    {"reason": warning},
                )
            for intent in risk.breaker_trips:
                trip_in_session(
                    session,
                    intent.scope,
                    intent.reason,
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=reason,
                )
            persist_sensitive(
                session,
                AuditEvent(
                    actor=actor,
                    action="candidate.order.queue",
                    target_type="order",
                    target_id=str(order.id),
                    request_id=request_id,
                    result_code=order.status,
                    created_at=now,
                ),
                {
                    "reason": reason,
                    "detail_json": json.dumps(
                        augment_lifecycle_detail(
                            session,
                            target_type="order",
                            target_id=order.id,
                            detail={
                                "candidate_reason_hash": (
                                    receipt.reason_hash
                                ),
                            },
                        ),
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            )
            receipt.state = "completed"
            receipt.target_id = order.id
            receipt.outcome_code = order.status
            receipt.http_status = 201
            receipt.completed_at = now
            receipt.updated_at = now
            self._crash("before_target_commit")
            session.commit()

    def _persist_rule(
        self,
        receipt_id: int,
        identity,
        payload: RuleCandidate,
        risk,
        *,
        actor: str,
        reason: str,
        request_id: str,
        now: datetime,
    ) -> None:
        _order_key, group_key = self._target_keys(identity)
        command = self._rule_command(payload, group_key)
        with self.service.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            receipt = session.get(CandidateQueueReceipt, receipt_id)
            if receipt is None or receipt.state != "reserved":
                session.rollback()
                raise CandidateError("candidate_receipt_inconsistent")
            if session.scalar(
                select(RuleGroup).where(
                    RuleGroup.group_key == group_key
                )
            ) is not None:
                session.rollback()
                raise CandidateError("candidate_target_conflict")
            rule = self.service.rule_application.persist_commands(
                session,
                [command],
                actor=actor,
                reason=reason,
                request_id=request_id,
            )[0]
            for warning in risk.warnings:
                persist_sensitive(
                    session,
                    RiskEvent(
                        order_id=None,
                        event_type="warning",
                    ),
                    {"reason": warning},
                )
            for intent in risk.breaker_trips:
                trip_in_session(
                    session,
                    intent.scope,
                    intent.reason,
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=reason,
                )
            persist_sensitive(
                session,
                AuditEvent(
                    actor=actor,
                    action="candidate.rule.queue",
                    target_type="rule",
                    target_id=str(rule.id),
                    request_id=request_id,
                    result_code="pending",
                    created_at=now,
                ),
                {
                    "reason": reason,
                    "detail_json": json.dumps(
                        augment_lifecycle_detail(
                            session,
                            target_type="rule",
                            target_id=rule.id,
                            detail={
                                "candidate_reason_hash": (
                                    receipt.reason_hash
                                ),
                            },
                        ),
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            )
            receipt.state = "completed"
            receipt.target_id = rule.id
            receipt.outcome_code = "queued"
            receipt.http_status = 201
            receipt.completed_at = now
            receipt.updated_at = now
            self._crash("before_target_commit")
            session.commit()

    @staticmethod
    def _rule_command(
        payload: RuleCandidate,
        group_key: str,
    ) -> RuleCommand:
        direction = (
            "below"
            if payload.condition.comparator == "price_below"
            else "above"
        )
        action = RuleAction.model_validate(
            {
                "side": payload.action.side,
                "order_type": payload.action.order_type,
                "qty": payload.action.quantity,
                "notional": payload.action.notional,
                "limit_price": payload.action.limit_price,
            }
        )
        command = RuleCommand.model_validate(
            {
                "ticker": payload.ticker,
                "kind": "price",
                "condition": {
                    "type": "price",
                    "direction": direction,
                    "price": payload.condition.trigger_price,
                },
                "action": action,
                "group_key": group_key,
                "pre_approved": False,
                "activation": "immediate",
            }
        )
        return command

    def _reject_rule_risk(
        self,
        receipt_id: int,
        risk,
        *,
        actor: str,
        reason: str,
        request_id: str,
        now: datetime,
    ) -> None:
        with self.service.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            receipt = session.get(CandidateQueueReceipt, receipt_id)
            if receipt is None or receipt.state != "reserved":
                session.rollback()
                raise CandidateError("candidate_receipt_inconsistent")
            for risk_reason in risk.reasons:
                persist_sensitive(
                    session,
                    RiskEvent(
                        order_id=None,
                        event_type="rejection",
                    ),
                    {"reason": risk_reason},
                )
            for warning in risk.warnings:
                persist_sensitive(
                    session,
                    RiskEvent(
                        order_id=None,
                        event_type="warning",
                    ),
                    {"reason": warning},
                )
            for intent in risk.breaker_trips:
                trip_in_session(
                    session,
                    intent.scope,
                    intent.reason,
                    actor,
                    request_id=request_id,
                    now=now,
                    audit_reason=reason,
                )
            persist_sensitive(
                session,
                AuditEvent(
                    actor=actor,
                    action="candidate.rule.queue",
                    target_type="candidate_receipt",
                    target_id=str(receipt.id),
                    request_id=request_id,
                    result_code="rejected",
                    created_at=now,
                ),
                {"reason": reason, "detail_json": "{}"},
            )
            receipt.state = "completed"
            receipt.outcome_code = "candidate_risk_rejected"
            receipt.http_status = 403
            receipt.completed_at = now
            receipt.updated_at = now
            session.commit()
        raise CandidateError(
            "candidate_risk_rejected",
            status_code=403,
        )

    def queue(
        self,
        candidate: SignedCandidate,
        *,
        expected_kind: Literal["order", "rule"],
        actor: str,
        session_binding: str,
        idempotency_key: str,
        reason: str,
        request_id: str,
    ) -> CandidateQueueResult:
        actor = actor.strip()
        reason = reason.strip()
        request_id = canonical_request_id(request_id)
        if not actor or not reason:
            raise CandidateError(
                "candidate_reason_required",
                status_code=422,
            )
        if (
            not isinstance(idempotency_key, str)
            or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key)
        ):
            raise CandidateError(
                "candidate_idempotency_invalid",
                status_code=422,
            )
        observed = _aware_utc(self.now())
        candidate = self.signer.verify(
            candidate,
            expected_kind=expected_kind,
            actor=actor,
            session_binding=session_binding,
            now=observed,
            validate_time=False,
        )
        identity = self._identity(
            actor=actor,
            session_binding=session_binding,
            kind=expected_kind,
            idempotency_key=idempotency_key,
            reason=reason,
            candidate=candidate,
        )
        existing = self._find_receipt(identity)
        if existing is not None:
            if not self._same_candidate(existing, identity):
                raise CandidateError("idempotency_conflict")
            recovered = self._recover_target(
                existing,
                identity,
                candidate,
            )
            if recovered is not None:
                return recovered

        config = self._risk_config(candidate.payload.ticker)
        if existing is None:
            self.signer.verify(
                candidate,
                expected_kind=expected_kind,
                actor=actor,
                session_binding=session_binding,
                now=observed,
                max_quote_age_seconds=config.max_quote_age_seconds,
            )
            self._validate_static(candidate.payload)
            receipt, created = self._reserve(
                candidate,
                identity,
                request_id=request_id,
                now=observed,
            )
        else:
            receipt = existing
            created = False
            try:
                self.signer.verify(
                    candidate,
                    expected_kind=expected_kind,
                    actor=actor,
                    session_binding=session_binding,
                    now=observed,
                    max_quote_age_seconds=config.max_quote_age_seconds,
                )
                self._validate_static(candidate.payload)
            except CandidateError as exc:
                self._complete_terminal(
                    receipt.id,
                    exc.code,
                    status_code=exc.status_code,
                )
        recovered = self._recover_target(
            receipt,
            identity,
            candidate,
        )
        if recovered is not None:
            return recovered
        if created:
            self._crash("after_receipt_reserve")
        original_request_id = receipt.request_id

        order_key, _group_key = self._target_keys(identity)
        request = self._request_from_payload(
            candidate.payload,
            order_key,
        )
        try:
            with self.submission_barrier.hold_writer():
                current = self._find_receipt(identity)
                if current is None:
                    raise CandidateError("candidate_receipt_missing")
                recovered = self._recover_target(
                    current,
                    identity,
                    candidate,
                )
                if recovered is not None:
                    return recovered
                snapshot = (
                    self.service.snapshot_service
                    .assemble_for_confirmation(request.ticker)
                )
                risk = self.service._risk_for(
                    AssetClass.for_symbol(request.ticker)
                ).check(request, snapshot)
                if expected_kind == "rule" and risk.rejected:
                    self._reject_rule_risk(
                        receipt.id,
                        risk,
                        actor=actor,
                        reason=reason,
                        request_id=original_request_id,
                        now=observed,
                    )
                if expected_kind == "order":
                    self._persist_order(
                        receipt.id,
                        identity,
                        request,
                        risk,
                        actor=actor,
                        reason=reason,
                        request_id=original_request_id,
                        now=observed,
                    )
                else:
                    assert isinstance(candidate.payload, RuleCandidate)
                    self._persist_rule(
                        receipt.id,
                        identity,
                        candidate.payload,
                        risk,
                        actor=actor,
                        reason=reason,
                        request_id=original_request_id,
                        now=observed,
                    )
        except CandidateError:
            raise
        except RequiredDependencyUnavailable:
            raise CandidateError(
                "candidate_dependency_unavailable",
                status_code=503,
            ) from None
        except Exception as exc:
            from trading_assistant.dependencies import (
                RequiredQuoteUnavailable,
            )

            if isinstance(exc, RequiredQuoteUnavailable):
                raise CandidateError(
                    "candidate_dependency_unavailable",
                    status_code=503,
                ) from None
            raise
        self._crash("after_target_commit")
        return self._complete(
            receipt.id,
            identity,
            candidate,
        )


__all__ = [
    "AgentReply",
    "CandidateDraftService",
    "CandidateError",
    "CandidateNonceStore",
    "CandidateQueueResult",
    "CandidateQueueService",
    "CandidateSigner",
    "OrderCandidate",
    "RuleActionCandidate",
    "RuleCandidate",
    "RuleConditionCandidate",
    "SignedCandidate",
]
