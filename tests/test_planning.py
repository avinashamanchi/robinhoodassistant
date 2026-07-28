"""Plan lifecycle: analyze -> size -> store -> approve (decompose) -> cancel + gate."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from trading_assistant.analyst.analyst import Analyst
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
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    BrokerFill,
    OrderResult,
    OrderStatus,
    Position,
)
from trading_assistant.config import Secrets, TradingMode
from trading_assistant.db.models import (
    AuditEvent,
    FILL_RECONCILIATION_REQUIRED,
    Fill,
    Order,
    PLAN_CANCEL_INDETERMINATE,
    PLAN_CANCEL_REQUESTED,
    PLAN_CANCEL_SETTLED,
    Proposal,
    ProviderBudgetDay,
    ProviderReservation,
    Rule,
    RuleGroup,
    TradePlanRow,
)
from trading_assistant.dependencies import RequiredDependencyUnavailable
from trading_assistant.llm.base import BudgetedLLMBackend
from trading_assistant.llm.budget import (
    BudgetLimits,
    ProviderBudgetService,
    Utf8ByteUpperBoundEstimator,
)
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.risk.clock import FakeClock
from trading_assistant.rules.application import RuleApplicationService
from trading_assistant.rules.repository import RuleRepository
from trading_assistant.rules.worker import RuleWorker
from trading_assistant.security.sensitive_fields import (
    persist_sensitive,
    sensitive_store,
)
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
        self.request_ids: list[str | None] = []

    def analyze_plan(
        self,
        features,
        held_symbols=None,
        untrusted_summary=None,
        request_id=None,
    ):
        self.request_ids.append(request_id)
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


def _approve(planning, plan_id, *, review_token=None, **context):
    if review_token is None:
        detail = planning.get_plan(plan_id)
        assert detail is not None
        review_token = detail["review_token"]
    return planning.approve_plan(
        plan_id,
        review_token=review_token,
        **context,
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


def test_analyze_returns_persisted_immutable_review_authority(make_service):
    planning = _planning(make_service())

    out = _analyze(planning, "return immutable review authority")
    detail = planning.get_plan(out["plan_id"])

    assert out["authority_version"] == 1
    assert len(out["authority_digest"]) == 64
    assert out["review_token"]
    assert detail is not None
    assert detail["authority_version"] == out["authority_version"]
    assert detail["authority_digest"] == out["authority_digest"]
    assert detail["review_token"] == out["review_token"]


def test_approval_rejects_authoritative_payload_mutation_without_rules(
    make_service,
):
    service = make_service()
    planning = _planning(service)
    review = _analyze(planning, "bind reviewed authority")
    plan_id = review["plan_id"]

    with service.session_factory() as session:
        row = session.get(TradePlanRow, plan_id)
        store = sensitive_store(session, service.session_factory)
        payload = json.loads(store.read(row, "plan_json"))
        payload["entry_plan"]["tranches"][0]["price_level"] = "77"
        store.write_many(
            row,
            {
                "plan_json": json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )
        session.commit()

    result = _approve(planning,
        plan_id,
        review_token=review["review_token"],
        actor="operator:test",
        reason="approve exact reviewed authority",
        request_id="planning-authority-stale",
    )

    assert result["error"] == "plan_review_stale"
    with service.session_factory() as session:
        assert session.get(TradePlanRow, plan_id).status == "proposed"
        assert session.scalar(
            select(func.count())
            .select_from(Rule)
            .where(Rule.plan_id == plan_id)
        ) == 0


def test_approval_rechecks_payload_after_review_before_atomic_claim(
    make_service,
):
    service = make_service()
    planning = _planning(service)
    review = _analyze(planning, "race reviewed authority")
    plan_id = review["plan_id"]
    original_decompose = planning._decompose

    def mutate_after_initial_review(plan, sized, supplied_plan_id):
        with service.session_factory() as session:
            row = session.get(TradePlanRow, supplied_plan_id)
            store = sensitive_store(session, service.session_factory)
            payload = json.loads(store.read(row, "sized_json"))
            payload["tranches"][0]["shares"] = "999"
            store.write_many(
                row,
                {
                    "sized_json": json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                },
            )
            session.commit()
        return original_decompose(plan, sized, supplied_plan_id)

    planning._decompose = mutate_after_initial_review
    result = _approve(
        planning,
        plan_id,
        review_token=review["review_token"],
        actor="operator:test",
        reason="reject raced reviewed authority",
        request_id="planning-authority-race",
    )

    assert result["error"] == "plan_review_stale"
    with service.session_factory() as session:
        assert session.get(TradePlanRow, plan_id).status == "proposed"
        assert session.scalar(
            select(func.count())
            .select_from(Rule)
            .where(Rule.plan_id == plan_id)
        ) == 0
        assert session.scalar(select(func.count()).select_from(Order)) == 0


def test_approval_maps_malformed_authority_payload_to_stale_review(
    make_service,
):
    service = make_service()
    planning = _planning(service)
    review = _analyze(planning, "malformed reviewed authority")
    plan_id = review["plan_id"]

    with service.session_factory() as session:
        row = session.get(TradePlanRow, plan_id)
        store = sensitive_store(session, service.session_factory)
        payload = json.loads(store.read(row, "sized_json"))
        payload["total_shares"] = "not-a-number"
        store.write_many(
            row,
            {
                "sized_json": json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )
        session.commit()

    result = _approve(
        planning,
        plan_id,
        review_token=review["review_token"],
        actor="operator:test",
        reason="reject malformed reviewed authority",
        request_id="planning-authority-malformed",
    )

    assert result["error"] == "plan_review_stale"
    with service.session_factory() as session:
        assert session.get(TradePlanRow, plan_id).status == "proposed"
        assert session.scalar(
            select(func.count())
            .select_from(Rule)
            .where(Rule.plan_id == plan_id)
        ) == 0


def test_non_authoritative_narrative_edit_preserves_review_authority(
    make_service,
):
    service = make_service()
    planning = _planning(service)
    review = _analyze(planning, "narrative is non authoritative")
    plan_id = review["plan_id"]

    with service.session_factory() as session:
        row = session.get(TradePlanRow, plan_id)
        store = sensitive_store(session, service.session_factory)
        payload = json.loads(store.read(row, "plan_json"))
        payload["thesis"] = "changed narrative must not gain authority"
        store.write_many(
            row,
            {
                "plan_json": json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )
        session.commit()

    result = _approve(planning,
        plan_id,
        review_token=review["review_token"],
        actor="operator:test",
        reason="approve unchanged authority",
        request_id="planning-authority-narrative",
    )

    assert result["status"] == "approved"


def test_planning_passes_boundary_request_id_to_each_structured_attempt(
    make_service,
):
    service = make_service()
    analyst = _StubAnalyst(_plan())
    planning = PlanningService(
        service,
        analyst,
        _provider,
        Secrets(),
    )

    planning.analyze(
        "AAPL",
        actor="operator:test",
        reason="propagate request identity",
        request_id="  planning-budget-request  ",
    )

    assert analyst.request_ids == ["planning-budget-request"]


@pytest.mark.parametrize(
    "request_id",
    [
        None,
        "",
        " ",
        "a" * 65,
        "request id",
        "request\nid",
        "reque\u0301st",
        "request-😀",
    ],
    ids=[
        "non-string",
        "empty",
        "blank",
        "too-long",
        "internal-space",
        "control",
        "nfd-unicode",
        "emoji",
    ],
)
def test_planning_rejects_noncanonical_request_id_before_feature_or_analyst(
    make_service,
    request_id,
):
    feature_calls: list[str] = []
    analyst = _StubAnalyst(_plan())
    planning = PlanningService(
        make_service(),
        analyst,
        lambda symbol: feature_calls.append(symbol),
        Secrets(),
    )

    with pytest.raises(ValueError, match="request_id"):
        planning.analyze(
            "AAPL",
            actor="operator:test",
            reason="reject invalid planning identity",
            request_id=request_id,
        )

    assert feature_calls == []
    assert analyst.request_ids == []


@pytest.mark.parametrize("method", ["approve_plan", "cancel_plan"])
@pytest.mark.parametrize(
    "request_id",
    ["a" * 65, "request id", "request\nid", "reque\u0301st"],
    ids=["too-long", "internal-space", "control", "nfd-unicode"],
)
def test_plan_mutation_boundaries_reject_noncanonical_request_id_before_lookup(
    make_service,
    method,
    request_id,
):
    planning = _planning(make_service())
    kwargs = {
        "actor": "operator:test",
        "reason": "reject invalid plan mutation identity",
        "request_id": request_id,
    }
    if method == "approve_plan":
        kwargs["review_token"] = "invalid-review-token"

    with pytest.raises(ValueError, match="request_id"):
        getattr(planning, method)(
            999_999,
            **kwargs,
        )


def test_planning_accepts_64_character_request_id(make_service):
    request_id = "planning:" + ("p" * 55)
    analyst = _StubAnalyst(_plan())
    planning = PlanningService(
        make_service(),
        analyst,
        _provider,
        Secrets(),
    )

    planning.analyze(
        "AAPL",
        actor="operator:test",
        reason="accept maximum planning identity",
        request_id=request_id,
    )

    assert len(request_id) == 64
    assert analyst.request_ids == [request_id]


def test_planning_repair_attempts_share_parent_id_but_reserve_separately(
    make_service,
):
    class RepairBackend:
        def __init__(self):
            plan_input = _plan().model_dump(
                mode="json",
                exclude={"symbol", "as_of", "reference_price"},
            )
            self.responses = [
                SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="invalid")],
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                ),
                SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="tool_use",
                            name="submit_plan",
                            input=plan_input,
                        )
                    ],
                    usage=SimpleNamespace(input_tokens=2, output_tokens=2),
                ),
            ]
            self.request_ids: list[str] = []

        def create(self, **kwargs):
            self.request_ids.append(kwargs["request_id"])
            return self.responses.pop(0)

    service = make_service()
    delegate = RepairBackend()
    budget = ProviderBudgetService(
        service.session_factory,
        BudgetLimits(
            calls=10,
            input_tokens=100_000,
            output_tokens=10_000,
        ),
    )
    backend = BudgetedLLMBackend(
        delegate,
        budget,
        provider="test",
        category="analysis",
        max_output_tokens=100,
        estimator=Utf8ByteUpperBoundEstimator(),
    )
    planning = PlanningService(
        service,
        Analyst(backend, max_attempts=2),
        _provider,
        Secrets(),
    )

    planning.analyze(
        "AAPL",
        actor="operator:test",
        reason="repair invalid structured plan",
        request_id="planning-repair-parent",
    )

    assert delegate.request_ids == [
        "planning-repair-parent",
        "planning-repair-parent",
    ]
    with service.session_factory() as session:
        reservations = session.scalars(
            select(ProviderReservation).where(
                ProviderReservation.request_id == "planning-repair-parent"
            )
        ).all()
        day = session.scalar(
            select(ProviderBudgetDay).where(
                ProviderBudgetDay.provider == "test"
            )
        )
    assert len(reservations) == 2
    assert len({row.reservation_id for row in reservations}) == 2
    assert {row.state for row in reservations} == {"settled"}
    assert day.calls_used == 2


def test_live_feature_provider_types_primary_market_data_outage(
    app_config,
    monkeypatch,
):
    from trading_assistant.analyst import live_features

    marker = "provider-market-data-secret"

    def fail_market_data(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(
        live_features,
        "_fetch_equity_df",
        fail_market_data,
    )
    provider = live_features.build_live_feature_provider(
        app_config,
        Secrets(),
    )

    with pytest.raises(RequiredDependencyUnavailable) as failure:
        provider("AAPL")

    assert marker not in str(failure.value)


def test_planning_types_required_snapshot_provider_outage(make_service):
    marker = "provider-account-secret"

    class AccountOutageBroker(MockBroker):
        def get_account(self):
            raise RuntimeError(marker)

    planning = _planning(make_service(broker=AccountOutageBroker()))

    with pytest.raises(RequiredDependencyUnavailable) as failure:
        _analyze(planning, "required snapshot provider outage")

    assert marker not in str(failure.value)


def test_approve_decomposes_into_human_gated_typed_rules(make_service):
    svc = make_service()
    pln = _planning(svc)
    pid = _analyze(pln, "decompose approved plan")["plan_id"]
    res = _approve(pln,
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
        entries = [rule for rule in rules if rule.kind == "entry"]
        exits = [rule for rule in rules if rule.kind != "entry"]
        assert len({rule.group_id for rule in entries}) == len(entries)
        assert len({rule.group_id for rule in exits}) == 1
        assert not (
            {rule.group_id for rule in entries}
            & {rule.group_id for rule in exits}
        )
        assert {rule.state for rule in entries} == {"active"}
        assert {rule.state for rule in exits} == {"pending"}
        assert {rule.activation for rule in entries} == {"immediate"}
        assert {rule.activation for rule in exits} == {"on_entry_fill"}
        exit_group = s.get(RuleGroup, exits[0].group_id)
        assert exit_group.state == "pending"
        assert s.get(TradePlanRow, pid).status == "approved"


class _FillAwareBroker(MockBroker):
    def __init__(self) -> None:
        super().__init__()
        self.activities: list[BrokerFill] = []
        self.sides_by_broker_id: dict[str, str] = {}

    def get_fill_activities(self, *, after=None):
        return list(self.activities)

    def submit_order(self, order):
        result = super().submit_order(order)
        assert result.broker_order_id is not None
        self.sides_by_broker_id[result.broker_order_id] = (
            order.side.value
        )
        return result

    def fill_order(
        self,
        broker_order_id: str,
        *,
        qty: Decimal,
        price: Decimal,
        side: str | None = None,
    ) -> None:
        current = self._orders_by_id[broker_order_id]
        side = side or self.sides_by_broker_id[broker_order_id]
        filled = OrderResult(
            idempotency_key=current.idempotency_key,
            broker_order_id=broker_order_id,
            status=OrderStatus.FILLED,
            filled_qty=qty,
            avg_fill_price=price,
            ticker=current.ticker,
        )
        self._orders_by_id[broker_order_id] = filled
        self._orders_by_key[current.idempotency_key] = filled
        self.activities.append(
            BrokerFill(
                broker_fill_id=f"fill-{broker_order_id}",
                broker_order_id=broker_order_id,
                ticker=current.ticker or "AAPL",
                side=side,
                qty=qty,
                price=price,
                filled_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        prior = self._positions.get("AAPL")
        prior_qty = prior.qty if prior is not None else Decimal(0)
        signed_qty = qty if side == "buy" else -qty
        resulting_qty = prior_qty + signed_qty
        self._positions["AAPL"] = Position(
            "AAPL",
            resulting_qty,
            price,
            price,
        )
        self.set_session_open_price("AAPL", price)


def _plan_worker(service):
    return RuleWorker(
        service,
        service.rule_repository,
        service.rule_application,
        max_quote_age_seconds=10**9,
    )


def _trigger_first_entry_and_confirm_fill(
    service,
    broker: _FillAwareBroker,
) -> Decimal:
    broker.set_price("AAPL", Decimal("98"))
    outcomes = _plan_worker(service).tick(
        actor="daemon:test",
        reason="trigger first independent entry",
        request_id="planning-trigger-first-entry",
    )
    assert len(outcomes) == 1
    proposal = outcomes[0].proposal
    assert proposal is not None
    approval = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve first plan entry",
        request_id="planning-approve-first-entry",
    )
    assert approval["status"] == "submitted"
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        assert order is not None
        assert order.qty is not None
        assert order.broker_order_id is not None
        qty = order.qty
        broker_order_id = order.broker_order_id
    broker.fill_order(
        broker_order_id,
        qty=qty,
        price=Decimal("98"),
    )
    service.sync_open_orders(
        actor="daemon:test",
        reason="confirm entry fill and activate exits",
        request_id="planning-confirm-entry-fill",
    )
    return qty


def test_entry_trigger_does_not_cancel_other_entries_or_protection(
    make_service,
):
    service = make_service()
    planning = _planning(service)
    plan_id = _analyze(planning, "independent entry groups")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed independent entries",
        request_id="planning-independent-entry-approval",
    )
    service.broker.set_price("AAPL", Decimal("98"))

    outcomes = _plan_worker(service).tick(
        actor="daemon:test",
        reason="evaluate one entry tranche",
        request_id="planning-independent-entry-trigger",
    )

    assert len(outcomes) == 1
    assert outcomes[0].oco_canceled == 0
    with service.session_factory() as session:
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
    entries = [rule for rule in rules if rule.kind == "entry"]
    exits = [rule for rule in rules if rule.kind != "entry"]
    assert sorted(rule.state for rule in entries) == [
        "active",
        "processing",
    ]
    assert {rule.state for rule in exits} == {"pending"}


def test_confirmed_entry_fill_activates_and_sizes_exit_group(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "fill activated exits")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed fill activated exits",
        request_id="planning-fill-activated-approval",
    )

    filled_qty = _trigger_first_entry_and_confirm_fill(service, broker)

    with service.session_factory() as session:
        exits = session.scalars(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind != "entry",
            )
        ).all()
        group = session.get(RuleGroup, exits[0].group_id)
    assert group.state == "active"
    assert {rule.state for rule in exits} == {"active"}
    assert {
        Decimal(json.loads(rule.action_json)["qty"])
        for rule in exits
    } == {filled_qty}


def test_reconciliation_commit_activates_exits_without_a_second_service_step(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "atomic fill activation")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed atomic activation",
        request_id="planning-atomic-activation-approval",
    )
    broker.set_price("AAPL", Decimal("98"))
    proposal = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose atomic activation entry",
        request_id="planning-atomic-entry-proposal",
    )[0].proposal
    approval = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve atomic activation entry",
        request_id="planning-atomic-entry-approval",
    )
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        qty = order.qty
    broker.fill_order(
        approval["broker_order_id"],
        qty=qty,
        price=Decimal("98"),
    )

    service.reconciliation.reconcile(
        actor="daemon:test",
        reason="commit fill and protection together",
        request_id="planning-atomic-fill-reconciliation",
    )

    with service.session_factory() as session:
        exits = session.scalars(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind != "entry",
            )
        ).all()
        group = session.get(RuleGroup, exits[0].group_id)
    assert group.state == "active"
    assert {rule.state for rule in exits} == {"active"}
    assert {
        Decimal(json.loads(rule.action_json)["qty"])
        for rule in exits
    } == {qty}


def _multi_target_plan() -> TradePlan:
    plan = _plan()
    return plan.model_copy(
        update={
            "exit_plan": plan.exit_plan.model_copy(
                update={
                    "targets": [
                        ExitTarget(
                            price_level=Decimal("120"),
                            fraction_to_sell=0.5,
                        ),
                        ExitTarget(
                            price_level=Decimal("130"),
                            fraction_to_sell=0.5,
                        ),
                    ]
                }
            )
        }
    )


def test_exit_proposal_preserves_every_protection_until_broker_fill(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = PlanningService(
        service,
        _StubAnalyst(_multi_target_plan()),
        _provider,
        Secrets(),
    )
    plan_id = _analyze(planning, "progressive exit lifecycle")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed progressive exits",
        request_id="planning-progressive-exit-approval",
    )
    filled_qty = _trigger_first_entry_and_confirm_fill(service, broker)
    with service.session_factory() as session:
        stop_rule_id = session.scalar(
            select(Rule.id).where(
                Rule.plan_id == plan_id,
                Rule.kind == "stop",
            )
        )
    broker.set_price("AAPL", Decimal("91"))

    outcomes = _plan_worker(service).tick(
        actor="daemon:test",
        reason="evaluate protective stop",
        request_id="planning-protective-stop",
    )
    outcome = next(
        result
        for result in outcomes
        if result.rule_id == stop_rule_id
    )

    assert outcome.proposal is not None
    assert outcome.oco_canceled == 0
    with service.session_factory() as session:
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
        stop = session.scalar(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind == "stop",
            )
        )
        exit_group = session.get(RuleGroup, stop.group_id)
    assert stop.state == "processing"
    assert exit_group.state == "active"
    assert {
        rule.state
        for rule in rules
        if rule.kind in {"target", "trailing", "time"}
    } == {"active"}
    assert all(
        rule.state in {"triggered", "canceled"}
        for rule in rules
        if rule.kind == "entry"
    )

    service.reject_order(
        outcome.proposal["order_id"],
        actor="operator:test",
        reason="reject protective proposal drill",
        request_id="planning-reject-protective-stop",
    )
    service.sync_open_orders(
        actor="daemon:test",
        reason="settle rejected protective proposal",
        request_id="planning-settle-rejected-stop",
    )

    with service.session_factory() as session:
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
        stop = next(
            rule for rule in rules if rule.kind == "stop"
        )
        exit_group = session.get(RuleGroup, stop.group_id)
    assert exit_group.state == "active"
    assert stop.state == "active"
    assert {
        rule.state
        for rule in rules
        if rule.kind in {"target", "trailing", "time"}
    } == {"active"}
    assert service.breakers.is_tripped(
        BreakerScope.operator_global()
    )

    broker.set_price("AAPL", Decimal("91"))
    retry = _plan_worker(service).tick(
        actor="daemon:test",
        reason="retry re-armed protective stop",
        request_id="planning-retry-protective-stop",
    )
    retry_proposal = next(
        item.proposal
        for item in retry
        if item.rule_id == stop_rule_id
    )
    assert retry_proposal is not None
    assert retry_proposal["approved_by_risk"] is True
    retry_approval = service.approve_order(
        retry_proposal["order_id"],
        actor="operator:test",
        reason="approve re-armed protective stop",
        request_id="planning-approve-rearmed-stop",
    )
    assert retry_approval["status"] == OrderStatus.SUBMITTED.value
    with service.session_factory() as session:
        first_order = session.get(
            Order,
            outcome.proposal["order_id"],
        )
        second_order = session.get(
            Order,
            retry_proposal["order_id"],
        )
    assert first_order.idempotency_key != second_order.idempotency_key


def test_direct_rule_cancel_cannot_strip_approved_plan_protection(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "reject direct plan rule cancel")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed direct cancellation guard",
        request_id="planning-direct-cancel-approval",
    )
    _trigger_first_entry_and_confirm_fill(service, broker)
    with service.session_factory() as session:
        stop = session.scalar(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind == "stop",
            )
        )
        stop_id = stop.id

    result = service.cancel_rule(
        stop_id,
        actor="operator:test",
        reason="attempt direct protective rule cancellation",
        request_id="planning-direct-rule-cancel",
    )

    assert result == {
        "rule_id": stop_id,
        "canceled": False,
        "error": "plan_rule_requires_plan_cancel",
    }
    with service.session_factory() as session:
        assert session.get(Rule, stop_id).state == "active"
        assert session.get(TradePlanRow, plan_id).status == "approved"


@pytest.mark.parametrize(
    "stale_status",
    [
        OrderStatus.PROPOSED,
        OrderStatus.APPROVAL_RECORDED,
    ],
)
def test_expired_protective_proposal_is_swept_and_rearmed(
    make_service,
    stale_status,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "passive protective ttl sweep")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed passive ttl sweep",
        request_id="planning-passive-ttl-approval",
    )
    _trigger_first_entry_and_confirm_fill(service, broker)
    with service.session_factory() as session:
        stop_rule_id = session.scalar(
            select(Rule.id).where(
                Rule.plan_id == plan_id,
                Rule.kind == "stop",
            )
        )
    broker.set_price("AAPL", Decimal("91"))
    first = _plan_worker(service).tick(
        actor="daemon:test",
        reason="create expiring protective proposal",
        request_id="planning-expiring-protection",
    )
    first_proposal = next(
        item.proposal
        for item in first
        if item.rule_id == stop_rule_id
        and item.proposal is not None
    )
    if stale_status is OrderStatus.APPROVAL_RECORDED:
        service.order_application.approve(
            ApprovalCommand(
                first_proposal["order_id"],
                "operator:test",
                "record approval before simulated process stop",
                datetime.now(timezone.utc),
                "planning-expiring-protection-approval",
            )
        )
    with service.session_factory() as session:
        proposal = session.scalar(
            select(Proposal).where(
                Proposal.order_id == first_proposal["order_id"]
            )
        )
        proposal.expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
        session.commit()

    retried = _plan_worker(service).tick(
        actor="daemon:test",
        reason="sweep and retry expired protection",
        request_id="planning-expired-protection-retry",
    )

    retry_proposal = next(
        item.proposal
        for item in retried
        if item.rule_id == stop_rule_id
        and item.proposal is not None
    )
    assert retry_proposal["order_id"] != first_proposal["order_id"]
    with service.session_factory() as session:
        first_order = session.get(
            Order,
            first_proposal["order_id"],
        )
        retry_order = session.get(
            Order,
            retry_proposal["order_id"],
        )
    assert first_order.status == OrderStatus.EXPIRED.value
    assert retry_order.status == OrderStatus.PROPOSED.value
    assert first_order.idempotency_key != retry_order.idempotency_key


def test_rejected_intermediate_target_does_not_shrink_final_exit(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = PlanningService(
        service,
        _StubAnalyst(_multi_target_plan()),
        _provider,
        Secrets(),
    )
    plan_id = _analyze(planning, "rejected target sizing")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed target sizing",
        request_id="planning-rejected-target-approval",
    )
    filled_qty = _trigger_first_entry_and_confirm_fill(service, broker)
    broker.set_price("AAPL", Decimal("121"))

    first = _plan_worker(service).tick(
        actor="daemon:test",
        reason="evaluate first target",
        request_id="planning-first-target",
    )[0]
    assert first.proposal is not None
    service.reject_order(
        first.proposal["order_id"],
        actor="operator:test",
        reason="reject first target",
        request_id="planning-reject-first-target",
    )
    service.sync_open_orders(
        actor="daemon:test",
        reason="settle rejected first target",
        request_id="planning-settle-first-target",
    )

    with service.session_factory() as session:
        targets = session.scalars(
            select(Rule)
            .where(
                Rule.plan_id == plan_id,
                Rule.kind == "target",
            )
            .order_by(Rule.id)
        ).all()
        stop = session.scalar(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind == "stop",
            )
        )
    assert targets[0].state == "active"
    assert targets[1].state == "active"
    assert Decimal(
        json.loads(targets[1].action_json)["qty"]
    ) == filled_qty
    assert stop.state == "active"

    broker.set_price("AAPL", Decimal("131"))
    retry = _plan_worker(service).tick(
        actor="daemon:test",
        reason="evaluate final target after rejection",
        request_id="planning-final-after-rejection",
    )[0]
    assert retry.proposal is not None
    with service.session_factory() as session:
        retry_order = session.get(
            Order,
            retry.proposal["order_id"],
        )
    assert retry_order.qty == filled_qty // 2


def test_confirmed_target_fills_progress_then_close_plan(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = PlanningService(
        service,
        _StubAnalyst(_multi_target_plan()),
        _provider,
        Secrets(),
    )
    plan_id = _analyze(planning, "confirmed target lifecycle")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed confirmed target lifecycle",
        request_id="planning-confirmed-target-approval",
    )
    filled_qty = _trigger_first_entry_and_confirm_fill(service, broker)
    broker.set_price("AAPL", Decimal("121"))
    first = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose first confirmed target",
        request_id="planning-propose-confirmed-first-target",
    )[0]
    assert first.proposal is not None
    approved = service.approve_order(
        first.proposal["order_id"],
        actor="operator:test",
        reason="approve first target",
        request_id="planning-approve-confirmed-first-target",
    )
    assert approved["status"] == "submitted"
    first_qty = filled_qty // 2
    broker.fill_order(
        approved["broker_order_id"],
        qty=first_qty,
        price=Decimal("121"),
        side="sell",
    )
    service.sync_open_orders(
        actor="daemon:test",
        reason="confirm first target fill",
        request_id="planning-confirm-first-target-fill",
    )

    with service.session_factory() as session:
        targets = session.scalars(
            select(Rule)
            .where(
                Rule.plan_id == plan_id,
                Rule.kind == "target",
            )
            .order_by(Rule.id)
        ).all()
        stop = session.scalar(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind == "stop",
            )
        )
        exit_group = session.get(RuleGroup, stop.group_id)
    remaining = filled_qty - first_qty
    assert targets[0].state == "triggered"
    assert targets[1].state == "active"
    assert stop.state == "active"
    assert exit_group.state == "active"
    assert Decimal(json.loads(stop.action_json)["qty"]) == remaining
    assert Decimal(
        json.loads(targets[1].action_json)["qty"]
    ) == remaining

    broker.set_price("AAPL", Decimal("131"))
    final = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose final confirmed target",
        request_id="planning-propose-confirmed-final-target",
    )[0]
    approved_final = service.approve_order(
        final.proposal["order_id"],
        actor="operator:test",
        reason="approve final target",
        request_id="planning-approve-confirmed-final-target",
    )
    assert approved_final["status"] == "submitted", approved_final
    broker.fill_order(
        approved_final["broker_order_id"],
        qty=remaining,
        price=Decimal("131"),
        side="sell",
    )
    service.sync_open_orders(
        actor="daemon:test",
        reason="confirm final target fill",
        request_id="planning-confirm-final-target-fill",
    )

    with service.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
        exit_group = session.get(RuleGroup, stop.group_id)
    assert plan.status == "completed"
    assert exit_group.state == "triggered"
    assert all(
        rule.state in {"triggered", "canceled"}
        for rule in rules
    )


def test_confirmed_exit_cancels_live_unfilled_entry_before_plan_closes(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "cancel live entry after exit")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed live entry cancellation",
        request_id="planning-live-entry-cancel-approval",
    )
    filled_qty = _trigger_first_entry_and_confirm_fill(service, broker)

    broker.set_price("AAPL", Decimal("95"))
    second_entry = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose second entry",
        request_id="planning-propose-second-entry",
    )[0]
    second_approval = service.approve_order(
        second_entry.proposal["order_id"],
        actor="operator:test",
        reason="approve second entry",
        request_id="planning-approve-second-entry",
    )
    assert second_approval["status"] == "submitted"
    service.sync_open_orders(
        actor="daemon:test",
        reason="reconcile live second entry before stop",
        request_id="planning-reconcile-second-entry",
    )

    broker.set_price("AAPL", Decimal("91"))
    stop = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose stop with live entry",
        request_id="planning-propose-stop-live-entry",
    )[0]
    assert stop.proposal["status"] == "proposed", stop.proposal
    assert broker.get_order_status(
        second_approval["broker_order_id"]
    ).status is OrderStatus.CANCELED
    stop_approval = service.approve_order(
        stop.proposal["order_id"],
        actor="operator:test",
        reason="approve stop with live entry",
        request_id="planning-approve-stop-live-entry",
    )
    assert stop_approval["status"] == "submitted", stop_approval
    broker.fill_order(
        stop_approval["broker_order_id"],
        qty=filled_qty,
        price=Decimal("91"),
        side="sell",
    )

    service.sync_open_orders(
        actor="daemon:test",
        reason="confirm stop and cancel live entry",
        request_id="planning-confirm-stop-cancel-entry",
    )

    assert broker.get_order_status(
        second_approval["broker_order_id"]
    ).status is OrderStatus.CANCELED
    with service.session_factory() as session:
        second_order = session.get(
            Order,
            second_entry.proposal["order_id"],
        )
        plan = session.get(TradePlanRow, plan_id)
    assert second_order.status == OrderStatus.CANCELED.value
    assert plan.status == "completed"


def test_late_entry_fill_reopens_terminal_plan_protection(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "late terminal entry fill")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed late terminal fill protection",
        request_id="planning-late-terminal-approval",
    )
    first_filled_qty = _trigger_first_entry_and_confirm_fill(
        service,
        broker,
    )
    broker.set_price("AAPL", Decimal("95"))
    second_entry = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose entry that will fill late",
        request_id="planning-late-terminal-entry",
    )[0]
    second_approval = service.approve_order(
        second_entry.proposal["order_id"],
        actor="operator:test",
        reason="approve entry that will fill after cancellation",
        request_id="planning-late-terminal-entry-approval",
    )
    with service.session_factory() as session:
        second_order = session.get(
            Order,
            second_entry.proposal["order_id"],
        )
        second_qty = second_order.qty

    broker.set_price("AAPL", Decimal("91"))
    stop_outcomes = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose flattening stop before late fill",
        request_id="planning-late-terminal-stop",
    )
    stop_proposal = next(
        item.proposal
        for item in stop_outcomes
        if item.proposal is not None
        and item.proposal["order_id"]
        != second_entry.proposal["order_id"]
    )
    stop_approval = service.approve_order(
        stop_proposal["order_id"],
        actor="operator:test",
        reason="approve flattening stop before late fill",
        request_id="planning-late-terminal-stop-approval",
    )
    broker.fill_order(
        stop_approval["broker_order_id"],
        qty=first_filled_qty,
        price=Decimal("91"),
        side="sell",
    )
    service.sync_open_orders(
        actor="daemon:test",
        reason="complete plan before delayed entry activity",
        request_id="planning-complete-before-late-fill",
    )
    with service.session_factory() as session:
        assert session.get(TradePlanRow, plan_id).status == "completed"
    assert broker.get_order_status(
        second_approval["broker_order_id"]
    ).status is OrderStatus.CANCELED

    broker.fill_order(
        second_approval["broker_order_id"],
        qty=second_qty,
        price=Decimal("95"),
        side="buy",
    )
    service.sync_open_orders(
        actor="startup:test",
        reason="reconcile delayed entry fill on terminal plan",
        request_id="planning-reconcile-late-terminal-fill",
    )

    with service.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        downside = session.scalars(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind.in_({"stop", "trailing"}),
            )
        ).all()
    assert plan.status == "protection_required"
    assert downside and {rule.state for rule in downside} == {"active"}
    assert {
        Decimal(json.loads(rule.action_json)["qty"])
        for rule in downside
    } == {second_qty}
    assert service.breakers.is_tripped(
        BreakerScope.broker_drift()
    )
    assert service.breakers.is_tripped(
        BreakerScope.operator_global()
    )


def test_cancel_plan_refuses_to_abandon_confirmed_open_quantity(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "refuse naked cancellation")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed cancellation guard",
        request_id="planning-cancel-guard-approval",
    )
    _trigger_first_entry_and_confirm_fill(service, broker)

    result = planning.cancel_plan(
        plan_id,
        actor="operator:test",
        reason="attempt to abandon open plan quantity",
        request_id="planning-refuse-open-plan-cancel",
    )

    assert result["error"] == "position_open"
    assert result["status"] == "approved"
    with service.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        exits = session.scalars(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind != "entry",
            )
        ).all()
    assert plan.status == "approved"
    assert exits and {rule.state for rule in exits} == {"active"}


def test_plan_cancel_requires_exact_fill_truth_after_broker_cancel(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "late fill cancellation latch")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed late fill latch",
        request_id="planning-late-fill-approval",
    )
    broker.set_price("AAPL", Decimal("98"))
    proposal = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose entry for late fill latch",
        request_id="planning-late-fill-proposal",
    )[0].proposal
    approval = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve entry for late fill latch",
        request_id="planning-late-fill-order-approval",
    )
    service.sync_open_orders(
        actor="daemon:test",
        reason="clear initial plan submission latch",
        request_id="planning-late-fill-initial-sync",
    )
    current = broker.get_order_status(approval["broker_order_id"])
    partial = OrderResult(
        idempotency_key=current.idempotency_key,
        broker_order_id=current.broker_order_id,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("98"),
        ticker="AAPL",
    )
    broker._orders_by_id[current.broker_order_id] = partial
    broker._orders_by_key[current.idempotency_key] = partial
    broker._positions["AAPL"] = Position(
        "AAPL",
        Decimal("1"),
        Decimal("98"),
        Decimal("98"),
    )

    result = planning.cancel_plan(
        plan_id,
        actor="operator:test",
        reason="cancel with unresolved exact fill",
        request_id="planning-late-fill-cancel",
    )

    assert result["error"] == "order_cancel_unconfirmed"
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        plan = session.get(TradePlanRow, plan_id)
        exits = session.scalars(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind != "entry",
            )
        ).all()
    assert order.status == OrderStatus.CANCELED.value
    assert order.acceptance_state == FILL_RECONCILIATION_REQUIRED
    assert plan.status == "approved"
    assert any(
        rule.state in {"pending", "active"}
        for rule in exits
    )


def test_only_one_nonterminal_exit_intent_can_exist_per_plan_group(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = PlanningService(
        service,
        _StubAnalyst(_multi_target_plan()),
        _provider,
        Secrets(),
    )
    plan_id = _analyze(planning, "single exit intent")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed single exit intent",
        request_id="planning-single-exit-approval",
    )
    _trigger_first_entry_and_confirm_fill(service, broker)
    broker.set_price("AAPL", Decimal("91"))
    first = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose one stop intent",
        request_id="planning-single-exit-first",
    )
    with service.session_factory() as session:
        exit_rule_ids = set(
            session.scalars(
                select(Rule.id).where(
                    Rule.plan_id == plan_id,
                    Rule.kind != "entry",
                )
            ).all()
        )
    assert sum(
        item.proposal is not None
        for item in first
        if item.rule_id in exit_rule_ids
    ) == 1

    broker.set_price("AAPL", Decimal("131"))
    second = _plan_worker(service).tick(
        actor="daemon:test",
        reason="refuse competing target intent",
        request_id="planning-single-exit-second",
    )

    assert all(
        item.proposal is None
        for item in second
        if item.rule_id in exit_rule_ids
    )
    with service.session_factory() as session:
        proposal_count = session.scalar(
            select(func.count())
            .select_from(Proposal)
            .join(Order, Order.id == Proposal.order_id)
            .join(Rule, Rule.id == Proposal.source_rule_id)
            .where(
                Rule.plan_id == plan_id,
                Rule.kind != "entry",
                Order.status.in_(
                    {
                        OrderStatus.PROPOSED.value,
                        OrderStatus.APPROVAL_RECORDED.value,
                        OrderStatus.SUBMITTED.value,
                        OrderStatus.PARTIALLY_FILLED.value,
                    }
                ),
            )
        )
    assert proposal_count == 1


def test_delayed_old_exit_fill_cancels_newer_stale_generation(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "delayed exit fill generation")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed delayed fill generation",
        request_id="planning-delayed-generation-approval",
    )
    entry_qty = _trigger_first_entry_and_confirm_fill(service, broker)
    with service.session_factory() as session:
        stop_rule_id = session.scalar(
            select(Rule.id).where(
                Rule.plan_id == plan_id,
                Rule.kind == "stop",
            )
        )
    broker.set_price("AAPL", Decimal("91"))
    first_stop = next(
        outcome
        for outcome in _plan_worker(service).tick(
            actor="daemon:test",
            reason="propose first generation stop",
            request_id="planning-first-generation-stop",
        )
        if outcome.rule_id == stop_rule_id
    )
    first_approval = service.approve_order(
        first_stop.proposal["order_id"],
        actor="operator:test",
        reason="approve first generation stop",
        request_id="planning-first-generation-stop-approval",
    )
    old_broker_id = first_approval["broker_order_id"]
    old = broker.get_order_status(old_broker_id)
    first_partial_qty = Decimal("1")
    partial = OrderResult(
        idempotency_key=old.idempotency_key,
        broker_order_id=old_broker_id,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_qty=first_partial_qty,
        avg_fill_price=Decimal("91"),
        ticker="AAPL",
    )
    broker._orders_by_id[old_broker_id] = partial
    broker._orders_by_key[old.idempotency_key] = partial
    broker.activities.append(
        BrokerFill(
            broker_fill_id=f"partial-{old_broker_id}",
            broker_order_id=old_broker_id,
            ticker="AAPL",
            side="sell",
            qty=first_partial_qty,
            price=Decimal("91"),
            filled_at=datetime.now(timezone.utc) - timedelta(
                seconds=2
            ),
        )
    )
    broker._positions["AAPL"] = Position(
        "AAPL",
        entry_qty - first_partial_qty,
        Decimal("98"),
        Decimal("91"),
    )

    service.sync_open_orders(
        actor="daemon:test",
        reason="reconcile first partial exit generation",
        request_id="planning-first-partial-generation-sync",
    )
    assert broker.get_order_status(
        old_broker_id
    ).status is OrderStatus.CANCELED

    retry = next(
        outcome
        for outcome in _plan_worker(service).tick(
            actor="daemon:test",
            reason="propose replacement stop generation",
            request_id="planning-replacement-generation-stop",
        )
        if outcome.rule_id == stop_rule_id
    )
    retry_approval = service.approve_order(
        retry.proposal["order_id"],
        actor="operator:test",
        reason="approve replacement stop generation",
        request_id="planning-replacement-generation-approval",
    )
    assert retry_approval["status"] == OrderStatus.SUBMITTED.value
    retry_broker_id = retry_approval["broker_order_id"]
    with service.session_factory() as session:
        retry_created_at = session.scalar(
            select(Proposal.created_at).where(
                Proposal.order_id == retry.proposal["order_id"]
            )
        )

    late_qty = Decimal("1")
    late_total = first_partial_qty + late_qty
    canceled_with_late_fill = OrderResult(
        idempotency_key=old.idempotency_key,
        broker_order_id=old_broker_id,
        status=OrderStatus.CANCELED,
        filled_qty=late_total,
        avg_fill_price=Decimal("90.5"),
        ticker="AAPL",
    )
    broker._orders_by_id[old_broker_id] = canceled_with_late_fill
    broker._orders_by_key[
        old.idempotency_key
    ] = canceled_with_late_fill
    broker.activities.append(
        BrokerFill(
            broker_fill_id=f"late-{old_broker_id}",
            broker_order_id=old_broker_id,
            ticker="AAPL",
            side="sell",
            qty=late_qty,
            price=Decimal("90"),
            # Deliberately predates the replacement proposal. Wall-clock
            # ordering must not decide whether the replacement is stale.
            filled_at=retry_created_at - timedelta(
                microseconds=1
            ),
        )
    )
    broker._positions["AAPL"] = Position(
        "AAPL",
        entry_qty - late_total,
        Decimal("98"),
        Decimal("90"),
    )

    service.sync_open_orders(
        actor="daemon:test",
        reason="reconcile delayed old-generation exit fill",
        request_id="planning-delayed-old-fill-sync",
    )

    assert broker.get_order_status(
        retry_broker_id
    ).status is OrderStatus.CANCELED
    with service.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        old_proposal = session.scalar(
            select(Proposal).where(
                Proposal.order_id
                == first_stop.proposal["order_id"]
            )
        )
        retry_proposal = session.scalar(
            select(Proposal).where(
                Proposal.order_id == retry.proposal["order_id"]
            )
        )
        retry_order = session.get(
            Order,
            retry.proposal["order_id"],
        )
        stop = session.get(Rule, stop_rule_id)
    assert old_proposal.plan_generation < retry_proposal.plan_generation
    assert retry_proposal.plan_generation < plan.residual_generation
    assert retry_order.status == OrderStatus.CANCELED.value
    assert stop.state == "active"
    assert Decimal(json.loads(stop.action_json)["qty"]) == (
        entry_qty - late_total
    )


def test_new_entry_fill_cancels_and_resizes_older_live_stop(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "entry growth residual generation")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed entry growth generation",
        request_id="planning-entry-growth-approval",
    )
    first_qty = _trigger_first_entry_and_confirm_fill(service, broker)
    with service.session_factory() as session:
        stop_rule_id = session.scalar(
            select(Rule.id).where(
                Rule.plan_id == plan_id,
                Rule.kind == "stop",
            )
        )
        second_entry_rule_id = session.scalar(
            select(Rule.id)
            .where(
                Rule.plan_id == plan_id,
                Rule.kind == "entry",
                Rule.state == "active",
            )
            .order_by(Rule.id)
        )

    broker.set_price("AAPL", Decimal("91"))
    stop = next(
        outcome
        for outcome in _plan_worker(service).tick(
            actor="daemon:test",
            reason="propose stop before later entry fill",
            request_id="planning-stop-before-entry-growth",
        )
        if outcome.rule_id == stop_rule_id
    )
    stop_approval = service.approve_order(
        stop.proposal["order_id"],
        actor="operator:test",
        reason="approve stop before later entry fill",
        request_id="planning-stop-before-entry-growth-approval",
    )
    assert stop_approval["status"] == OrderStatus.SUBMITTED.value

    with service.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        second_qty = first_qty
        second_order = Order(
            idempotency_key="late-entry-with-live-stop",
            ticker="AAPL",
            side="buy",
            order_type="market",
            qty=second_qty,
            status=OrderStatus.FILLED.value,
            broker_order_id="late-entry-with-live-stop-broker",
            acceptance_state=OrderStatus.FILLED.value,
        )
        persist_sensitive(
            session,
            second_order,
            {"approval_reason": "test fixture"},
        )
        persist_sensitive(
            session,
            Proposal(
                order_id=second_order.id,
                source_rule_group_id=session.get(
                    Rule,
                    second_entry_rule_id,
                ).group_id,
                source_rule_id=second_entry_rule_id,
                plan_generation=plan.residual_generation,
                ttl_minutes=15,
                expires_at=datetime.now(timezone.utc)
                + timedelta(minutes=15),
            ),
            {"reasoning": "test fixture"},
        )
        session.add(
            Fill(
                order_id=second_order.id,
                ticker="AAPL",
                side="buy",
                qty=second_qty,
                price=Decimal("95"),
                broker_fill_id="late-entry-with-live-stop-fill",
                filled_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    broker._positions["AAPL"] = Position(
        "AAPL",
        first_qty + second_qty,
        Decimal("96.5"),
        Decimal("95"),
    )

    service.sync_open_orders(
        actor="daemon:test",
        reason="resize protection after additional entry fill",
        request_id="planning-resize-after-entry-growth",
    )

    assert broker.get_order_status(
        stop_approval["broker_order_id"]
    ).status is OrderStatus.CANCELED
    with service.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        stop_rule = session.get(Rule, stop_rule_id)
        stop_proposal = session.scalar(
            select(Proposal).where(
                Proposal.order_id == stop.proposal["order_id"]
            )
        )
    assert stop_proposal.plan_generation < plan.residual_generation
    assert stop_rule.state == "active"
    assert Decimal(json.loads(stop_rule.action_json)["qty"]) == (
        first_qty + second_qty
    )


def test_plan_exit_rejects_unproven_cross_plan_allocation(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    first_planning = _planning(service)
    first_plan_id = _analyze(
        first_planning,
        "first allocation plan",
    )["plan_id"]
    _approve(first_planning,
        first_plan_id,
        actor="operator:test",
        reason="approve first allocation plan",
        request_id="planning-first-allocation-approval",
    )
    first_qty = _trigger_first_entry_and_confirm_fill(
        service,
        broker,
    )

    second_planning = _planning(service)
    second_plan_id = _analyze(
        second_planning,
        "second allocation plan",
    )["plan_id"]
    _approve(second_planning,
        second_plan_id,
        actor="operator:test",
        reason="approve second allocation plan",
        request_id="planning-second-allocation-approval",
    )
    second_qty = _trigger_first_entry_and_confirm_fill(
        service,
        broker,
    )
    assert second_qty == first_qty
    broker._positions["AAPL"] = Position(
        "AAPL",
        first_qty,
        Decimal("98"),
        Decimal("98"),
    )
    # Broker truth still contains only the first plan's quantity. The second
    # local plan allocation must therefore make every exit fail closed.
    assert broker.get_positions()[0].qty == first_qty
    with service.session_factory() as session:
        first_stop_id = session.scalar(
            select(Rule.id).where(
                Rule.plan_id == first_plan_id,
                Rule.kind == "stop",
            )
        )
    broker.set_price("AAPL", Decimal("91"))
    first_stop = next(
        outcome
        for outcome in _plan_worker(service).tick(
            actor="daemon:test",
            reason="propose exit with conflicting allocation",
            request_id="planning-conflicting-allocation-proposal",
        )
        if outcome.rule_id == first_stop_id
    )

    result = service.approve_order(
        first_stop.proposal["order_id"],
        actor="operator:test",
        reason="attempt exit with conflicting plan allocations",
        request_id="planning-conflicting-allocation-approval",
    )

    assert result["status"] == OrderStatus.REJECTED.value
    assert result["risk_reasons"] == [
        (
            "plan execution guard: plan allocation exceeds "
            "reconciled broker position"
        )
    ]
    assert service.breakers.is_tripped(
        BreakerScope.broker_drift()
    )


def test_manual_exit_cannot_consume_plan_allocated_position(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(
        planning,
        "reserve plan allocation from manual exit",
    )["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="approve allocation reservation plan",
        request_id="planning-allocation-reservation-approval",
    )
    filled_qty = _trigger_first_entry_and_confirm_fill(
        service,
        broker,
    )
    assert broker.get_positions()[0].qty == filled_qty

    proposal = service.propose_order(
        "AAPL",
        "sell",
        "market",
        qty="1",
        actor="operator:test",
        reason="attempt unrelated manual exit",
        request_id="planning-manual-exit-proposal",
    )
    assert proposal["status"] == OrderStatus.PROPOSED.value
    broker_orders_before = len(broker._orders_by_id)

    result = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve unrelated manual exit",
        request_id="planning-manual-exit-approval",
    )

    assert result["status"] == OrderStatus.REJECTED.value
    assert result["risk_reasons"] == [
        (
            "plan execution guard: order would consume "
            "plan-allocated position"
        )
    ]
    assert len(broker._orders_by_id) == broker_orders_before


def test_manual_exit_can_use_only_unallocated_position(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(
        planning,
        "preserve plan allocation while reducing excess",
    )["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="approve excess allocation plan",
        request_id="planning-excess-allocation-approval",
    )
    filled_qty = _trigger_first_entry_and_confirm_fill(
        service,
        broker,
    )
    unallocated_qty = Decimal("2")
    broker._positions["AAPL"] = Position(
        "AAPL",
        filled_qty + unallocated_qty,
        Decimal("98"),
        Decimal("98"),
    )
    proposal = service.propose_order(
        "AAPL",
        "sell",
        "market",
        qty=str(unallocated_qty),
        actor="operator:test",
        reason="reduce only unallocated shares",
        request_id="planning-unallocated-exit-proposal",
    )
    broker_orders_before = len(broker._orders_by_id)

    result = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve unallocated share reduction",
        request_id="planning-unallocated-exit-approval",
    )

    assert result["status"] == OrderStatus.SUBMITTED.value
    assert result["risk_reasons"] == []
    assert len(broker._orders_by_id) == broker_orders_before + 1


def test_plan_over_exit_trips_drift_and_cannot_complete(make_service):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "over exit detection")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed over exit detection",
        request_id="planning-over-exit-approval",
    )
    filled_qty = _trigger_first_entry_and_confirm_fill(service, broker)
    with service.session_factory() as session:
        stop_rule_id = session.scalar(
            select(Rule.id).where(
                Rule.plan_id == plan_id,
                Rule.kind == "stop",
            )
        )
    broker.set_price("AAPL", Decimal("91"))
    outcomes = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose over exit stop",
        request_id="planning-over-exit-stop",
    )
    proposal = next(
        outcome.proposal
        for outcome in outcomes
        if outcome.rule_id == stop_rule_id
    )
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        order.status = OrderStatus.FILLED.value
        order.broker_order_id = "manual-over-exit"
        order.acceptance_state = OrderStatus.FILLED.value
        session.add(
            Fill(
                order_id=order.id,
                ticker="AAPL",
                side="sell",
                qty=filled_qty + Decimal("1"),
                price=Decimal("91"),
                broker_fill_id="manual-over-exit-fill",
                filled_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    service.rule_repository.refresh_fill_activated_rules(
        now=datetime.now(timezone.utc),
        actor="daemon:test",
        reason="detect signed plan residual",
        request_id="planning-over-exit-reconcile",
    )

    with service.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
    assert plan.status == "approved"
    assert service.breakers.is_tripped(
        BreakerScope.broker_drift()
    )


def test_immediate_entry_fill_reconciles_before_returning(
    make_service,
):
    class ImmediateFillBroker(_FillAwareBroker):
        def submit_order(self, order):
            submitted = super().submit_order(order)
            assert order.qty is not None
            self.fill_order(
                submitted.broker_order_id,
                qty=order.qty,
                price=Decimal("98"),
            )
            return self.get_order_status(submitted.broker_order_id)

    broker = ImmediateFillBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "immediate fill protection")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed immediate fill protection",
        request_id="planning-immediate-fill-approval",
    )
    broker.set_price("AAPL", Decimal("98"))
    proposal = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose immediate fill entry",
        request_id="planning-immediate-fill-proposal",
    )[0].proposal

    result = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve immediate fill entry",
        request_id="planning-immediate-fill-order-approval",
    )

    assert result["status"] == OrderStatus.FILLED.value
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        exits = session.scalars(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind != "entry",
            )
        ).all()
    assert order.acceptance_state != FILL_RECONCILIATION_REQUIRED
    assert exits and {rule.state for rule in exits} == {"active"}


def test_immediate_entry_fill_requires_full_downside_protection(
    make_service,
):
    class ImmediateFillBroker(_FillAwareBroker):
        def submit_order(self, order):
            submitted = super().submit_order(order)
            assert order.qty is not None
            self.fill_order(
                submitted.broker_order_id,
                qty=order.qty,
                price=Decimal("98"),
            )
            return self.get_order_status(submitted.broker_order_id)

    broker = ImmediateFillBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "mandatory downside protection")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed mandatory downside protection",
        request_id="planning-downside-protection-approval",
    )
    with service.session_factory() as session:
        downside_rules = session.scalars(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind.in_({"stop", "trailing"}),
            )
        ).all()
        for rule in downside_rules:
            rule.state = "canceled"
        session.commit()
    broker.set_price("AAPL", Decimal("98"))
    proposal = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose entry without downside protection",
        request_id="planning-unprotected-entry-proposal",
    )[0].proposal

    result = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve entry without downside protection",
        request_id="planning-unprotected-entry-approval",
    )

    assert result["status"] == OrderStatus.ACCEPTANCE_UNKNOWN.value
    assert service.breakers.is_tripped(
        BreakerScope.broker_drift()
    )
    assert service.breakers.is_tripped(
        BreakerScope.operator_global()
    )


def test_restart_recovers_fill_before_post_submission_protection_callback(
    make_service,
):
    class ImmediateFillBroker(_FillAwareBroker):
        def submit_order(self, order):
            submitted = super().submit_order(order)
            assert order.qty is not None
            self.fill_order(
                submitted.broker_order_id,
                qty=order.qty,
                price=Decimal("98"),
            )
            return self.get_order_status(submitted.broker_order_id)

    broker = ImmediateFillBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "post send crash recovery")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed post-send recovery",
        request_id="planning-post-send-recovery-approval",
    )
    broker.set_price("AAPL", Decimal("98"))
    proposal = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose post-send recovery entry",
        request_id="planning-post-send-recovery-proposal",
    )[0].proposal
    # Simulate process death after broker response persistence and before the
    # normal callback. The durable group latch must survive for startup.
    service.order_submission.reconcile_immediate_fill = None
    result = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve post-send recovery entry",
        request_id="planning-post-send-recovery-submit",
    )
    assert result["status"] == OrderStatus.FILLED.value
    with service.session_factory() as session:
        exits = session.scalars(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind != "entry",
            )
        ).all()
        entry = session.scalar(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind == "entry",
                Rule.state == "processing",
            )
        )
        entry_group = session.get(RuleGroup, entry.group_id)
    assert {rule.state for rule in exits} == {"pending"}
    assert entry_group.reconciliation_required is True

    restarted = make_service(broker=broker)
    report = restarted.sync_open_orders(
        actor="startup:test",
        reason="recover post-send fill before startup readiness",
        request_id="planning-post-send-recovery-sync",
    )

    assert report["failed"] == 0
    with restarted.session_factory() as session:
        exits = session.scalars(
            select(Rule).where(
                Rule.plan_id == plan_id,
                Rule.kind != "entry",
            )
        ).all()
        entry_group = session.get(RuleGroup, entry.group_id)
    assert exits and {rule.state for rule in exits} == {"active"}
    assert entry_group.reconciliation_required is False


def test_startup_sync_resumes_durable_plan_cancel_intent(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "durable plan cancel intent")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed durable cancellation intent",
        request_id="planning-durable-cancel-approval",
    )
    broker.set_price("AAPL", Decimal("98"))
    proposal = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose entry for cancellation recovery",
        request_id="planning-cancel-recovery-proposal",
    )[0].proposal
    approval = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve entry for cancellation recovery",
        request_id="planning-cancel-recovery-approval",
    )
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        order.last_error_code = "plan_cancel"
        order.plan_cancel_state = PLAN_CANCEL_REQUESTED
        session.commit()

    report = service.sync_open_orders(
        actor="startup:test",
        reason="resume durable plan cancellation intent",
        request_id="planning-resume-cancel-intent",
    )

    assert report["plan_order_cancel_failures"] == 0
    assert broker.get_order_status(
        approval["broker_order_id"]
    ).status is OrderStatus.CANCELED
    with service.session_factory() as session:
        order = session.get(
            Order,
            proposal["order_id"],
        )
        assert order.status == OrderStatus.CANCELED.value
        assert order.plan_cancel_state == PLAN_CANCEL_SETTLED


def test_unresolved_plan_cancel_retries_and_blocks_startup(
    make_service,
    app_config,
    session_factory,
):
    from trading_assistant.orders.startup import (
        StartupReconciliationFailed,
    )
    from trading_assistant.service import TradingService

    class CancellationStaysLiveBroker(_FillAwareBroker):
        def __init__(self):
            super().__init__()
            self.cancel_attempts = 0

        def cancel_order(self, order_id):
            self.cancel_attempts += 1
            return self.get_order_status(order_id)

    broker = CancellationStaysLiveBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "startup blocking plan cancel")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="approve startup blocking cancellation",
        request_id="planning-startup-cancel-approval",
    )
    broker.set_price("AAPL", Decimal("98"))
    proposal = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose startup blocking entry",
        request_id="planning-startup-cancel-proposal",
    )[0].proposal
    service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve startup blocking entry",
        request_id="planning-startup-cancel-submit",
    )
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        order.plan_cancel_state = PLAN_CANCEL_REQUESTED
        session.commit()

    restarted = TradingService(
        broker,
        session_factory,
        app_config,
        FakeClock(is_open=True),
        require_startup_reconciliation=True,
    )
    generation = restarted.require_startup_reconciliation(
        actor="startup:test",
        reason="require cancellation recovery",
        request_id="planning-startup-cancel-require",
    )

    with pytest.raises(
        StartupReconciliationFailed,
        match="broker_reconciliation_failed",
    ):
        restarted.reconcile_startup_epoch(
            generation,
            actor="startup:test",
            reason="attempt cancellation recovery",
            request_id="planning-startup-cancel-reconcile",
        )

    assert broker.cancel_attempts == 1
    with restarted.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        assert order.status == OrderStatus.SUBMITTED.value
        assert order.plan_cancel_state == PLAN_CANCEL_REQUESTED
    assert restarted.breakers.is_tripped(
        BreakerScope.broker_drift()
    )

    report = restarted.sync_open_orders(
        actor="startup:test",
        reason="retry unresolved cancellation",
        request_id="planning-startup-cancel-retry",
    )

    assert broker.cancel_attempts == 2
    assert report["failed"] >= 1
    assert report["plan_order_cancel_failures"] == 1
    assert report["remaining_plan_cancel_intents"] == [
        proposal["order_id"]
    ]


def test_indeterminate_cancel_error_never_erases_retry_intent(
    make_service,
):
    class IndeterminateCancellationBroker(_FillAwareBroker):
        def __init__(self):
            super().__init__()
            self.cancel_attempts = 0
            self.fail_next_status = False

        def cancel_order(self, order_id):
            self.cancel_attempts += 1
            self.fail_next_status = True
            raise ConnectionError("cancel unavailable")

        def get_order_status(self, order_id):
            if self.fail_next_status:
                self.fail_next_status = False
                raise ConnectionError("status unavailable")
            return super().get_order_status(order_id)

    broker = IndeterminateCancellationBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "indeterminate cancel intent")[
        "plan_id"
    ]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="approve indeterminate cancellation",
        request_id="planning-indeterminate-cancel-approval",
    )
    broker.set_price("AAPL", Decimal("98"))
    proposal = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose indeterminate cancellation entry",
        request_id="planning-indeterminate-cancel-proposal",
    )[0].proposal
    service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve indeterminate cancellation entry",
        request_id="planning-indeterminate-cancel-submit",
    )
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        order.plan_cancel_state = PLAN_CANCEL_REQUESTED
        session.commit()

    first = service.sync_open_orders(
        actor="startup:test",
        reason="first indeterminate cancellation attempt",
        request_id="planning-indeterminate-cancel-first",
    )

    assert first["failed"] >= 1
    assert first["remaining_plan_cancel_intents"] == [
        proposal["order_id"]
    ]
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        assert order.last_error_code == "indeterminate_cancel"
        assert (
            order.plan_cancel_state
            == PLAN_CANCEL_INDETERMINATE
        )

    second = service.sync_open_orders(
        actor="startup:test",
        reason="retry indeterminate cancellation attempt",
        request_id="planning-indeterminate-cancel-second",
    )

    assert broker.cancel_attempts == 2
    assert second["failed"] >= 1
    assert second["remaining_plan_cancel_intents"] == [
        proposal["order_id"]
    ]


def test_completed_plan_is_terminal_and_cannot_be_canceled(
    make_service,
):
    service = make_service()
    planning = _planning(service)
    plan_id = _analyze(planning, "completed plan terminality")[
        "plan_id"
    ]
    with service.session_factory() as session:
        plan = session.get(TradePlanRow, plan_id)
        plan.status = "completed"
        session.commit()

    result = planning.cancel_plan(
        plan_id,
        actor="operator:test",
        reason="attempt completed plan cancellation",
        request_id="planning-completed-plan-cancel",
    )

    assert result["error"] == "invalid_state"
    assert result["status"] == "completed"
    with service.session_factory() as session:
        assert session.get(TradePlanRow, plan_id).status == "completed"


def test_cancel_plan_confirms_broker_entry_cancel_before_rules_terminal(
    make_service,
):
    broker = _FillAwareBroker()
    service = make_service(broker=broker)
    planning = _planning(service)
    plan_id = _analyze(planning, "cancel broker live plan")["plan_id"]
    _approve(planning,
        plan_id,
        actor="operator:test",
        reason="reviewed broker live cancellation",
        request_id="planning-live-plan-approval",
    )
    broker.set_price("AAPL", Decimal("98"))
    proposal = _plan_worker(service).tick(
        actor="daemon:test",
        reason="propose live plan entry",
        request_id="planning-live-plan-entry",
    )[0].proposal
    approval = service.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="approve live plan entry",
        request_id="planning-live-plan-entry-approval",
    )
    assert approval["status"] == "submitted"

    result = planning.cancel_plan(
        plan_id,
        actor="operator:test",
        reason="cancel plan with broker live entry",
        request_id="planning-cancel-broker-live-plan",
    )

    assert result["status"] == "canceled"
    assert broker.get_order_status(
        approval["broker_order_id"]
    ).status is OrderStatus.CANCELED
    with service.session_factory() as session:
        order = session.get(Order, proposal["order_id"])
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
    assert order.status == OrderStatus.CANCELED.value
    assert all(rule.state == "canceled" for rule in rules)


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
        _approve(planning,
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
            _approve(planning,
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
    _approve(pln,
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
        store = sensitive_store(s)
        audit_contexts = {
            (
                audit.actor,
                store.read(audit, "reason"),
                audit.request_id,
            )
            for audit in audits
        }
    assert [audit.action for audit in audits].count("plan.cancel") == 1
    assert [audit.action for audit in audits].count(
        "rule_group.cancel"
    ) == len(group_ids)
    assert [audit.action for audit in audits].count(
        "rule.cancel"
    ) == len(rules)
    assert audit_contexts == {
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
    _approve(planning,
        plan_id,
        actor="operator:cancel-setup",
        reason="approve before cancel rollback",
        request_id="plan-cancel-rollback-setup",
    )
    with svc.session_factory() as session:
        rules = session.scalars(
            select(Rule).where(Rule.plan_id == plan_id)
        ).all()
        group_ids = {rule.group_id for rule in rules}
        rule_ids = [rule.id for rule in rules]
        original_rule_states = {
            rule.id: rule.state for rule in rules
        }
        original_group_states = {
            group.id: group.state
            for group in session.scalars(
                select(RuleGroup).where(
                    RuleGroup.id.in_(group_ids)
                )
            ).all()
        }

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
        rules = session.scalars(
            select(Rule).where(Rule.id.in_(rule_ids))
        ).all()
        groups = session.scalars(
            select(RuleGroup).where(RuleGroup.id.in_(group_ids))
        ).all()
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.request_id == "plan-cancel-audit-rollback"
            )
        ).all()
    assert plan.status == "approved"
    assert {
        group.id: group.state for group in groups
    } == original_group_states
    assert {
        rule.id: rule.state for rule in rules
    } == original_rule_states
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

    first = _approve(planning, plan_id, **context)
    second = _approve(planning, plan_id, **context)

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
        store = sensitive_store(session)
        audit_contexts = {
            (
                audit.actor,
                store.read(audit, "reason"),
                audit.request_id,
            )
            for audit in audits
        }
    entry_count = sum(rule.kind == "entry" for rule in rules)
    assert len(group_ids) == entry_count + 1
    assert [audit.action for audit in audits].count("plan.approve") == 1
    assert [audit.action for audit in audits].count(
        "rule_group.create"
    ) == len(group_ids)
    assert [audit.action for audit in audits].count(
        "rule.create"
    ) == len(rules)
    assert audit_contexts == {
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
    _approve(planning,
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
        plan_group_ids = set(
            session.scalars(
                select(Rule.group_id).where(
                    Rule.plan_id == plan_id
                )
            ).all()
        )
        expected_count = session.scalar(
            select(func.count())
            .select_from(Rule)
            .where(Rule.group_id.in_(plan_group_ids))
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
        groups = session.scalars(
            select(RuleGroup).where(
                RuleGroup.id.in_(plan_group_ids)
            )
        ).all()
        states = session.scalars(
            select(Rule.state).where(
                Rule.group_id.in_(plan_group_ids)
            )
        ).all()
    assert groups and {group.state for group in groups} == {"canceled"}
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
    _approve(planning,
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
    res = _approve(pln,
        pid,
        actor="operator:test",
        reason="reviewed plan",
        request_id="planning-promotion-gate",
    )  # 0 graded calls -> gate blocks live approval
    assert "promotion gate" in res["error"]
