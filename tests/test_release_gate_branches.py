from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Thread

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from trading_assistant.app.auth import (
    CsrfRejected,
    InvalidCredentials,
    InvalidSession,
    SessionAuth,
)
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    OrderResult,
    OrderStatus,
    OrderTimeInForce,
)
from trading_assistant.db.models import AuthSession, Order, Proposal, Rule, RuleGroup
from trading_assistant.db.models import utcnow
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.orders.submission import bracket_prices, order_to_request
from trading_assistant.risk.submission_barrier import (
    SubmissionBarrier,
    SubmissionGuard,
)
from trading_assistant.rules.models import RuleCommand, RuleState
from trading_assistant.rules.repository import RuleRepository
from trading_assistant.rules.worker import RuleWorker
from trading_assistant.security.sensitive_fields import sensitive_store

AUTH_SECRET = "task-10-release-gate-auth-secret"
NOW = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
MUTATION = {
    "actor": "operator:release-gate",
    "reason": "exercise a release safety branch",
    "request_id": "task-10-release-branch",
}


def _auth(session_factory, *, secret=AUTH_SECRET):
    return SessionAuth(
        session_factory,
        application_secret=secret,
        now=lambda: NOW,
    )


@pytest.mark.parametrize("application_secret", ["", "   "])
def test_session_auth_refuses_missing_application_key(
    session_factory,
    application_secret,
):
    with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
        _auth(session_factory, secret=application_secret)


def test_login_refuses_missing_or_mismatched_expected_key(session_factory):
    auth = _auth(session_factory)

    with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
        auth.login(AUTH_SECRET, "")
    with pytest.raises(RuntimeError, match="key mismatch"):
        auth.login(AUTH_SECRET, "different-release-gate-secret")
    with pytest.raises(InvalidCredentials):
        auth.login(None, AUTH_SECRET)


def test_reauthentication_refuses_missing_mismatched_and_non_string_key(
    session_factory,
):
    auth = _auth(session_factory)
    issued = auth.login(AUTH_SECRET, AUTH_SECRET)

    with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
        auth.reauthenticate(issued.token, AUTH_SECRET, "")
    with pytest.raises(RuntimeError, match="key mismatch"):
        auth.reauthenticate(
            issued.token,
            AUTH_SECRET,
            "different-release-gate-secret",
        )
    with pytest.raises(InvalidCredentials):
        auth.reauthenticate(issued.token, None, AUTH_SECRET)


@pytest.mark.parametrize(
    ("operation", "row_change", "expected_error"),
    [
        ("csrf", "delete", InvalidSession),
        ("csrf", "revoke", InvalidSession),
        ("csrf", "corrupt", CsrfRejected),
        ("reauthenticate", "delete", InvalidSession),
        ("reauthenticate", "revoke", InvalidSession),
        ("logout", "delete", InvalidSession),
    ],
)
def test_session_mutations_fail_closed_when_state_changes_mid_request(
    session_factory,
    operation,
    row_change,
    expected_error,
):
    class RacingSessionAuth(SessionAuth):
        def authenticate(self, token):
            principal = super().authenticate(token)
            with self.session_factory() as session:
                row = session.get(AuthSession, principal.session_id)
                assert row is not None
                if row_change == "delete":
                    session.delete(row)
                elif row_change == "revoke":
                    row.revoked_at = NOW
                else:
                    row.csrf_hash = "0" * 64
                session.commit()
            return principal

    auth = RacingSessionAuth(
        session_factory,
        application_secret=AUTH_SECRET,
        now=lambda: NOW,
    )
    issued = SessionAuth(
        session_factory,
        application_secret=AUTH_SECRET,
        now=lambda: NOW,
    ).login(AUTH_SECRET, AUTH_SECRET)

    with pytest.raises(expected_error):
        if operation == "csrf":
            auth.csrf_token(issued.token)
        elif operation == "reauthenticate":
            auth.reauthenticate(
                issued.token,
                AUTH_SECRET,
                AUTH_SECRET,
            )
        else:
            auth.logout(issued.token)


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"take_profit": "110"}',
        '{"take_profit": "bad", "stop_loss": "90"}',
    ],
)
def test_bracket_payload_must_have_exact_positive_decimal_shape(
    payload,
):
    order = Order(
        idempotency_key="release-invalid-bracket",
        ticker="AAPL",
        side="buy",
        order_type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("100"),
        status=OrderStatus.APPROVAL_RECORDED.value,
        submission_kind="bracket",
        submission_payload_json=payload,
    )

    with pytest.raises(ValueError, match="invalid bracket submission payload"):
        bracket_prices(order)


def test_bracket_payload_refuses_nonpositive_prices():
    order = Order(
        idempotency_key="release-nonpositive-bracket",
        ticker="AAPL",
        side="buy",
        order_type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("100"),
        status=OrderStatus.APPROVAL_RECORDED.value,
        submission_kind="bracket",
        submission_payload_json='{"take_profit": "0", "stop_loss": "90"}',
    )

    with pytest.raises(ValueError, match="positive"):
        bracket_prices(order)


def test_simple_submission_payload_persists_explicit_gtc():
    order = Order(
        idempotency_key="release-explicit-gtc",
        ticker="AAPL",
        side="buy",
        order_type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("96"),
        status=OrderStatus.APPROVAL_RECORDED.value,
        submission_kind="simple",
        submission_payload_json='{"time_in_force": "gtc"}',
    )

    assert order_to_request(order).time_in_force is OrderTimeInForce.GTC


@pytest.mark.parametrize(
    "payload",
    [
        '{"time_in_force": "ioc"}',
        '{"time_in_force": "gtc", "unexpected": true}',
        "[]",
    ],
)
def test_simple_submission_payload_refuses_unknown_or_malformed_time_in_force(
    payload,
):
    order = Order(
        idempotency_key="release-invalid-time-in-force",
        ticker="AAPL",
        side="buy",
        order_type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("96"),
        status=OrderStatus.APPROVAL_RECORDED.value,
        submission_kind="simple",
        submission_payload_json=payload,
    )

    with pytest.raises(ValueError, match="invalid order time in force"):
        order_to_request(order)


def _approval_recorded(service) -> int:
    proposal = service.propose_order(
        "AAPL",
        "buy",
        "limit",
        qty="1",
        limit_price="96",
        actor="operator:release-gate",
        reason="prepare approval claim branch",
        request_id="prepare-release-approval",
    )
    service.order_application.approve(
        ApprovalCommand(
            proposal["order_id"],
            "operator:release-gate",
            "record human release-gate approval",
            utcnow(),
            "record-release-approval",
        )
    )
    return proposal["order_id"]


def test_submission_refuses_missing_context_and_missing_order(make_service):
    submission = make_service().order_submission

    with pytest.raises(ValueError, match="must be non-empty"):
        submission.submit(1, actor="", reason="reason", request_id="request")
    with pytest.raises(KeyError, match="not found"):
        submission.submit(999, **MUTATION)


def test_submission_returns_changed_state_when_claim_is_lost(
    make_service,
    monkeypatch,
):
    service = make_service()
    order_id = _approval_recorded(service)
    monkeypatch.setattr(
        service.order_submission.repository,
        "claim_submission",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        service.order_submission.repository,
        "expire_approved",
        lambda *args, **kwargs: False,
    )

    result = service.order_submission.submit(order_id, **MUTATION)

    assert result.status is OrderStatus.APPROVAL_RECORDED
    assert service.broker.submit_calls == 0


def test_submission_fails_closed_if_lost_claim_order_disappears(
    make_service,
    monkeypatch,
):
    service = make_service()
    order_id = _approval_recorded(service)

    def remove_order(*args, **kwargs):
        with service.session_factory() as session:
            proposal = session.scalar(
                select(Proposal).where(Proposal.order_id == order_id)
            )
            if proposal is not None:
                sensitive_store(session).delete(proposal)
            order = session.get(Order, order_id)
            assert order is not None
            sensitive_store(session).delete(order)
            session.commit()
        return False

    monkeypatch.setattr(
        service.order_submission.repository,
        "claim_submission",
        remove_order,
    )
    monkeypatch.setattr(
        service.order_submission.repository,
        "expire_approved",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(KeyError, match="not found"):
        service.order_submission.submit(order_id, **MUTATION)
    assert service.broker.submit_calls == 0


def test_stale_risk_snapshot_is_rebuilt_before_single_broker_send(
    make_service,
    monkeypatch,
):
    service = make_service()
    order_id = _approval_recorded(service)
    checks = 0

    class Guard:
        def __init__(self, current):
            self.current = current

        @contextmanager
        def claim_if_current(self):
            yield self.current

    class Barrier:
        def __init__(self):
            self.entries = 0

        @contextmanager
        def hold_submission(self):
            self.entries += 1
            yield Guard(self.entries > 1)

    original = service.order_submission._risk_check

    def counted_risk_check(*args, **kwargs):
        nonlocal checks
        checks += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        service.order_submission,
        "submission_barrier",
        Barrier(),
    )
    monkeypatch.setattr(
        service.order_submission,
        "_risk_check",
        counted_risk_check,
    )

    result = service.order_submission.submit(order_id, **MUTATION)

    assert result.status is OrderStatus.SUBMITTED
    assert checks == 2
    assert service.broker.submit_calls == 1


def test_unrecognized_synchronous_broker_status_is_acceptance_unknown(
    make_service,
):
    class InvalidStatusBroker(MockBroker):
        def __init__(self):
            super().__init__(prices={"AAPL": Decimal("100")})
            self.submit_calls = 0

        def submit_order(self, order):
            self.submit_calls += 1
            return OrderResult(
                idempotency_key=order.idempotency_key,
                broker_order_id="release-invalid-status",
                status=OrderStatus.PROPOSED,
                ticker=order.ticker,
            )

    broker = InvalidStatusBroker()
    service = make_service(broker=broker)
    order_id = _approval_recorded(service)

    result = service.order_submission.submit(order_id, **MUTATION)

    assert result.status is OrderStatus.ACCEPTANCE_UNKNOWN
    assert broker.submit_calls == 1
    with service.session_factory() as session:
        order = session.get(Order, order_id)
        assert order.last_error_code == "invalid_broker_submission_status"


def _rule_command(group_key: str, *, price="101") -> dict:
    return {
        "ticker": "AAPL",
        "kind": "price",
        "condition": {
            "type": "price",
            "direction": "below",
            "price": price,
        },
        "action": {
            "side": "buy",
            "order_type": "market",
            "notional": "10",
        },
        "group_key": group_key,
        "pre_approved": False,
    }


def _group_with_rule(service, key: str) -> tuple[int, int]:
    rule_id = service.rule_application.create_rule(
        _rule_command(key),
        **MUTATION,
    )
    with service.session_factory() as session:
        group_id = session.scalar(
            select(RuleGroup.id).where(RuleGroup.group_key == key)
        )
    assert group_id is not None
    return group_id, rule_id


def test_rule_repository_refuses_missing_owner_context_and_invalid_ttl(
    session_factory,
):
    with pytest.raises(ValueError, match="owner"):
        RuleRepository(session_factory, owner=" ")
    repository = RuleRepository(session_factory, owner="release-worker")
    with pytest.raises(ValueError, match="must be non-empty"):
        repository.lease_group(
            1,
            NOW,
            actor="",
            reason="reason",
            request_id="request",
        )
    with pytest.raises(ValueError, match="ttl"):
        repository.lease_group(
            1,
            NOW,
            ttl=timedelta(0),
            **MUTATION,
        )


def test_rule_loader_refuses_unknown_persisted_payload_version(
    make_service,
):
    service = make_service()
    group_id, rule_id = _group_with_rule(
        service,
        "release-unknown-payload",
    )
    repository = RuleRepository(
        service.session_factory,
        owner="release-worker",
    )
    lease = repository.lease_group(group_id, NOW, **MUTATION)
    assert lease is not None
    with service.session_factory() as session:
        session.get(Rule, rule_id).payload_version = 99
        session.commit()

    with pytest.raises(ValueError, match="unsupported payload_version"):
        repository.load_rules(lease)


def test_stale_rule_release_is_a_noop(make_service):
    service = make_service()
    group_id, _ = _group_with_rule(service, "release-stale-lease")
    repository = RuleRepository(
        service.session_factory,
        owner="release-worker",
    )
    lease = repository.lease_group(group_id, NOW, **MUTATION)
    assert lease is not None
    with service.session_factory() as session:
        session.get(RuleGroup, group_id).version += 1
        session.commit()

    assert repository.release_group(lease, now=NOW, **MUTATION) is False


def test_terminal_claim_refuses_nonterminal_state_and_wrong_winner(
    make_service,
):
    service = make_service()
    group_id, _ = _group_with_rule(service, "release-invalid-terminal")
    _, other_group_rule_id = _group_with_rule(
        service,
        "release-other-terminal-group",
    )
    repository = RuleRepository(
        service.session_factory,
        owner="release-worker",
    )
    lease = repository.lease_group(group_id, NOW, **MUTATION)
    assert lease is not None

    with pytest.raises(ValueError, match="terminal"):
        repository.claim_terminal(
            lease,
            999,
            now=NOW,
            terminal_state=RuleState.ACTIVE,
            **MUTATION,
        )
    assert repository.claim_terminal(
        lease,
        other_group_rule_id,
        now=NOW,
        **MUTATION,
    ) is False
    with service.session_factory() as session:
        group = session.get(RuleGroup, group_id)
        assert group.state == RuleState.ACTIVE.value
        assert group.terminal_rule_id is None


def test_terminal_claim_rolls_back_if_audit_cannot_commit(
    make_service,
    monkeypatch,
):
    from trading_assistant.rules import repository as repository_module

    service = make_service()
    group_id, rule_id = _group_with_rule(
        service,
        "release-terminal-audit-rollback",
    )
    repository = RuleRepository(
        service.session_factory,
        owner="release-worker",
    )
    lease = repository.lease_group(group_id, NOW, **MUTATION)
    assert lease is not None

    def fail_audit(*args, **kwargs):
        raise RuntimeError("supplementary audit failed")

    monkeypatch.setattr(repository_module, "_audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit failed"):
        repository.claim_terminal(
            lease,
            rule_id,
            now=NOW,
            **MUTATION,
        )
    with service.session_factory() as session:
        group = session.get(RuleGroup, group_id)
        rule = session.get(Rule, rule_id)
        assert group.state == RuleState.ACTIVE.value
        assert group.lease_owner == lease.owner
        assert rule.state == RuleState.ACTIVE.value


def test_rule_worker_releases_empty_group_without_outcome(
    make_service,
):
    service = make_service()
    with service.session_factory() as session:
        group = RuleGroup(group_key="release-empty-group")
        session.add(group)
        session.commit()
    worker = RuleWorker(
        service,
        RuleRepository(service.session_factory, owner="release-worker"),
        service.rule_application,
        now=lambda: NOW,
    )

    assert worker.tick(**MUTATION) == []
    with service.session_factory() as session:
        group = session.scalar(
            select(RuleGroup).where(
                RuleGroup.group_key == "release-empty-group"
            )
        )
        assert group.lease_owner is None


def test_rule_worker_isolates_notification_failure(
    make_service,
    caplog,
    monkeypatch,
):
    from trading_assistant.rules import worker as worker_module

    class FailingNotifier:
        def send(self, message):
            raise RuntimeError("notification provider unavailable")

    monkeypatch.setattr(worker_module.log, "disabled", False)
    caplog.set_level("ERROR", logger=worker_module.log.name)
    service = make_service()
    _group_with_rule(service, "release-notification-failure")
    worker = RuleWorker(
        service,
        RuleRepository(service.session_factory, owner="release-worker"),
        service.rule_application,
        notifier=FailingNotifier(),
        now=lambda: datetime.now(timezone.utc),
        max_quote_age_seconds=60,
    )

    outcomes = worker.tick(**MUTATION)

    assert len(outcomes) == 1
    assert outcomes[0].proposal is not None
    assert "code=notification_failed" in caplog.text
    assert "notification provider unavailable" not in caplog.text


def test_rule_worker_normalizes_naive_time_deadline():
    command = RuleCommand.model_validate(
        {
            "ticker": "AAPL",
            "kind": "time",
            "condition": {
                "type": "time",
                "deadline": "2026-07-26T17:59:00",
            },
            "action": {
                "side": "buy",
                "order_type": "market",
                "notional": "10",
            },
        }
    )

    fired, high_water_mark = RuleWorker._fires(
        command,
        Decimal("100"),
        NOW,
    )

    assert fired is True
    assert high_water_mark is None


def test_submission_barrier_validates_engine_and_storage(
    engine,
    session_factory,
):
    SubmissionBarrier(engine)
    with session_factory() as session:
        SubmissionBarrier(session)
    with pytest.raises(ValueError, match="bound SQLite"):
        SubmissionBarrier(sessionmaker())
    with pytest.raises(ValueError, match="file-backed"):
        SubmissionBarrier(create_engine("sqlite:///:memory:"))


def test_submission_barrier_reentrant_writer_and_nested_submit_fail_closed(
    session_factory,
):
    barrier = SubmissionBarrier(session_factory)

    with barrier.hold_writer():
        with barrier.hold_writer():
            with barrier.hold():
                assert barrier._is_owned_by_current_thread()
        with pytest.raises(RuntimeError, match="cannot be nested"):
            with barrier.hold_submission():
                pass


def test_submission_guard_reports_writer_intent_without_waiting(
    session_factory,
):
    barrier = SubmissionBarrier(session_factory)
    descriptor = barrier._open(barrier.intent_path)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        guard = SubmissionGuard(barrier)
        with guard.claim_if_current() as current:
            assert current is False
        assert guard.writer_pending is True
        assert barrier._intent_is_clear() is False
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_submission_waits_for_announced_intent_then_proceeds(
    session_factory,
):
    barrier = SubmissionBarrier(session_factory)
    descriptor = barrier._open(barrier.intent_path)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release_intent():
        try:
            import time

            time.sleep(0.05)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    release = Thread(target=release_intent)
    release.start()
    with barrier.hold_submission() as guard:
        with guard.claim_if_current() as current:
            assert current is True
    release.join(timeout=1)
    assert not release.is_alive()


def test_writer_arriving_after_snapshot_invalidates_claim_and_is_waited_for(
    session_factory,
):
    barrier = SubmissionBarrier(session_factory)

    with barrier.hold_submission() as guard:
        descriptor = barrier._open(barrier.intent_path)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with guard.claim_if_current() as current:
                assert current is False
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    assert guard.writer_pending is True
