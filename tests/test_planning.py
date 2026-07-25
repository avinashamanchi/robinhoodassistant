"""Plan lifecycle: analyze -> size -> store -> approve (decompose) -> cancel + gate."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import event, func, select

from trading_assistant.analyst.models import (
    EntryPlan,
    ExitPlan,
    ExitTarget,
    Invalidation,
    PlanAction,
    Scenario,
    TradePlan,
    Tranche,
)
from trading_assistant.analyst.planning import PlanningService
from trading_assistant.assets import AssetClass
from trading_assistant.config import Secrets, TradingMode
from trading_assistant.db.models import (
    AuditEvent,
    Proposal,
    Rule,
    RuleGroup,
    TradePlanRow,
)
from trading_assistant.risk.clock import FakeClock
from trading_assistant.rules.application import RuleApplicationService
from trading_assistant.rules.repository import RuleRepository
from trading_assistant.rules.worker import RuleWorker
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)


def _plan():
    return TradePlan(
        symbol="AAPL", as_of=TS, action=PlanAction.BUY, confidence=0.6, thesis="t",
        cited_concepts=["Trend"], regime_note="range", reference_price=Decimal("100"),
        scenarios=[
            Scenario(name="bear", price_target=Decimal("90"), horizon_days=30, probability=0.2),
            Scenario(name="base", price_target=Decimal("110"), horizon_days=30, probability=0.5),
            Scenario(name="bull", price_target=Decimal("130"), horizon_days=30, probability=0.3),
        ],
        invalidation=Invalidation(price_level=Decimal("88"), rationale="r"),
        entry_plan=EntryPlan(type="ladder", tranches=[
            Tranche(price_level=Decimal("99"), fraction=0.5),
            Tranche(price_level=Decimal("96"), fraction=0.5)]),
        exit_plan=ExitPlan(
            targets=[ExitTarget(price_level=Decimal("120"), fraction_to_sell=1.0)],
            stop=Decimal("92"), trailing_stop_pct=8.0, time_stop_days=45),
    )


class _StubAnalyst:
    def __init__(self, plan):
        self.plan = plan

    def analyze_plan(self, features, held_symbols=None, news=None):
        return self.plan


def _provider(symbol):
    return MarketFeatures(symbol=symbol, asset_class=AssetClass.EQUITY, as_of=TS,
                          last_close=100.0, regime=Regime.RANGING)


def _planning(svc):
    return PlanningService(svc, _StubAnalyst(_plan()), _provider, Secrets())


def _analyze(planning, reason):
    return planning.analyze(
        "AAPL",
        actor="operator:test",
        reason=reason,
        request_id=f"planning-{reason.replace(' ', '-')}",
    )


def _fail_audit_action(action):
    def fail(session, flush_context, instances):
        if any(
            isinstance(row, AuditEvent) and row.action == action
            for row in session.new
        ):
            raise RuntimeError(f"injected {action} audit failure")

    return fail


def test_analyze_stores_sized_plan(make_service):
    svc = make_service()
    out = _analyze(_planning(svc), "store sized plan")
    assert out["plan_id"] > 0
    assert out["sized"]["direction"] == "long"
    assert Decimal(out["sized"]["total_shares"]) > 0


def test_approve_decomposes_into_human_gated_typed_rules(make_service):
    svc = make_service()
    pln = _planning(svc)
    pid = _analyze(pln, "decompose approved plan")["plan_id"]
    res = pln.approve_plan(
        pid,
        actor="operator:test",
        reason="reviewed plan",
        request_id="planning-approve",
    )
    assert res["status"] == "approved"

    with svc.session_factory() as s:
        rules = s.execute(select(Rule).where(Rule.plan_id == pid)).scalars().all()
        kinds = sorted(r.kind for r in rules)
        assert "entry" in kinds and "target" in kinds and "stop" in kinds
        assert "trailing" in kinds and "time" in kinds
        assert all(not r.pre_approved for r in rules)
        assert all(r.payload_version == 1 for r in rules)
        assert len({r.group_id for r in rules}) == 1
        assert s.get(TradePlanRow, pid).status == "approved"


def test_plan_approval_crash_before_atomic_commit_is_retryable(make_service):
    svc = make_service()
    planning = _planning(svc)
    plan_id = _analyze(planning, "approval crash retry")["plan_id"]

    class SimulatedCrash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise SimulatedCrash("process stopped before approval commit")

    planning._decompose = crash
    with pytest.raises(SimulatedCrash):
        planning.approve_plan(
            plan_id,
            actor="operator:approval-crash",
            reason="approval crash drill",
            request_id="plan-approval-crash",
        )

    with svc.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        rule_count = session.scalar(
            select(func.count())
            .select_from(Rule)
            .where(Rule.plan_id == plan_id)
        )
    assert plan.status == "proposed"
    assert rule_count == 0


def test_plan_approval_and_all_lifecycle_audits_roll_back_together(
    make_service,
):
    svc = make_service()
    planning = _planning(svc)
    plan_id = _analyze(planning, "approval audit rollback")["plan_id"]
    listener = _fail_audit_action("plan.approve")
    session_type = svc.session_factory.class_
    event.listen(session_type, "before_flush", listener)
    try:
        with pytest.raises(
            RuntimeError,
            match="injected plan.approve audit failure",
        ):
            planning.approve_plan(
                plan_id,
                actor="operator:approval-rollback",
                reason="approval audit rollback drill",
                request_id="plan-approval-audit-rollback",
            )
    finally:
        event.remove(session_type, "before_flush", listener)

    with svc.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.request_id == "plan-approval-audit-rollback"
            )
        ).all()
    assert plan.status == "proposed"
    assert rules == []
    assert audits == []


def test_cancel_plan_cancels_rules(make_service):
    svc = make_service()
    pln = _planning(svc)
    pid = _analyze(pln, "cancel plan")["plan_id"]
    pln.approve_plan(
        pid,
        actor="operator:test",
        reason="reviewed plan",
        request_id="planning-cancel-approval",
    )
    res = pln.cancel_plan(
        pid,
        actor="operator:test",
        reason="cancel reviewed plan",
        request_id="planning-cancel",
    )
    assert res["status"] == "canceled" and res["rules_canceled"] >= 1
    with svc.session_factory() as s:
        rules = s.scalars(
            select(Rule).where(Rule.plan_id == pid)
        ).all()
        assert all(rule.state == "canceled" for rule in rules)
        group_ids = {rule.group_id for rule in rules}
        audits = s.scalars(
            select(AuditEvent).where(
                AuditEvent.request_id == "planning-cancel"
            )
        ).all()
    assert [audit.action for audit in audits].count("plan.cancel") == 1
    assert [audit.action for audit in audits].count(
        "rule_group.cancel"
    ) == len(group_ids)
    assert [audit.action for audit in audits].count(
        "rule.cancel"
    ) == len(rules)
    assert {
        (audit.actor, audit.reason, audit.request_id)
        for audit in audits
    } == {
        (
            "operator:test",
            "cancel reviewed plan",
            "planning-cancel",
        )
    }


def test_plan_cancellation_and_per_target_audits_are_one_transaction(
    make_service,
):
    svc = make_service()
    planning = _planning(svc)
    plan_id = _analyze(planning, "cancel audit rollback")["plan_id"]
    planning.approve_plan(
        plan_id,
        actor="operator:cancel-setup",
        reason="approve before cancel rollback",
        request_id="plan-cancel-rollback-setup",
    )
    with svc.session_factory() as session:
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
        group_id = rules[0].group_id
        rule_ids = [rule.id for rule in rules]

    listener = _fail_audit_action("plan.cancel")
    session_type = svc.session_factory.class_
    event.listen(session_type, "before_flush", listener)
    try:
        with pytest.raises(
            RuntimeError,
            match="injected plan.cancel audit failure",
        ):
            planning.cancel_plan(
                plan_id,
                actor="operator:cancel-rollback",
                reason="cancel audit rollback drill",
                request_id="plan-cancel-audit-rollback",
            )
    finally:
        event.remove(session_type, "before_flush", listener)

    with svc.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        group = session.get(RuleGroup, group_id)
        rules = session.scalars(
            select(Rule).where(Rule.id.in_(rule_ids))
        ).all()
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.request_id == "plan-cancel-audit-rollback"
            )
        ).all()
    assert plan.status == "approved"
    assert group.state == "active"
    assert {rule.state for rule in rules} == {"active"}
    assert audits == []


def test_plan_approval_retry_is_idempotent_and_lifecycle_audits_are_exact(
    make_service,
):
    svc = make_service()
    planning = _planning(svc)
    plan_id = _analyze(planning, "approval idempotency")["plan_id"]
    context = {
        "actor": "operator:approval-idempotency",
        "reason": "approve exactly once",
        "request_id": "plan-approval-idempotency",
    }

    first = planning.approve_plan(plan_id, **context)
    second = planning.approve_plan(plan_id, **context)

    assert first["status"] == "approved"
    assert second["status"] == "approved"
    assert "error" in second
    with svc.session_factory() as session:
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
        group_ids = {rule.group_id for rule in rules}
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.request_id == context["request_id"]
            )
        ).all()
    assert len(group_ids) == 1
    assert [audit.action for audit in audits].count("plan.approve") == 1
    assert [audit.action for audit in audits].count(
        "rule_group.create"
    ) == 1
    assert [audit.action for audit in audits].count(
        "rule.create"
    ) == len(rules)
    assert {
        (audit.actor, audit.reason, audit.request_id)
        for audit in audits
    } == {
        (
            context["actor"],
            context["reason"],
            context["request_id"],
        )
    }


def test_cancel_plan_cancels_every_resumable_member_of_mixed_group(make_service):
    svc = make_service()
    planning = _planning(svc)
    plan_id = planning.analyze(
        "AAPL",
        actor="operator:test",
        reason="planning lifecycle analysis",
        request_id="planning-lifecycle-analysis",
    )["plan_id"]
    planning.approve_plan(
        plan_id,
        actor="operator:test",
        reason="reviewed plan",
        request_id="planning-mixed-group-approval",
    )
    with svc.session_factory() as session:
        plan_rule = session.scalar(
            select(Rule).where(Rule.plan_id == plan_id).limit(1)
        )
        group_id = plan_rule.group_id
        session.add(
            Rule(
                group_id=group_id,
                payload_version=1,
                ticker="MSFT",
                condition_json=(
                    '{"direction":"above","price":"500","type":"price"}'
                ),
                action_json=(
                    '{"notional":"100","order_type":"market","side":"buy"}'
                ),
                state="processing",
                plan_id=None,
                kind="price",
                pre_approved=False,
            )
        )
        session.commit()
        expected_count = session.scalar(
            select(func.count())
            .select_from(Rule)
            .where(Rule.group_id == group_id)
        )

    result = planning.cancel_plan(
        plan_id,
        actor="operator:test",
        reason="cancel mixed plan group",
        request_id="planning-mixed-group-cancel",
    )

    assert result["status"] == "canceled"
    assert result["rules_canceled"] == expected_count
    with svc.session_factory() as session:
        group = session.get(RuleGroup, group_id)
        states = session.scalars(
            select(Rule.state).where(Rule.group_id == group_id)
        ).all()
    assert group.state == "canceled"
    assert states and set(states) == {"canceled"}


def test_worker_and_plan_cancellation_commit_one_coherent_group_state(make_service):
    svc = make_service()
    planning = _planning(svc)
    plan_id = planning.analyze(
        "AAPL",
        actor="operator:test",
        reason="planning race analysis",
        request_id="planning-race-analysis",
    )["plan_id"]
    planning.approve_plan(
        plan_id,
        actor="operator:test",
        reason="reviewed plan",
        request_id="planning-race-approval",
    )
    svc.broker.set_price("AAPL", Decimal("80"))
    barrier = Barrier(2)

    def run_worker():
        repository = RuleRepository(svc.session_factory, owner="race-worker")
        worker = RuleWorker(
            svc,
            repository,
            RuleApplicationService(svc, repository),
            max_quote_age_seconds=10**9,
        )
        barrier.wait(timeout=2)
        return worker.tick(
            actor="daemon:test-worker",
            reason="planning worker race",
            request_id="planning-worker-race",
        )

    def cancel_plan():
        barrier.wait(timeout=2)
        return planning.cancel_plan(
            plan_id,
            actor="operator:test",
            reason="cancel plan during worker race",
            request_id="planning-race-cancel",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker_future = pool.submit(run_worker)
        cancel_future = pool.submit(cancel_plan)
        worker_outcomes = worker_future.result(timeout=10)
        cancel_result = cancel_future.result(timeout=10)

    with svc.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        group = session.scalar(
            select(RuleGroup)
            .join(Rule, Rule.group_id == RuleGroup.id)
            .where(Rule.plan_id == plan_id)
            .distinct()
        )
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
        proposal_count = session.scalar(
            select(func.count())
            .select_from(Proposal)
            .where(Proposal.source_rule_group_id == group.id)
        )

    assert svc.broker.submit_calls == 0
    assert group.lease_owner is None
    assert group.lease_expires_at is None
    if group.state == "canceled":
        assert cancel_result["status"] == "canceled"
        assert "error" not in cancel_result
        assert plan.status == "canceled"
        assert group.terminal_rule_id is None
        assert all(rule.state == "canceled" for rule in rules)
        assert proposal_count == 0
        assert not worker_outcomes or worker_outcomes[0].error == "lease_conflict"
    else:
        assert group.state in {"triggered", "failed"}
        assert cancel_result["error"] == "group_conflict"
        assert plan.status == "approved"
        assert group.terminal_rule_id is not None
        winner = next(rule for rule in rules if rule.id == group.terminal_rule_id)
        assert winner.state == group.state
        assert all(
            rule.state == "canceled"
            for rule in rules
            if rule.id != group.terminal_rule_id
        )
        assert proposal_count == 1
        assert len(worker_outcomes) == 1


def test_promotion_gate_blocks_live_without_track_record(make_service, app_config, session_factory):
    from trading_assistant.broker.mock import MockBroker
    from trading_assistant.service import TradingService

    live_cfg = app_config.model_copy(update={
        "trading": app_config.trading.model_copy(update={"mode": TradingMode.LIVE})})
    broker = MockBroker()
    broker.set_price("AAPL", Decimal("100"))
    svc_live = TradingService(broker, session_factory, live_cfg, FakeClock(is_open=True))
    sec = Secrets(live_trading_confirm="I_UNDERSTAND_LIVE_TRADING")
    pln = PlanningService(svc_live, _StubAnalyst(_plan()), _provider, sec)

    pid = _analyze(pln, "promotion gate")["plan_id"]
    res = pln.approve_plan(
        pid,
        actor="operator:test",
        reason="reviewed plan",
        request_id="planning-promotion-gate",
    )  # 0 graded calls -> gate blocks live approval
    assert "promotion gate" in res["error"]
