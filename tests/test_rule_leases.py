from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trading_assistant.assets import AssetClass
from trading_assistant.db.models import (
    AuditEvent,
    Order,
    Proposal,
    Rule,
    RuleGroup,
)
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.rules.application import RuleApplicationService
from trading_assistant.rules.models import RuleCommand
from trading_assistant.rules.repository import RuleRepository
from trading_assistant.rules.worker import RuleWorker
from trading_assistant.security.sensitive_fields import persist_sensitive
from tests.conftest import decrypt_test_sensitive

NOW = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
DEFAULT_RULE_CONTEXT = {
    "actor": "daemon:test-rule-worker",
    "reason": "test conditional rule lifecycle",
    "request_id": "test-rule-lifecycle",
}


def _command(
    *,
    group_key: str = "oco-aapl",
    direction: str = "below",
    price: str = "175",
    pre_approved: bool = False,
) -> RuleCommand:
    return RuleCommand.model_validate(
        {
            "ticker": "AAPL",
            "kind": "price",
            "condition": {
                "type": "price",
                "direction": direction,
                "price": price,
            },
            "action": {
                "side": "buy",
                "order_type": "market",
                "notional": "100",
            },
            "group_key": group_key,
            "pre_approved": pre_approved,
        }
    )


def _seed_group(session_factory, *commands: RuleCommand) -> tuple[int, list[int]]:
    assert commands
    with session_factory() as session:
        group = RuleGroup(group_key=commands[0].group_key)
        session.add(group)
        session.flush()
        rule_ids = []
        for command in commands:
            dumped = command.model_dump(mode="json")
            rule = Rule(
                group_id=group.id,
                payload_version=1,
                ticker=command.ticker,
                kind=command.kind.value,
                condition_json=json.dumps(dumped["condition"]),
                action_json=json.dumps(dumped["action"]),
                state="active",
                pre_approved=command.pre_approved,
                fraction=command.fraction,
                hwm=command.high_water_mark,
            )
            session.add(rule)
            session.flush()
            rule_ids.append(rule.id)
        session.commit()
        return group.id, rule_ids


@pytest.fixture
def seeded_oco_group(session_factory):
    group_id, _ = _seed_group(
        session_factory,
        _command(price="175"),
        _command(direction="above", price="50"),
    )
    return group_id


def test_two_workers_cannot_lease_sibling_rules(session_factory, seeded_oco_group):
    repo_a = RuleRepository(session_factory, owner="worker-a")
    repo_b = RuleRepository(session_factory, owner="worker-b")

    assert repo_a.lease_group(
        seeded_oco_group, now=NOW, **DEFAULT_RULE_CONTEXT
    ) is not None
    assert repo_b.lease_group(
        seeded_oco_group, now=NOW, **DEFAULT_RULE_CONTEXT
    ) is None


def test_expired_lease_can_be_recovered_with_a_new_owner(
    session_factory, seeded_oco_group
):
    first = RuleRepository(session_factory, owner="worker-a")
    second = RuleRepository(session_factory, owner="worker-b")
    lease = first.lease_group(
        seeded_oco_group,
        now=NOW,
        ttl=timedelta(seconds=30),
        **DEFAULT_RULE_CONTEXT,
    )

    assert lease is not None
    recovered = second.lease_group(
        seeded_oco_group,
        now=NOW + timedelta(seconds=31),
        **DEFAULT_RULE_CONTEXT,
    )
    assert recovered is not None
    assert recovered.owner == "worker-b"
    assert recovered.version > lease.version


def test_group_reuse_requires_homogeneous_plan_ownership(make_service):
    svc = make_service()
    command = _command(group_key="plan-owned-group")

    svc.rule_application.create_rule(
        command, plan_id=7, **DEFAULT_RULE_CONTEXT
    )
    svc.rule_application.create_rule(
        command, plan_id=7, **DEFAULT_RULE_CONTEXT
    )

    with pytest.raises(ValueError, match="plan ownership"):
        svc.rule_application.create_rule(
            command, plan_id=8, **DEFAULT_RULE_CONTEXT
        )

    with svc.session_factory() as session:
        rules = session.scalars(
            select(Rule).order_by(Rule.id)
        ).all()
    assert len(rules) == 2
    assert {rule.plan_id for rule in rules} == {7}
    assert len({rule.group_id for rule in rules}) == 1


def test_claim_terminal_is_owner_version_guarded_and_cancels_siblings(
    session_factory, seeded_oco_group
):
    repo = RuleRepository(session_factory, owner="worker-a")
    lease = repo.lease_group(
        seeded_oco_group, now=NOW, **DEFAULT_RULE_CONTEXT
    )
    assert lease is not None
    with session_factory() as session:
        winner = session.scalar(
            select(Rule.id)
            .where(Rule.group_id == seeded_oco_group)
            .order_by(Rule.id)
        )

    assert repo.claim_terminal(
        lease, winner, now=NOW, **DEFAULT_RULE_CONTEXT
    ) is True
    assert repo.claim_terminal(
        lease, winner, now=NOW, **DEFAULT_RULE_CONTEXT
    ) is False

    with session_factory() as session:
        group = session.get(RuleGroup, seeded_oco_group)
        states = session.execute(
            select(Rule.id, Rule.state).where(Rule.group_id == seeded_oco_group)
        ).all()
    assert group.state == "triggered"
    assert group.terminal_rule_id == winner
    assert group.lease_owner is None and group.lease_expires_at is None
    assert dict(states)[winner] == "triggered"
    assert sorted(dict(states).values()) == ["canceled", "triggered"]


def test_rule_lease_and_terminal_mutations_have_exact_per_target_audits(
    session_factory,
    seeded_oco_group,
):
    repository = RuleRepository(
        session_factory,
        owner="audited-worker",
    )
    context = {
        "actor": "daemon:audited-worker",
        "reason": "evaluate audited rule group",
        "request_id": "rule-lifecycle-audit",
    }
    lease = repository.lease_group(
        seeded_oco_group,
        now=NOW,
        **context,
    )
    assert lease is not None
    with session_factory() as session:
        rule_ids = session.scalars(
            select(Rule.id)
            .where(Rule.group_id == seeded_oco_group)
            .order_by(Rule.id)
        ).all()

    assert repository.claim_terminal(
        lease,
        rule_ids[0],
        now=NOW,
        **context,
    ) is True

    with session_factory() as session:
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.request_id == context["request_id"]
            )
        ).all()
    assert {
        (audit.action, audit.target_type, audit.target_id)
        for audit in audits
    } >= {
        (
            "rule_group.lease",
            "rule_group",
            str(seeded_oco_group),
        ),
        (
            "rule_group.terminal",
            "rule_group",
            str(seeded_oco_group),
        ),
        ("rule.terminal", "rule", str(rule_ids[0])),
        ("rule.cancel", "rule", str(rule_ids[1])),
    }
    assert {
        (
            audit.actor,
            decrypt_test_sensitive(audit, "reason"),
            audit.request_id,
        )
        for audit in audits
    } == {
        (
            context["actor"],
            context["reason"],
            context["request_id"],
        )
    }


def test_claim_terminal_rejects_unpersistable_runtime_hwm_before_transition(
    session_factory,
):
    command = _command(group_key="runtime-hwm-terminal")
    group_id, (rule_id,) = _seed_group(session_factory, command)
    repo = RuleRepository(session_factory, owner="worker-a")
    lease = repo.lease_group(
        group_id, now=NOW, **DEFAULT_RULE_CONTEXT
    )
    assert lease is not None

    with pytest.raises(ValidationError):
        repo.claim_terminal(
            lease,
            rule_id,
            now=NOW,
            high_water_mark=Decimal("0.0000001"),
            **DEFAULT_RULE_CONTEXT,
        )

    with session_factory() as session:
        group = session.get(RuleGroup, group_id)
        rule = session.get(Rule, rule_id)
        assert group.state == "active"
        assert group.terminal_rule_id is None
        assert group.lease_owner == lease.owner
        assert group.version == lease.version
        assert rule.state == "active"
        assert rule.hwm is None


def test_stale_lease_cannot_create_a_proposal(make_service):
    svc = make_service()
    command = _command()
    group_id, (rule_id,) = _seed_group(svc.session_factory, command)
    repo = RuleRepository(svc.session_factory, owner="worker-a")
    stale = repo.lease_group(
        group_id, now=NOW, **DEFAULT_RULE_CONTEXT
    )
    assert stale is not None
    with svc.session_factory() as session:
        session.get(RuleGroup, group_id).version += 1
        session.commit()

    outcome = RuleApplicationService(svc, repo).propose_from_lease(
        stale,
        rule_id,
        command,
        actor="daemon:worker-a",
        reason="stale lease evaluation",
        request_id="stale-lease-evaluation",
        now=NOW,
    )

    assert outcome.proposal is None
    assert outcome.error == "lease_conflict"
    with svc.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 0
        assert session.get(Rule, rule_id).state == "active"


def test_proposal_winner_and_sibling_cancellation_commit_atomically(make_service):
    svc = make_service()
    winner_command = _command(price="175")
    group_id, rule_ids = _seed_group(
        svc.session_factory,
        winner_command,
        _command(direction="above", price="50"),
    )
    repo = RuleRepository(svc.session_factory, owner="worker-a")
    lease = repo.lease_group(
        group_id, now=NOW, **DEFAULT_RULE_CONTEXT
    )
    assert lease is not None

    outcome = RuleApplicationService(svc, repo).propose_from_lease(
        lease,
        rule_ids[0],
        winner_command,
        actor="daemon:worker-a",
        reason="winning lease evaluation",
        request_id="winning-lease-evaluation",
        now=NOW,
    )

    assert outcome.proposal["status"] == "proposed"
    assert outcome.executed is None
    assert outcome.oco_canceled == 1
    assert svc.broker.submit_calls == 0
    with svc.session_factory() as session:
        group = session.get(RuleGroup, group_id)
        proposals = session.scalars(
            select(Proposal).where(Proposal.source_rule_group_id == group_id)
        ).all()
        states = {
            rule.id: rule.state
            for rule in session.scalars(
                select(Rule).where(Rule.group_id == group_id)
            )
        }
    assert group.terminal_rule_id == rule_ids[0]
    assert len(proposals) == 1
    assert states == {rule_ids[0]: "triggered", rule_ids[1]: "canceled"}


def test_rejected_rule_proposal_audit_preserves_operation_reason(
    make_service,
):
    service = make_service()
    command = _command(group_key="rejected-provenance")
    group_id, (rule_id,) = _seed_group(service.session_factory, command)
    service.breakers.trip(
        BreakerScope.data(AssetClass.EQUITY),
        "feed disagreement",
        "daemon:risk",
        request_id="rejected-rule-breaker",
    )
    repository = RuleRepository(
        service.session_factory,
        owner="rejected-worker",
    )
    lease = repository.lease_group(
        group_id,
        now=NOW,
        actor="daemon:rejected-worker",
        reason="evaluate rejected conditional rule",
        request_id="rejected-rule-provenance",
    )
    assert lease is not None

    outcome = RuleApplicationService(
        service,
        repository,
    ).propose_from_lease(
        lease,
        rule_id,
        command,
        actor="daemon:rejected-worker",
        reason="evaluate rejected conditional rule",
        request_id="rejected-rule-provenance",
        now=NOW,
    )

    assert outcome.proposal["status"] == OrderStatus.REJECTED.value
    with service.session_factory() as session:
        proposal_audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "order.propose",
                AuditEvent.request_id == "rejected-rule-provenance",
            )
        )
    assert proposal_audit.actor == "daemon:rejected-worker"
    assert decrypt_test_sensitive(
        proposal_audit,
        "reason",
    ) == "evaluate rejected conditional rule"
    assert "circuit breaker" not in decrypt_test_sensitive(
        proposal_audit,
        "reason",
    )


def test_reconciliation_required_blocks_expired_recovery_until_client_id_truth(
    make_service,
):
    broker = MockBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    command = _command()
    group_id, _ = _seed_group(svc.session_factory, command)
    with svc.session_factory() as session:
        group = session.get(RuleGroup, group_id)
        group.lease_owner = "dead-worker"
        group.lease_expires_at = NOW - timedelta(seconds=1)
        order = Order(
            idempotency_key="rule-acceptance-unknown",
            ticker="AAPL",
            side="buy",
            order_type="market",
            notional=Decimal("100"),
            status=OrderStatus.ACCEPTANCE_UNKNOWN.value,
        )
        persist_sensitive(
            session,
            order,
            {"approval_reason": "rule reconciliation fixture"},
        )
        persist_sensitive(
            session,
            Proposal(
                order_id=order.id,
                source_rule_group_id=group_id,
                expires_at=NOW + timedelta(minutes=15),
            ),
            {"reasoning": "rule acceptance unknown fixture"},
        )
        session.commit()

    recovering = RuleRepository(svc.session_factory, owner="new-worker")
    assert recovering.lease_group(
        group_id, now=NOW, **DEFAULT_RULE_CONTEXT
    ) is None
    with svc.session_factory() as session:
        assert session.get(RuleGroup, group_id).reconciliation_required is True
    assert svc.reconciliation.reconcile_unknown(
        actor="test:rule-leases",
        reason="rule lease unresolved acceptance",
        request_id="rule-lease-unresolved",
    ) == (0, (1,))
    assert recovering.lease_group(
        group_id, now=NOW, **DEFAULT_RULE_CONTEXT
    ) is None

    broker.submit_order(
        OrderRequest(
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            idempotency_key="rule-acceptance-unknown",
            notional=Decimal("100"),
        )
    )
    assert svc.reconciliation.reconcile_unknown(
        actor="test:rule-leases",
        reason="rule lease acceptance recovery",
        request_id="rule-lease-recovery",
    ) == (1, ())
    assert recovering.lease_group(
        group_id, now=NOW, **DEFAULT_RULE_CONTEXT
    ) is not None
    with svc.session_factory() as session:
        assert session.get(RuleGroup, group_id).reconciliation_required is False


def test_unknown_acceptance_marks_group_until_reconciliation_resolves_client_id(
    make_service,
):
    class AcceptThenDisconnectBroker(MockBroker):
        def submit_order(self, order):
            super().submit_order(order)
            raise ConnectionError("response lost after acceptance")

    broker = AcceptThenDisconnectBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc = make_service(broker=broker)
    command = _command(group_key="unknown-acceptance")
    group_id, (rule_id,) = _seed_group(svc.session_factory, command)
    repo = RuleRepository(svc.session_factory, owner="worker-a")
    lease = repo.lease_group(
        group_id, now=NOW, **DEFAULT_RULE_CONTEXT
    )
    outcome = RuleApplicationService(svc, repo).propose_from_lease(
        lease,
        rule_id,
        command,
        actor="daemon:worker-a",
        reason="unknown acceptance evaluation",
        request_id="unknown-acceptance-evaluation",
        now=NOW,
    )
    with svc.session_factory() as session:
        proposal = session.scalar(
            select(Proposal).where(
                Proposal.order_id == outcome.proposal["order_id"]
            )
        )
        proposal.expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        session.commit()

    submitted = svc.approve_order(
        outcome.proposal["order_id"],
        actor="operator:test",
        reason="explicit human approval",
        request_id="rule-lease-human-approval",
    )

    assert submitted["status"] == "acceptance_unknown"
    with svc.session_factory() as session:
        assert session.get(RuleGroup, group_id).reconciliation_required is True

    assert svc.reconciliation.reconcile_unknown(
        actor="test:rule-leases",
        reason="rule group acceptance recovery",
        request_id="rule-group-recovery",
    ) == (1, ())
    with svc.session_factory() as session:
        assert session.get(RuleGroup, group_id).reconciliation_required is False


@pytest.mark.parametrize("crash_phase", ["before_transaction", "after_transaction"])
def test_crash_immediately_before_or_after_transaction_creates_at_most_one_proposal(
    make_service, crash_phase
):
    svc = make_service()
    command = _command(group_key=f"crash-{crash_phase}")
    group_id, (rule_id,) = _seed_group(svc.session_factory, command)
    repo = RuleRepository(svc.session_factory, owner="worker-a")
    lease = repo.lease_group(
        group_id, now=NOW, **DEFAULT_RULE_CONTEXT
    )
    assert lease is not None

    def crash(phase):
        if phase == crash_phase:
            raise RuntimeError(f"crash at {phase}")

    application = RuleApplicationService(svc, repo, crash_hook=crash)
    with pytest.raises(RuntimeError, match="crash at"):
        application.propose_from_lease(
            lease,
            rule_id,
            command,
            actor="daemon:worker-a",
            reason="crash phase evaluation",
            request_id=f"crash-phase-{crash_phase}",
            now=NOW,
        )

    restart_repo = RuleRepository(svc.session_factory, owner="worker-restart")
    recovered = restart_repo.lease_group(
        group_id,
        now=NOW + timedelta(seconds=31),
        **DEFAULT_RULE_CONTEXT,
    )
    if recovered is not None:
        RuleApplicationService(svc, restart_repo).propose_from_lease(
            recovered,
            rule_id,
            command,
            actor="daemon:worker-restart",
            reason="crash recovery evaluation",
            request_id=f"crash-recovery-{crash_phase}",
            now=NOW + timedelta(seconds=31),
        )

    with svc.session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Proposal)
                .where(Proposal.source_rule_group_id == group_id)
            )
            == 1
        )


def test_two_thread_sibling_trigger_records_one_proposal_and_one_terminal_rule(
    make_service,
):
    svc = make_service(quote_now=lambda: NOW)
    svc.snapshot_service.now = lambda: NOW
    command_a = _command(price="175")
    command_b = _command(direction="above", price="50")
    group_id, _ = _seed_group(svc.session_factory, command_a, command_b)
    barrier = Barrier(2)

    def run(owner):
        repo = RuleRepository(svc.session_factory, owner=owner)
        worker = RuleWorker(
            svc,
            repo,
            RuleApplicationService(svc, repo),
            max_quote_age_seconds=10**9,
            now=lambda: NOW,
        )
        barrier.wait(timeout=2)
        return worker.tick(
            actor=f"daemon:{owner}",
            reason="rule lease race evaluation",
            request_id=f"rule-lease-race-{owner}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("worker-a", "worker-b")))

    assert sum(len(result) for result in results) == 1
    with svc.session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Proposal)
                .where(Proposal.source_rule_group_id == group_id)
            )
            == 1
        )
        states = session.scalars(
            select(Rule.state).where(Rule.group_id == group_id)
        ).all()
    assert states.count("triggered") == 1
    assert states.count("canceled") == 1
    assert svc.broker.submit_calls == 0
