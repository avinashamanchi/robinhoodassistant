"""Task 9: signed candidate drafting and crash-safe queue boundary."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, func, select

from trading_assistant.assets import AssetClass
from trading_assistant.app.main import create_app
from trading_assistant.app.limits import InterlockDecision
from trading_assistant.db.models import (
    AuditEvent,
    CandidateNonce,
    CircuitBreakerState,
    Fill,
    Order,
    OrderStateMachine,
    RiskEvent,
    Rule,
    RuleGroup,
)
from trading_assistant.broker.models import OrderStatus
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.risk.engine import BreakerTripIntent, RiskResult
from trading_assistant.rules.worker import RuleWorker


NOW = datetime(2026, 7, 28, 17, 0, tzinfo=timezone.utc)
ACTOR = "operator:local"
REASON = "operator explicitly queued this candidate"
REQUEST_ID = "candidate-boundary-request"
IDEMPOTENCY_KEY = "candidate-boundary-once"


def _candidate_api():
    try:
        from trading_assistant.security.candidates import (
            CandidateDraftService,
            CandidateError,
            CandidateQueueService,
            CandidateSigner,
            OrderCandidate,
            RuleCandidate,
            SignedCandidate,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.fail(f"Task 9 candidate boundary is missing: {exc}")
    return SimpleNamespace(
        CandidateDraftService=CandidateDraftService,
        CandidateError=CandidateError,
        CandidateQueueService=CandidateQueueService,
        CandidateSigner=CandidateSigner,
        OrderCandidate=OrderCandidate,
        RuleCandidate=RuleCandidate,
        SignedCandidate=SignedCandidate,
    )


def _receipt_model():
    try:
        from trading_assistant.db.models import CandidateQueueReceipt
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.fail(f"Task 9 queue receipt model is missing: {exc}")
    return CandidateQueueReceipt


def _signer():
    return _candidate_api().CandidateSigner(
        b"k" * 32,
        now=lambda: NOW,
    )


def _binding(signer, *, session_id: int = 7, authenticated_at=NOW) -> str:
    return signer.session_binding(
        actor=ACTOR,
        session_id=session_id,
        authenticated_at=authenticated_at,
    )


def _service_at(make_service, *, broker=None):
    service = make_service(
        broker=broker,
        quote_now=lambda: NOW,
    )
    service.snapshot_service.now = lambda: NOW
    return service


def _drafts(make_service, *, service=None, signer=None):
    api = _candidate_api()
    signer = signer or _signer()
    service = service or _service_at(make_service)
    return (
        api.CandidateDraftService(
            service,
            signer,
            now=lambda: NOW,
        ),
        service,
        signer,
    )


def _order_envelope(
    make_service,
    *,
    service=None,
    signer=None,
    binding=None,
    tool_input=None,
):
    drafts, service, signer = _drafts(
        make_service,
        service=service,
        signer=signer,
    )
    binding = binding or _binding(signer)
    envelope = drafts.draft_order(
        tool_input
        or {
            "ticker": "aapl",
            "side": "buy",
            "order_type": "market",
            "notional": "100",
            "thesis": "Bounded candidate for explicit operator review.",
        },
        actor=ACTOR,
        session_binding=binding,
    )
    return envelope, service, signer, binding


def _rule_envelope(
    make_service,
    *,
    service=None,
    signer=None,
    binding=None,
    tool_input=None,
):
    drafts, service, signer = _drafts(
        make_service,
        service=service,
        signer=signer,
    )
    binding = binding or _binding(signer)
    envelope = drafts.draft_rule(
        tool_input
        or {
            "ticker": "AAPL",
            "condition": {
                "comparator": "price_below",
                "trigger_price": "90",
            },
            "action": {
                "side": "buy",
                "order_type": "market",
                "notional": "100",
            },
            "thesis": "Queue only after an explicit operator action.",
        },
        actor=ACTOR,
        session_binding=binding,
    )
    return envelope, service, signer, binding


def _queue(service, signer, *, crash_hook=None):
    return _candidate_api().CandidateQueueService(
        service,
        signer,
        now=lambda: NOW,
        crash_hook=crash_hook,
    )


def _queue_order(
    queue,
    envelope,
    binding,
    *,
    idempotency_key=IDEMPOTENCY_KEY,
):
    return queue.queue(
        envelope,
        expected_kind="order",
        actor=ACTOR,
        session_binding=binding,
        idempotency_key=idempotency_key,
        reason=REASON,
        request_id=REQUEST_ID,
    )


def _queue_rule(
    queue,
    envelope,
    binding,
    *,
    idempotency_key=IDEMPOTENCY_KEY,
):
    return queue.queue(
        envelope,
        expected_kind="rule",
        actor=ACTOR,
        session_binding=binding,
        idempotency_key=idempotency_key,
        reason=REASON,
        request_id=REQUEST_ID,
    )


def _mark_receipt_target_persisted(service) -> None:
    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        receipt.state = "target_persisted"
        receipt.completed_at = None
        session.commit()


def test_candidate_models_are_strict_frozen_and_omit_rule_proposal_ttl():
    api = _candidate_api()
    order = api.OrderCandidate.model_validate(
        {
            "ticker": "AAPL",
            "side": "buy",
            "notional": "100",
            "order_type": "market",
            "reference_price": "100",
            "quote_as_of": NOW.isoformat(),
            "thesis": "Explicit review only.",
        }
    )
    assert order.ticker == "AAPL"
    with pytest.raises(ValidationError):
        api.OrderCandidate.model_validate(
            {
                **order.model_dump(mode="json"),
                "unexpected": "forbidden",
            }
        )
    with pytest.raises(ValidationError):
        api.RuleCandidate.model_validate(
            {
                "ticker": "AAPL",
                "condition": {
                    "comparator": "price_below",
                    "trigger_price": "90",
                },
                "action": {
                    "side": "buy",
                    "notional": "100",
                    "order_type": "market",
                },
                "reference_price": "100",
                "quote_as_of": NOW.isoformat(),
                "proposal_ttl_minutes": 30,
                "thesis": "Must not carry an unenforced TTL.",
            }
        )
    with pytest.raises(ValidationError):
        api.OrderCandidate.model_validate(
            {
                **order.model_dump(mode="json"),
                "notional": "NaN",
            }
        )
    with pytest.raises(ValidationError):
        api.OrderCandidate.model_validate(
            {
                **order.model_dump(mode="json"),
                "quantity": "1",
            }
        )
    with pytest.raises(ValidationError):
        api.OrderCandidate.model_validate(
            {
                **order.model_dump(mode="json"),
                "order_type": "limit",
            }
        )
    with pytest.raises(ValidationError):
        order.ticker = "MSFT"


@pytest.mark.parametrize(
    "value",
    ["01", "1.0", "1.2300", "+1", "1e2", "-0", "0", "Infinity", "NaN"],
)
def test_candidate_decimals_reject_noncanonical_or_unsafe_forms(value):
    api = _candidate_api()
    with pytest.raises(ValidationError):
        api.OrderCandidate.model_validate(
            {
                "ticker": "AAPL",
                "side": "buy",
                "notional": value,
                "order_type": "market",
                "reference_price": "100",
                "quote_as_of": NOW.isoformat(),
                "thesis": "Canonical decimal required.",
            }
        )


def test_signer_uses_opaque_session_binding_and_round_trips_exact_payload():
    signer = _signer()
    binding = _binding(signer)
    assert binding != "7"
    assert ACTOR not in binding
    assert "=" not in binding
    drafts_binding = _binding(signer)
    assert drafts_binding == binding


def test_signer_rejects_tamper_wrong_context_future_and_noncanonical_base64(
    make_service,
):
    api = _candidate_api()
    envelope, _service, signer, binding = _order_envelope(make_service)

    verified = signer.verify(
        envelope,
        expected_kind="order",
        actor=ACTOR,
        session_binding=binding,
    )
    assert verified.payload.ticker == "AAPL"

    tampered = envelope.model_copy(
        update={
            "payload": envelope.payload.model_copy(
                update={"notional": Decimal("101")}
            )
        }
    )
    with pytest.raises(api.CandidateError, match="candidate_signature_invalid"):
        signer.verify(
            tampered,
            expected_kind="order",
            actor=ACTOR,
            session_binding=binding,
        )
    for kwargs, code in (
        ({"expected_kind": "rule", "actor": ACTOR, "session_binding": binding}, "candidate_kind_mismatch"),
        ({"expected_kind": "order", "actor": "operator:other", "session_binding": binding}, "candidate_actor_mismatch"),
        ({"expected_kind": "order", "actor": ACTOR, "session_binding": _binding(signer, session_id=8)}, "candidate_session_mismatch"),
    ):
        with pytest.raises(api.CandidateError, match=code):
            signer.verify(envelope, **kwargs)

    future = signer.issue(
        kind="order",
        payload=envelope.payload,
        actor=ACTOR,
        session_binding=binding,
        issued_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(api.CandidateError, match="candidate_issued_in_future"):
        signer.verify(
            future,
            expected_kind="order",
            actor=ACTOR,
            session_binding=binding,
        )

    raw = envelope.model_dump(mode="json")
    raw["nonce"] = raw["nonce"] + "="
    with pytest.raises(ValidationError):
        api.SignedCandidate.model_validate(raw)
    raw = envelope.model_dump(mode="json")
    raw["signature"] = raw["signature"] + "="
    with pytest.raises(ValidationError):
        api.SignedCandidate.model_validate(raw)


def test_signer_rejects_quote_after_issue_stale_quote_and_overlong_envelope(
    make_service,
):
    api = _candidate_api()
    envelope, _service, signer, binding = _order_envelope(make_service)
    after_issue_payload = envelope.payload.model_copy(
        update={"quote_as_of": envelope.issued_at + timedelta(microseconds=1)}
    )
    with pytest.raises(ValueError, match="quote"):
        signer.issue(
            kind="order",
            payload=after_issue_payload,
            actor=ACTOR,
            session_binding=binding,
            issued_at=envelope.issued_at,
        )
    stale_payload = envelope.payload.model_copy(
        update={"quote_as_of": NOW - timedelta(seconds=61)}
    )
    with pytest.raises(api.CandidateError, match="candidate_quote_stale"):
        signer.verify(
            signer.issue(
                kind="order",
                payload=stale_payload,
                actor=ACTOR,
                session_binding=binding,
                issued_at=NOW,
            ),
            expected_kind="order",
            actor=ACTOR,
            session_binding=binding,
            max_quote_age_seconds=60,
        )
    with pytest.raises(ValueError, match="TTL"):
        signer.issue(
            kind="order",
            payload=envelope.payload,
            actor=ACTOR,
            session_binding=binding,
            issued_at=NOW,
            ttl=timedelta(minutes=5, microseconds=1),
        )


def test_reauthentication_changes_candidate_session_binding():
    signer = _signer()
    before = _binding(signer, authenticated_at=NOW)
    after = _binding(
        signer,
        authenticated_at=NOW + timedelta(seconds=1),
    )
    assert before != after


def test_draft_tools_stamp_server_quote_and_enforce_allowlist_and_static_cap(
    make_service,
):
    api = _candidate_api()
    drafts, service, signer = _drafts(make_service)
    binding = _binding(signer)
    before = (
        len(service.get_pending()),
        len(service.list_rules()),
    )
    envelope = drafts.draft_order(
        {
            "ticker": " aapl ",
            "side": "buy",
            "order_type": "market",
            "quantity": "2",
            "thesis": "Use server quote facts only.",
        },
        actor=ACTOR,
        session_binding=binding,
    )
    assert envelope.payload.ticker == "AAPL"
    assert envelope.payload.reference_price == Decimal("100")
    assert envelope.payload.quote_as_of == NOW
    assert (
        len(service.get_pending()),
        len(service.list_rules()),
    ) == before

    for tool_input, code in (
        (
            {
                "ticker": "TSLA",
                "side": "buy",
                "order_type": "market",
                "notional": "100",
                "thesis": "Not allowlisted.",
            },
            "candidate_symbol_denied",
        ),
        (
            {
                "ticker": "AAPL",
                "side": "buy",
                "order_type": "market",
                "notional": "501",
                "thesis": "Above static cap.",
            },
            "candidate_static_cap_exceeded",
        ),
    ):
        with pytest.raises(api.CandidateError, match=code):
            drafts.draft_order(
                tool_input,
                actor=ACTOR,
                session_binding=binding,
            )


def test_server_decimal_trailing_zeroes_normalize_before_signing(
    make_service,
):
    drafts, service, signer = _drafts(make_service)
    service.broker.set_price("AAPL", Decimal("100.00"))

    envelope = drafts.draft_order(
        {
            "ticker": "AAPL",
            "side": "buy",
            "order_type": "market",
            "notional": "100",
            "thesis": "Broker decimals are trusted typed values.",
        },
        actor=ACTOR,
        session_binding=_binding(signer),
    )

    assert envelope.payload.reference_price == Decimal("100")
    assert signer.verify(
        envelope,
        expected_kind="order",
        actor=ACTOR,
        session_binding=_binding(signer),
    ) == envelope


def test_successful_order_queue_persists_only_proposed_and_never_calls_broker_write(
    make_service,
):
    envelope, service, signer, binding = _order_envelope(make_service)
    service.broker.submit_order = lambda *_args, **_kwargs: pytest.fail(
        "candidate queue must never submit"
    )
    service.broker.cancel_order = lambda *_args, **_kwargs: pytest.fail(
        "candidate queue must never cancel"
    )
    service.approve_order = lambda *_args, **_kwargs: pytest.fail(
        "candidate queue must never approve"
    )

    result = _queue_order(_queue(service, signer), envelope, binding)

    assert result.status == "proposed"
    assert result.executed is False
    with service.session_factory() as session:
        CandidateQueueReceipt = _receipt_model()
        order = session.get(Order, result.target_id)
        assert order is not None
        assert order.status == "proposed"
        assert session.scalar(
            select(func.count()).select_from(CandidateNonce)
        ) == 1
        receipt = session.scalar(select(CandidateQueueReceipt))
        assert receipt.state == "completed"
        assert receipt.target_id == order.id
        assert receipt.idempotency_key_hash != IDEMPOTENCY_KEY
        assert REASON not in json.dumps(
            {
                column.name: getattr(receipt, column.name)
                for column in CandidateQueueReceipt.__table__.columns
            },
            default=str,
        )


def test_queue_rejects_missing_reason_before_nonce_or_receipt(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _order_envelope(make_service)

    with pytest.raises(
        api.CandidateError,
        match="candidate_reason_required",
    ) as caught:
        _queue(service, signer).queue(
            envelope,
            expected_kind="order",
            actor=ACTOR,
            session_binding=binding,
            idempotency_key=IDEMPOTENCY_KEY,
            reason=" ",
            request_id=REQUEST_ID,
        )

    assert caught.value.status_code == 422
    with service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(CandidateNonce)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(_receipt_model())
        ) == 0


def test_order_risk_rejection_is_terminal_rejected_order_and_replays(
    make_service,
):
    envelope, service, signer, binding = _order_envelope(
        make_service,
        tool_input={
            "ticker": "AAPL",
            "side": "sell",
            "order_type": "market",
            "quantity": "1",
            "thesis": "No position exists to sell.",
        },
    )
    queue = _queue(service, signer)

    first = _queue_order(queue, envelope, binding)
    second = _queue_order(queue, envelope, binding)

    assert first.status == "rejected"
    assert second == first
    with service.session_factory() as session:
        CandidateQueueReceipt = _receipt_model()
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.get(Order, first.target_id).status == "rejected"


def test_rule_queue_runs_full_risk_then_persists_one_disabled_nonpreapproved_rule(
    make_service,
):
    envelope, service, signer, binding = _rule_envelope(make_service)
    result = _queue_rule(_queue(service, signer), envelope, binding)

    assert result.status == "queued"
    assert result.executed is False
    with service.session_factory() as session:
        rule = session.get(Rule, result.target_id)
        group = session.get(RuleGroup, rule.group_id)
        assert rule.pre_approved is False
        assert rule.state == "active"
        assert rule.activation == "immediate"
        assert group.state == "active"
        assert group.group_key.startswith("candidate-rule-")


def test_rule_risk_rejection_replays_exact_status_and_persists_safety_evidence(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _rule_envelope(make_service)
    risk_engine = service._risk_for(AssetClass.EQUITY)
    risk_engine.check = lambda _order, _snapshot: RiskResult(
        approved=False,
        reasons=["synthetic required-data rejection"],
        warnings=["synthetic rule warning"],
        breaker_trips=(
            BreakerTripIntent(
                BreakerScope.data(AssetClass.EQUITY),
                "synthetic required-data rejection",
            ),
        ),
    )
    queue = _queue(service, signer)

    errors = []
    for _index in range(2):
        with pytest.raises(api.CandidateError) as caught:
            _queue_rule(queue, envelope, binding)
        errors.append((caught.value.code, caught.value.status_code))

    assert errors == [
        ("candidate_risk_rejected", 403),
        ("candidate_risk_rejected", 403),
    ]
    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "completed"
        assert receipt.http_status == 403
        assert receipt.target_id is None
        assert session.scalar(select(func.count()).select_from(Rule)) == 0
        assert session.scalar(
            select(func.count()).select_from(RiskEvent)
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "candidate.rule.queue"
            )
        ) == 1
        breaker = session.get(
            CircuitBreakerState,
            BreakerScope.data(AssetClass.EQUITY).key,
        )
        assert breaker is not None
        assert breaker.tripped is True


def test_accepted_rule_persists_nonblocking_risk_warning(make_service):
    envelope, service, signer, binding = _rule_envelope(make_service)
    risk_engine = service._risk_for(AssetClass.EQUITY)
    risk_engine.check = lambda _order, _snapshot: RiskResult(
        approved=True,
        warnings=["synthetic concentration warning"],
    )

    result = _queue_rule(_queue(service, signer), envelope, binding)

    assert result.status == "queued"
    with service.session_factory() as session:
        warnings = list(
            session.scalars(
                select(RiskEvent).where(
                    RiskEvent.event_type == "warning"
                )
            )
        )
        assert len(warnings) == 1


def test_queued_active_rule_trigger_only_creates_pending_proposal(
    make_service,
):
    envelope, service, signer, binding = _rule_envelope(
        make_service,
        tool_input={
            "ticker": "AAPL",
            "condition": {
                "comparator": "price_above",
                "trigger_price": "90",
            },
            "action": {
                "side": "buy",
                "order_type": "market",
                "notional": "100",
            },
            "thesis": "Explicit queue enables evaluation, never execution.",
        },
    )
    queued = _queue_rule(_queue(service, signer), envelope, binding)
    service.broker.submit_order = lambda *_args, **_kwargs: pytest.fail(
        "non-preapproved rule must never submit"
    )
    worker = RuleWorker(
        service,
        service.rule_repository,
        service.rule_application,
        now=lambda: NOW,
    )

    outcomes = worker.tick(
        actor=ACTOR,
        reason="evaluate explicitly queued rule",
        request_id="candidate-rule-trigger",
    )

    assert len(outcomes) == 1
    assert outcomes[0].proposal is not None
    assert outcomes[0].proposal["status"] == "proposed"
    with service.session_factory() as session:
        source_rule = session.get(Rule, queued.target_id)
        assert source_rule.pre_approved is False
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        order = session.scalar(select(Order))
        assert order.status == "proposed"


@pytest.mark.parametrize("_repeat", range(8))
def test_same_key_retries_and_concurrent_retries_return_exactly_one_target(
    make_service,
    _repeat,
):
    envelope, service, signer, binding = _order_envelope(make_service)
    queue = _queue(service, signer)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _index: _queue_order(
                    queue,
                    envelope,
                    binding,
                ),
                range(4),
            )
        )

    assert len({result.target_id for result in results}) == 1
    assert {result.status for result in results} == {"proposed"}
    with service.session_factory() as session:
        CandidateQueueReceipt = _receipt_model()
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(
            select(func.count()).select_from(CandidateQueueReceipt)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(CandidateNonce)
        ) == 1


def test_same_key_different_candidate_conflicts_and_different_key_nonce_replays(
    make_service,
):
    api = _candidate_api()
    first, service, signer, binding = _order_envelope(make_service)
    second, _service, _signer, _binding_value = _order_envelope(
        make_service,
        service=service,
        signer=signer,
        binding=binding,
        tool_input={
            "ticker": "AAPL",
            "side": "buy",
            "order_type": "market",
            "notional": "101",
            "thesis": "Different candidate.",
        },
    )
    queue = _queue(service, signer)
    _queue_order(queue, first, binding)

    with pytest.raises(api.CandidateError, match="idempotency_conflict"):
        _queue_order(queue, second, binding)
    with pytest.raises(api.CandidateError, match="candidate_replayed"):
        _queue_order(
            queue,
            first,
            binding,
            idempotency_key="candidate-boundary-other-key",
        )


@pytest.mark.parametrize("stage", ["after_receipt_reserve", "after_target_commit"])
def test_order_crash_windows_recover_original_target_without_duplication(
    make_service,
    stage,
):
    envelope, service, signer, binding = _order_envelope(make_service)
    armed = {"value": True}

    def crash_hook(observed):
        if armed["value"] and observed == stage:
            armed["value"] = False
            raise RuntimeError(f"simulated {stage}")

    queue = _queue(service, signer, crash_hook=crash_hook)
    with pytest.raises(RuntimeError, match=stage):
        _queue_order(queue, envelope, binding)

    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        if stage == "after_target_commit":
            assert receipt.state == "completed"
            assert receipt.completed_at == NOW
            assert session.scalar(select(func.count()).select_from(Order)) == 1
        else:
            assert receipt.state == "reserved"
            assert session.scalar(select(func.count()).select_from(Order)) == 0

    recovered = _queue_order(queue, envelope, binding)
    assert recovered.status == "proposed"
    with service.session_factory() as session:
        CandidateQueueReceipt = _receipt_model()
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        receipt = session.scalar(select(CandidateQueueReceipt))
        assert receipt.state == "completed"
        assert receipt.target_id == recovered.target_id


def test_order_crash_before_target_commit_rolls_back_target_and_completion(
    make_service,
):
    envelope, service, signer, binding = _order_envelope(make_service)
    armed = {"value": True}

    def crash_hook(stage):
        if armed["value"] and stage == "before_target_commit":
            armed["value"] = False
            raise RuntimeError("simulated precommit crash")

    queue = _queue(service, signer, crash_hook=crash_hook)
    with pytest.raises(RuntimeError, match="precommit"):
        _queue_order(queue, envelope, binding)

    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "reserved"
        assert receipt.target_id is None
        assert session.scalar(select(func.count()).select_from(Order)) == 0

    recovered = _queue_order(queue, envelope, binding)

    assert recovered.status == "proposed"
    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "completed"
        assert session.scalar(select(func.count()).select_from(Order)) == 1


def test_rule_target_commit_crash_recovers_unique_group_and_rule(make_service):
    envelope, service, signer, binding = _rule_envelope(make_service)
    armed = {"value": True}

    def crash_hook(stage):
        if armed["value"] and stage == "after_target_commit":
            armed["value"] = False
            raise RuntimeError("simulated rule target commit crash")

    queue = _queue(service, signer, crash_hook=crash_hook)
    with pytest.raises(RuntimeError, match="rule target"):
        _queue_rule(queue, envelope, binding)

    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "completed"
        assert receipt.completed_at == NOW
        assert session.scalar(select(func.count()).select_from(RuleGroup)) == 1
        assert session.scalar(select(func.count()).select_from(Rule)) == 1

    recovered = _queue_rule(queue, envelope, binding)
    with service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RuleGroup)) == 1
        assert session.scalar(select(func.count()).select_from(Rule)) == 1
        assert session.get(Rule, recovered.target_id) is not None


def test_rule_crash_before_target_commit_rolls_back_group_rule_and_completion(
    make_service,
):
    envelope, service, signer, binding = _rule_envelope(make_service)
    armed = {"value": True}

    def crash_hook(stage):
        if armed["value"] and stage == "before_target_commit":
            armed["value"] = False
            raise RuntimeError("simulated rule precommit crash")

    queue = _queue(service, signer, crash_hook=crash_hook)
    with pytest.raises(RuntimeError, match="precommit"):
        _queue_rule(queue, envelope, binding)

    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "reserved"
        assert receipt.target_id is None
        assert session.scalar(select(func.count()).select_from(RuleGroup)) == 0
        assert session.scalar(select(func.count()).select_from(Rule)) == 0

    recovered = _queue_rule(queue, envelope, binding)

    assert recovered.status == "queued"
    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "completed"
        assert session.scalar(select(func.count()).select_from(RuleGroup)) == 1
        assert session.scalar(select(func.count()).select_from(Rule)) == 1


def test_receipt_binds_reason_and_reuses_original_request_identity(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _order_envelope(make_service)
    armed = {"value": True}

    def crash_hook(stage):
        if armed["value"] and stage == "after_receipt_reserve":
            armed["value"] = False
            raise RuntimeError("reserved")

    queue = _queue(service, signer, crash_hook=crash_hook)
    with pytest.raises(RuntimeError, match="reserved"):
        queue.queue(
            envelope,
            expected_kind="order",
            actor=ACTOR,
            session_binding=binding,
            idempotency_key=IDEMPOTENCY_KEY,
            reason=REASON,
            request_id="original-request",
        )
    with pytest.raises(api.CandidateError, match="idempotency_conflict"):
        queue.queue(
            envelope,
            expected_kind="order",
            actor=ACTOR,
            session_binding=binding,
            idempotency_key=IDEMPOTENCY_KEY,
            reason="changed reasoning is forbidden",
            request_id="changed-request",
        )

    result = queue.queue(
        envelope,
        expected_kind="order",
        actor=ACTOR,
        session_binding=binding,
        idempotency_key=IDEMPOTENCY_KEY,
        reason=REASON,
        request_id="retry-request",
    )

    assert result.status == "proposed"
    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "candidate.order.queue"
            )
        )
        assert receipt.request_id == "original-request"
        assert receipt.reason_hash != REASON
        assert audit.request_id == "original-request"


def test_completed_same_key_receipt_replays_after_envelope_expiry(
    make_service,
):
    envelope, service, signer, binding = _order_envelope(make_service)
    first = _queue_order(_queue(service, signer), envelope, binding)
    expired_queue = _candidate_api().CandidateQueueService(
        service,
        signer,
        now=lambda: NOW + timedelta(days=1),
    )

    replay = _queue_order(expired_queue, envelope, binding)

    assert replay == first
    with service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1


def test_completed_order_receipt_replays_after_legal_status_progression(
    make_service,
):
    envelope, service, signer, binding = _order_envelope(make_service)
    queue = _queue(service, signer)
    first = _queue_order(queue, envelope, binding)
    with service.session_factory() as session:
        order = session.get(Order, first.target_id)
        OrderStateMachine.transition(order, OrderStatus.EXPIRED)
        session.commit()

    replay = _queue_order(queue, envelope, binding)

    assert replay == first
    with service.session_factory() as session:
        assert session.get(Order, first.target_id).status == "expired"


def test_completed_order_receipt_rejects_unreachable_legacy_approved_state(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _order_envelope(make_service)
    queue = _queue(service, signer)
    first = _queue_order(queue, envelope, binding)
    with service.session_factory() as session:
        session.get(Order, first.target_id).status = OrderStatus.APPROVED.value
        session.commit()

    with pytest.raises(
        api.CandidateError,
        match="candidate_receipt_inconsistent",
    ):
        _queue_order(queue, envelope, binding)


@pytest.mark.parametrize("progressed_state", ["triggered", "canceled"])
def test_completed_rule_receipt_replays_after_trigger_lifecycle_progression(
    make_service,
    progressed_state,
):
    envelope, service, signer, binding = _rule_envelope(make_service)
    queue = _queue(service, signer)
    first = _queue_rule(queue, envelope, binding)
    with service.session_factory() as session:
        rule = session.get(Rule, first.target_id)
        group = session.get(RuleGroup, rule.group_id)
        rule.state = progressed_state
        group.state = progressed_state
        group.version = 1
        if progressed_state == "triggered":
            group.terminal_rule_id = rule.id
        session.commit()

    replay = _queue_rule(queue, envelope, binding)

    assert replay == first
    with service.session_factory() as session:
        assert session.get(Rule, first.target_id).state == progressed_state


@pytest.mark.parametrize(
    ("group_state", "rule_state", "terminal_rule"),
    [
        ("pending", "pending", False),
        ("triggered", "active", True),
        ("active", "triggered", False),
        ("canceled", "triggered", False),
    ],
)
def test_completed_rule_receipt_rejects_backward_or_inconsistent_states(
    make_service,
    group_state,
    rule_state,
    terminal_rule,
):
    api = _candidate_api()
    envelope, service, signer, binding = _rule_envelope(make_service)
    queue = _queue(service, signer)
    first = _queue_rule(queue, envelope, binding)
    with service.session_factory() as session:
        rule = session.get(Rule, first.target_id)
        group = session.get(RuleGroup, rule.group_id)
        group.state = group_state
        rule.state = rule_state
        group.terminal_rule_id = rule.id if terminal_rule else None
        session.commit()

    with pytest.raises(
        api.CandidateError,
        match="candidate_receipt_inconsistent",
    ):
        _queue_rule(queue, envelope, binding)


def test_completed_rule_receipt_rejects_terminal_state_without_version_progress(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _rule_envelope(make_service)
    queue = _queue(service, signer)
    first = _queue_rule(queue, envelope, binding)
    with service.session_factory() as session:
        rule = session.get(Rule, first.target_id)
        group = session.get(RuleGroup, rule.group_id)
        group.state = "canceled"
        rule.state = "canceled"
        assert group.version == 0
        session.commit()

    with pytest.raises(
        api.CandidateError,
        match="candidate_receipt_inconsistent",
    ):
        _queue_rule(queue, envelope, binding)


def test_target_persisted_order_recovery_accepts_legal_forward_progression(
    make_service,
):
    envelope, service, signer, binding = _order_envelope(make_service)
    queue = _queue(service, signer)
    first = _queue_order(queue, envelope, binding)
    _mark_receipt_target_persisted(service)
    with service.session_factory() as session:
        order = session.get(Order, first.target_id)
        OrderStateMachine.transition(order, OrderStatus.EXPIRED)
        session.commit()

    replay = _queue_order(queue, envelope, binding)

    assert replay == first
    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "completed"


@pytest.mark.parametrize(
    "tamper",
    [
        "broker_order_id",
        "approval_metadata",
        "submission_markers",
        "fill_marker",
    ],
)
def test_target_persisted_initial_order_rejects_lifecycle_marker_tamper(
    make_service,
    tamper,
):
    api = _candidate_api()
    envelope, service, signer, binding = _order_envelope(make_service)
    queue = _queue(service, signer)
    first = _queue_order(queue, envelope, binding)
    _mark_receipt_target_persisted(service)
    with service.session_factory() as session:
        order = session.get(Order, first.target_id)
        if tamper == "broker_order_id":
            order.broker_order_id = "unexpected-broker-order"
        elif tamper == "approval_metadata":
            order.approval_actor = "unexpected-approver"
            order.approved_at = NOW
        elif tamper == "submission_markers":
            order.submission_attempt = 1
            order.submission_started_at = NOW
            order.acceptance_state = "started"
        else:
            session.add(
                Fill(
                    order_id=order.id,
                    ticker=order.ticker,
                    side=order.side,
                    qty=Decimal("1"),
                    price=Decimal("100"),
                    broker_fill_id="unexpected-candidate-fill",
                    filled_at=NOW,
                )
            )
        session.commit()

    with pytest.raises(
        api.CandidateError,
        match="candidate_receipt_inconsistent",
    ):
        _queue_order(queue, envelope, binding)


def test_target_persisted_rule_recovery_accepts_legal_forward_progression(
    make_service,
):
    envelope, service, signer, binding = _rule_envelope(make_service)
    queue = _queue(service, signer)
    first = _queue_rule(queue, envelope, binding)
    _mark_receipt_target_persisted(service)
    with service.session_factory() as session:
        rule = session.get(Rule, first.target_id)
        group = session.get(RuleGroup, rule.group_id)
        rule.state = "triggered"
        group.state = "triggered"
        group.terminal_rule_id = rule.id
        group.version = 1
        session.commit()

    replay = _queue_rule(queue, envelope, binding)

    assert replay == first
    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "completed"


@pytest.mark.parametrize(
    "tamper",
    [
        "group_key",
        "group_initial_state",
        "group_terminal_rule_id",
        "group_version",
        "group_lease",
        "group_reconciliation",
        "payload_version",
        "ticker",
        "kind",
        "condition_json",
        "action_quantity",
        "action_notional",
        "action_limit",
        "rule_initial_state",
        "activation",
        "pre_approved",
        "terminal_on_trigger",
        "fraction",
        "hwm",
        "deadline",
        "plan_id",
    ],
)
def test_rule_target_persisted_recovery_rejects_any_immutable_drift(
    make_service,
    tamper,
):
    api = _candidate_api()
    envelope, service, signer, binding = _rule_envelope(make_service)
    queue = _queue(service, signer)
    _queue_rule(queue, envelope, binding)
    _mark_receipt_target_persisted(service)

    with service.session_factory() as session:
        rule = session.scalar(select(Rule))
        group = session.get(RuleGroup, rule.group_id)
        if tamper == "group_key":
            group.group_key = "tampered-group-key"
        elif tamper == "group_initial_state":
            group.state = "triggered"
        elif tamper == "group_terminal_rule_id":
            group.terminal_rule_id = rule.id
        elif tamper == "group_version":
            group.version = 1
        elif tamper == "group_lease":
            group.lease_owner = "unexpected-worker"
            group.lease_expires_at = NOW + timedelta(minutes=1)
        elif tamper == "group_reconciliation":
            group.reconciliation_required = True
        elif tamper == "payload_version":
            rule.payload_version = 2
        elif tamper == "ticker":
            rule.ticker = "MSFT"
        elif tamper == "kind":
            rule.kind = "target"
        elif tamper == "condition_json":
            rule.condition_json = '{"direction":"below","price":"91","type":"price"}'
        elif tamper.startswith("action_"):
            action = json.loads(rule.action_json)
            if tamper == "action_quantity":
                action["qty"] = "1"
                action["notional"] = None
            elif tamper == "action_notional":
                action["notional"] = "101"
            else:
                action["order_type"] = "limit"
                action["limit_price"] = "99"
            rule.action_json = json.dumps(
                action,
                sort_keys=True,
                separators=(",", ":"),
            )
        elif tamper == "rule_initial_state":
            rule.state = "triggered"
        elif tamper == "activation":
            rule.activation = "on_entry_fill"
        elif tamper == "pre_approved":
            rule.pre_approved = True
        elif tamper == "terminal_on_trigger":
            rule.terminal_on_trigger = False
        elif tamper == "fraction":
            rule.fraction = Decimal("0.5")
        elif tamper == "hwm":
            rule.hwm = Decimal("101")
        elif tamper == "deadline":
            rule.deadline = NOW + timedelta(days=1)
        elif tamper == "plan_id":
            rule.plan_id = 99
        session.commit()

    with pytest.raises(
        api.CandidateError,
        match="candidate_receipt_inconsistent",
    ):
        _queue_rule(queue, envelope, binding)


def test_completed_rule_replay_still_rejects_immutable_payload_drift(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _rule_envelope(make_service)
    queue = _queue(service, signer)
    _queue_rule(queue, envelope, binding)
    with service.session_factory() as session:
        rule = session.scalar(select(Rule))
        action = json.loads(rule.action_json)
        action["notional"] = "101"
        rule.action_json = json.dumps(
            action,
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()

    with pytest.raises(
        api.CandidateError,
        match="candidate_receipt_inconsistent",
    ):
        _queue_rule(queue, envelope, binding)


def test_reserved_receipt_cannot_resume_after_candidate_ttl(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _order_envelope(make_service)
    service.broker.get_positions = lambda: (_ for _ in ()).throw(
        RuntimeError("offline")
    )
    first_queue = _queue(service, signer)
    with pytest.raises(
        api.CandidateError,
        match="candidate_dependency_unavailable",
    ):
        _queue_order(first_queue, envelope, binding)

    expired_queue = api.CandidateQueueService(
        service,
        signer,
        now=lambda: NOW + timedelta(minutes=6),
    )
    errors = []
    for _index in range(2):
        with pytest.raises(api.CandidateError) as caught:
            _queue_order(expired_queue, envelope, binding)
        errors.append((caught.value.code, caught.value.status_code))

    assert errors == [
        ("candidate_expired", 409),
        ("candidate_expired", 409),
    ]
    with service.session_factory() as session:
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "completed"
        assert receipt.outcome_code == "candidate_expired"
        assert receipt.http_status == 409
        assert session.scalar(select(func.count()).select_from(Order)) == 0


def test_visible_raw_nonce_hash_target_cannot_preclaim_rule_recovery(
    make_service,
):
    envelope, service, signer, binding = _rule_envelope(make_service)
    armed = {"value": True}

    def crash_hook(stage):
        if armed["value"] and stage == "after_receipt_reserve":
            armed["value"] = False
            raise RuntimeError("reserved")

    queue = _queue(service, signer, crash_hook=crash_hook)
    with pytest.raises(RuntimeError, match="reserved"):
        _queue_rule(queue, envelope, binding)
    raw_group_key = (
        "candidate-rule-" + signer.nonce_hash(envelope.nonce)[:40]
    )
    with service.session_factory() as session:
        session.add(
            RuleGroup(
                group_key=raw_group_key,
                state="active",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    recovered = _queue_rule(queue, envelope, binding)

    with service.session_factory() as session:
        target = session.get(Rule, recovered.target_id)
        target_group = session.get(RuleGroup, target.group_id)
        assert target_group.group_key != raw_group_key
        assert session.scalar(
            select(func.count()).select_from(Rule)
        ) == 1


def test_rule_receipt_recovery_requires_exactly_one_target(make_service):
    api = _candidate_api()
    envelope, service, signer, binding = _rule_envelope(make_service)
    armed = {"value": True}

    def crash_hook(stage):
        if armed["value"] and stage == "after_target_commit":
            armed["value"] = False
            raise RuntimeError("target committed")

    queue = _queue(service, signer, crash_hook=crash_hook)
    with pytest.raises(RuntimeError, match="target committed"):
        _queue_rule(queue, envelope, binding)
    with service.session_factory() as session:
        original = session.scalar(select(Rule))
        session.add(
            Rule(
                group_id=original.group_id,
                payload_version=original.payload_version,
                ticker=original.ticker,
                condition_json=original.condition_json,
                action_json=original.action_json,
                state="active",
                kind=original.kind,
                pre_approved=False,
                activation="immediate",
                terminal_on_trigger=True,
                created_at=NOW,
            )
        )
        session.commit()

    with pytest.raises(
        api.CandidateError,
        match="candidate_receipt_inconsistent",
    ):
        _queue_rule(queue, envelope, binding)


def test_dependency_failure_is_resumable_only_by_same_receipt(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _order_envelope(make_service)
    original = service.broker.get_positions
    service.broker.get_positions = lambda: (_ for _ in ()).throw(
        RuntimeError("offline")
    )
    queue = _queue(service, signer)

    with pytest.raises(api.CandidateError, match="candidate_dependency_unavailable"):
        _queue_order(queue, envelope, binding)
    with pytest.raises(api.CandidateError, match="candidate_replayed"):
        _queue_order(
            queue,
            envelope,
            binding,
            idempotency_key="different-key-during-outage",
        )

    service.broker.get_positions = original
    recovered = _queue_order(queue, envelope, binding)
    assert recovered.status == "proposed"


def test_signed_quote_can_expire_before_envelope_and_queue_fails_closed(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _order_envelope(make_service)
    queue = api.CandidateQueueService(
        service,
        signer,
        now=lambda: NOW + timedelta(seconds=61),
    )
    with pytest.raises(api.CandidateError, match="candidate_quote_stale"):
        _queue_order(queue, envelope, binding)
    with pytest.raises(api.CandidateError, match="candidate_quote_stale"):
        _queue_order(queue, envelope, binding)
    with pytest.raises(api.CandidateError, match="candidate_quote_stale"):
        _queue_order(
            queue,
            envelope,
            binding,
            idempotency_key="stale-candidate-other-key",
        )
    with service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(
            select(func.count()).select_from(CandidateNonce)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(_receipt_model())
        ) == 0


def test_queue_rejects_wrong_actor_session_kind_and_expired_candidate(
    make_service,
):
    api = _candidate_api()
    envelope, service, signer, binding = _order_envelope(make_service)
    queue = _queue(service, signer)
    cases = (
        (
            {"actor": "operator:other", "session_binding": binding, "expected_kind": "order"},
            "candidate_actor_mismatch",
        ),
        (
            {"actor": ACTOR, "session_binding": _binding(signer, session_id=99), "expected_kind": "order"},
            "candidate_session_mismatch",
        ),
        (
            {"actor": ACTOR, "session_binding": binding, "expected_kind": "rule"},
            "candidate_kind_mismatch",
        ),
    )
    for values, code in cases:
        with pytest.raises(api.CandidateError, match=code):
            queue.queue(
                envelope,
                idempotency_key=f"{code}-key",
                reason=REASON,
                request_id=REQUEST_ID,
                **values,
            )
    expired_queue = api.CandidateQueueService(
        service,
        signer,
        now=lambda: NOW + timedelta(minutes=5, microseconds=1),
    )
    with pytest.raises(api.CandidateError, match="candidate_expired"):
        _queue_order(expired_queue, envelope, binding)


def test_candidate_expires_at_exact_observed_instant(make_service):
    api = _candidate_api()
    envelope, service, signer, binding = _order_envelope(make_service)
    queue = api.CandidateQueueService(
        service,
        signer,
        now=lambda: envelope.expires_at,
    )

    with pytest.raises(api.CandidateError, match="candidate_expired"):
        _queue_order(queue, envelope, binding)

    with service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(CandidateNonce)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(_receipt_model())
        ) == 0


def test_provider_reads_never_happen_inside_sqlite_write_transaction(
    make_service,
    engine,
):
    state = threading.local()
    state.write = False

    def before_execute(_conn, _cursor, statement, _params, _context, _many):
        normalized = statement.lstrip().upper()
        if normalized.startswith(
            ("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE")
        ):
            state.write = True

    def clear(*_args):
        state.write = False

    event.listen(engine, "before_cursor_execute", before_execute)
    event.listen(engine, "commit", clear)
    event.listen(engine, "rollback", clear)
    envelope, service, signer, binding = _order_envelope(make_service)
    for name in ("get_quote", "get_account", "get_positions"):
        original = getattr(service.broker, name)

        def guarded(*args, _original=original, **kwargs):
            assert not getattr(state, "write", False)
            return _original(*args, **kwargs)

        setattr(service.broker, name, guarded)

    _queue_order(_queue(service, signer), envelope, binding)


def test_receipts_are_isolated_by_session_kind_and_provider_independent_state(
    make_service,
):
    service = _service_at(make_service)
    signer = _signer()
    first_binding = _binding(signer, session_id=1)
    second_binding = _binding(signer, session_id=2)
    first, *_ = _order_envelope(
        make_service,
        service=service,
        signer=signer,
        binding=first_binding,
    )
    second, *_ = _order_envelope(
        make_service,
        service=service,
        signer=signer,
        binding=second_binding,
    )
    rule, *_ = _rule_envelope(
        make_service,
        service=service,
        signer=signer,
        binding=first_binding,
    )
    queue = _queue(service, signer)
    queue.queue(
        first,
        expected_kind="order",
        actor=ACTOR,
        session_binding=first_binding,
        idempotency_key="shared-visible-key",
        reason=REASON,
        request_id="session-one",
    )
    queue.queue(
        second,
        expected_kind="order",
        actor=ACTOR,
        session_binding=second_binding,
        idempotency_key="shared-visible-key",
        reason=REASON,
        request_id="session-two",
    )
    queue.queue(
        rule,
        expected_kind="rule",
        actor=ACTOR,
        session_binding=first_binding,
        idempotency_key="shared-visible-key",
        reason=REASON,
        request_id="kind-rule",
    )
    with service.session_factory() as session:
        CandidateQueueReceipt = _receipt_model()
        assert session.scalar(
            select(func.count()).select_from(CandidateQueueReceipt)
        ) == 3


def test_http_queue_requires_csrf_idempotency_and_never_approves_or_submits(
    make_service,
    authenticate_client,
):
    service = _service_at(make_service)
    signer = _signer()
    queue = _queue(service, signer)
    forbidden_calls = []

    def forbidden(name):
        def fail(*_args, **_kwargs):
            forbidden_calls.append(name)
            raise AssertionError(f"{name} must not run")

        return fail

    service.approve_order = forbidden("approve")
    service.cancel_live_order = forbidden("cancel")
    service.broker.cancel_order = forbidden("broker_cancel")

    class StubAgent:
        def chat(self, message, **context):
            return {"reply": message, "candidates": []}

    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token="candidate-http-operator-secret",
        planning=None,
        candidate_signer=signer,
        candidate_queue=queue,
    )
    client, csrf = authenticate_client(
        TestClient(app),
        "candidate-http-operator-secret",
    )
    token = client.cookies.get(app.state.session_auth.cookie_name())
    principal = app.state.session_auth.authenticate(token)
    binding = signer.session_binding(
        actor=principal.actor,
        session_id=principal.session_id,
        authenticated_at=principal.authenticated_at,
    )
    drafts = _candidate_api().CandidateDraftService(
        service,
        signer,
        now=lambda: NOW,
    )
    envelope = drafts.draft_order(
        {
            "ticker": "AAPL",
            "side": "buy",
            "order_type": "market",
            "notional": "100",
            "thesis": "HTTP explicit queue.",
        },
        actor=principal.actor,
        session_binding=binding,
    )
    body = {
        "candidate": envelope.model_dump(mode="json"),
        "reason": REASON,
    }

    assert client.post("/candidates/order/queue", json=body).status_code == 403
    assert client.post(
        "/candidates/order/queue",
        json=body,
        headers={"X-CSRF-Token": csrf},
    ).status_code == 422
    response = client.post(
        "/candidates/order/queue",
        json=body,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "http-candidate-once",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "proposed"
    assert response.json()["executed"] is False
    assert service.broker.submit_calls == 0
    assert forbidden_calls == []


def test_candidate_queue_http_rate_denial_is_fail_closed(
    make_service,
    authenticate_client,
):
    service = _service_at(make_service)
    limits = service.config.security.rate_limits
    mutation = limits.mutation.model_copy(
        update={
            "requests": 1,
            "global_requests": 1,
            "window_seconds": 60,
            "concurrency": 1,
        }
    )
    service.config = service.config.model_copy(
        update={
            "security": service.config.security.model_copy(
                update={
                    "rate_limits": limits.model_copy(
                        update={"mutation": mutation}
                    )
                }
            )
        }
    )
    signer = _signer()
    queue = _queue(service, signer)

    class StubAgent:
        def chat(self, message, **context):
            return {"reply": message, "candidates": []}

    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token="candidate-rate-operator-secret",
        planning=None,
        candidate_signer=signer,
        candidate_queue=queue,
    )
    client, csrf = authenticate_client(
        TestClient(app),
        "candidate-rate-operator-secret",
    )
    token = client.cookies.get(app.state.session_auth.cookie_name())
    principal = app.state.session_auth.authenticate(token)
    binding = signer.session_binding(
        actor=principal.actor,
        session_id=principal.session_id,
        authenticated_at=principal.authenticated_at,
    )
    drafts = _candidate_api().CandidateDraftService(
        service,
        signer,
        now=lambda: NOW,
    )

    def body(notional):
        envelope = drafts.draft_order(
            {
                "ticker": "AAPL",
                "side": "buy",
                "order_type": "market",
                "notional": notional,
                "thesis": "Explicit rate-limit test candidate.",
            },
            actor=principal.actor,
            session_binding=binding,
        )
        return {
            "candidate": envelope.model_dump(mode="json"),
            "reason": REASON,
        }

    headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "candidate-rate-first",
    }
    first = client.post(
        "/candidates/order/queue",
        json=body("100"),
        headers=headers,
    )
    assert first.status_code == 201, first.text

    headers["Idempotency-Key"] = "candidate-rate-second"
    denied = client.post(
        "/candidates/order/queue",
        json=body("101"),
        headers=headers,
    )
    assert denied.status_code == 429, denied.text
    assert service.broker.submit_calls == 0


def test_receipt_retry_ignores_abandoned_generic_mutation_interlock(
    make_service,
    authenticate_client,
):
    service = _service_at(make_service)
    signer = _signer()
    queue = _queue(service, signer)

    class StubAgent:
        def chat(self, message, **context):
            return {"reply": message, "candidates": []}

    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token="candidate-recovery-operator-secret",
        planning=None,
        candidate_signer=signer,
        candidate_queue=queue,
    )
    client, csrf = authenticate_client(
        TestClient(app),
        "candidate-recovery-operator-secret",
    )
    token = client.cookies.get(app.state.session_auth.cookie_name())
    principal = app.state.session_auth.authenticate(token)
    binding = signer.session_binding(
        actor=principal.actor,
        session_id=principal.session_id,
        authenticated_at=principal.authenticated_at,
    )
    envelope = _candidate_api().CandidateDraftService(
        service,
        signer,
        now=lambda: NOW,
    ).draft_order(
        {
            "ticker": "AAPL",
            "side": "buy",
            "order_type": "market",
            "notional": "100",
            "thesis": "Recover only through the durable candidate receipt.",
        },
        actor=principal.actor,
        session_binding=binding,
    )
    armed = {"value": True}

    def crash_after_reserve(stage):
        if armed["value"] and stage == "after_receipt_reserve":
            armed["value"] = False
            raise RuntimeError("receipt reserved")

    with pytest.raises(RuntimeError, match="receipt reserved"):
        _candidate_api().CandidateQueueService(
            service,
            signer,
            now=lambda: NOW,
            crash_hook=crash_after_reserve,
        ).queue(
            envelope,
            expected_kind="order",
            actor=principal.actor,
            session_binding=binding,
            idempotency_key="receipt-managed-retry",
            reason=REASON,
            request_id="receipt-managed-original",
        )

    app.state.mutation_interlocks.inspect = lambda _key: InterlockDecision(
        acquired=False,
        resource_key="abandoned-generic-interlock",
        owner="dead-worker",
        generation=1,
        operation="order_approve",
        state="uncertain",
        outcome_code="handler_failed",
        worker_finished_at=NOW,
    )
    app.state.mutation_interlocks.claim = (
        lambda *_args, **_kwargs: pytest.fail(
            "receipt-managed queue must not claim the generic interlock"
        )
    )
    response = client.post(
        "/candidates/order/queue",
        json={
            "candidate": envelope.model_dump(mode="json"),
            "reason": REASON,
        },
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "receipt-managed-retry",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "proposed"
    with service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        receipt = session.scalar(select(_receipt_model()))
        assert receipt.state == "completed"


def test_candidate_route_lease_still_rejects_concurrent_execution(
    make_service,
    authenticate_client,
):
    service = _service_at(make_service)
    signer = _signer()
    delegate = _queue(service, signer)
    entered = threading.Event()
    release = threading.Event()
    queue_calls = 0
    queue_lock = threading.Lock()

    class BlockingQueue:
        def queue(self, *args, **kwargs):
            nonlocal queue_calls
            with queue_lock:
                queue_calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return delegate.queue(*args, **kwargs)

    class StubAgent:
        def chat(self, message, **context):
            return {"reply": message, "candidates": []}

    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token="candidate-lease-operator-secret",
        planning=None,
        candidate_signer=signer,
        candidate_queue=BlockingQueue(),
    )
    owner, csrf = authenticate_client(
        TestClient(app),
        "candidate-lease-operator-secret",
    )
    token = owner.cookies.get(app.state.session_auth.cookie_name())
    principal = app.state.session_auth.authenticate(token)
    binding = signer.session_binding(
        actor=principal.actor,
        session_id=principal.session_id,
        authenticated_at=principal.authenticated_at,
    )
    envelope = _candidate_api().CandidateDraftService(
        service,
        signer,
        now=lambda: NOW,
    ).draft_order(
        {
            "ticker": "AAPL",
            "side": "buy",
            "order_type": "market",
            "notional": "100",
            "thesis": "One route lease at a time.",
        },
        actor=principal.actor,
        session_binding=binding,
    )
    request = {
        "json": {
            "candidate": envelope.model_dump(mode="json"),
            "reason": REASON,
        },
        "headers": {
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "candidate-route-lease",
        },
    }

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner_future = pool.submit(
            lambda: owner.post(
                "/candidates/order/queue",
                **request,
            )
        )
        assert entered.wait(timeout=5)
        follower = owner.post(
            "/candidates/order/queue",
            **request,
        )
        release.set()
        owner_response = owner_future.result(timeout=5)

    assert owner_response.status_code == 201, owner_response.text
    assert follower.status_code == 409
    assert follower.json()["error"]["code"] == "route_busy"
    assert queue_calls == 1


def test_http_terminal_rule_rejection_replays_original_403(
    make_service,
    authenticate_client,
):
    service = _service_at(make_service)
    signer = _signer()
    risk_engine = service._risk_for(AssetClass.EQUITY)
    risk_engine.check = lambda _order, _snapshot: RiskResult(
        approved=False,
        reasons=["synthetic terminal rejection"],
    )

    class StubAgent:
        def chat(self, message, **context):
            return {"reply": message, "candidates": []}

    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token="candidate-terminal-operator-secret",
        planning=None,
        candidate_signer=signer,
        candidate_queue=_queue(service, signer),
    )
    client, csrf = authenticate_client(
        TestClient(app),
        "candidate-terminal-operator-secret",
    )
    token = client.cookies.get(app.state.session_auth.cookie_name())
    principal = app.state.session_auth.authenticate(token)
    binding = signer.session_binding(
        actor=principal.actor,
        session_id=principal.session_id,
        authenticated_at=principal.authenticated_at,
    )
    drafts = _candidate_api().CandidateDraftService(
        service,
        signer,
        now=lambda: NOW,
    )
    envelope = drafts.draft_rule(
        {
            "ticker": "AAPL",
            "condition": {
                "comparator": "price_below",
                "trigger_price": "90",
            },
            "action": {
                "side": "buy",
                "order_type": "market",
                "notional": "100",
            },
            "thesis": "Terminal status must replay exactly.",
        },
        actor=principal.actor,
        session_binding=binding,
    )
    headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "candidate-terminal-rule",
    }
    body = {
        "candidate": envelope.model_dump(mode="json"),
        "reason": REASON,
    }

    responses = [
        client.post(
            "/candidates/rule/queue",
            json=body,
            headers=headers,
        )
        for _index in range(2)
    ]

    assert [response.status_code for response in responses] == [403, 403]
    assert {
        response.json()["error"]["code"] for response in responses
    } == {"candidate_risk_rejected"}
    assert service.broker.submit_calls == 0


def test_duplicate_json_and_extra_fields_fail_at_candidate_http_boundary(
    make_service,
    authenticate_client,
):
    service = _service_at(make_service)
    signer = _signer()

    class StubAgent:
        def chat(self, message, **context):
            return {"reply": message, "candidates": []}

    app = create_app(
        service=service,
        agent=StubAgent(),
        api_token="candidate-json-operator-secret",
        planning=None,
        candidate_signer=signer,
        candidate_queue=_queue(service, signer),
    )
    client, csrf = authenticate_client(
        TestClient(app),
        "candidate-json-operator-secret",
    )
    headers = {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "duplicate-json-rejected",
    }
    duplicate = (
        '{"candidate":{},"candidate":{},"reason":"'
        + REASON
        + '"}'
    )
    response = client.post(
        "/candidates/order/queue",
        content=duplicate,
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    response = client.post(
        "/candidates/order/queue",
        json={"candidate": {}, "reason": REASON, "extra": True},
        headers={
            **headers,
            "Idempotency-Key": "extra-json-rejected",
        },
    )
    assert response.status_code == 422
