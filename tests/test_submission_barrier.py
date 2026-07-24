"""Cross-process ordering between durable claims and breaker/panic writers."""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from decimal import Decimal
from multiprocessing.context import SpawnContext

import pytest
from sqlalchemy import func, select, text

from trading_assistant.assets import AssetClass
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    BrokerFill,
    OrderResult,
    OrderStatus,
    Position,
)
from trading_assistant.config import RiskConfig
from trading_assistant.db.models import Fill, Order, utcnow
from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.orders.reconciliation import ReconciliationService
from trading_assistant.orders.repository import OrderRepository
from trading_assistant.orders.snapshot import PortfolioSnapshotService
from trading_assistant.orders.submission import OrderSubmissionService
from trading_assistant.risk.breakers import BreakerScope, BreakerService
from trading_assistant.risk.clock import FakeClock
from trading_assistant.risk.engine import RiskEngine, RiskResult
from trading_assistant.risk.killswitch import KillSwitch
from trading_assistant.risk.submission_barrier import SubmissionBarrier
from trading_assistant.service import TradingService


class _StaticSnapshotService:
    def assemble_for_execution(self, ticker, *, exclude_order_id=None):
        return object()


class _AlwaysApproves:
    def check(self, order, snapshot):
        return RiskResult(approved=True)


class _ProcessBroker:
    reconciliation_key = "process-test"

    def __init__(self, entered, release) -> None:
        self.entered = entered
        self.release = release

    def submit_order(self, order):
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test did not release broker call")
        return OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id="process-broker-order",
            status=OrderStatus.SUBMITTED,
        )


class _ImmediateProcessBroker:
    reconciliation_key = "immediate-process-test"

    def __init__(self, entered) -> None:
        self.entered = entered

    def submit_order(self, order):
        self.entered.set()
        return OrderResult(
            idempotency_key=order.idempotency_key,
            broker_order_id=f"process-broker-order-{order.idempotency_key}",
            status=OrderStatus.SUBMITTED,
        )


class _QueuedSnapshotBroker(MockBroker):
    reconciliation_key = "queued-snapshot-process-test"

    def __init__(self, submit_entered, *, disconnect_after_acceptance) -> None:
        super().__init__(prices={"AAPL": Decimal("100")})
        self.submit_entered = submit_entered
        self.disconnect_after_acceptance = disconnect_after_acceptance

    def submit_order(self, order):
        accepted = super().submit_order(order)
        self.submit_entered.set()
        if self.disconnect_after_acceptance:
            raise ConnectionError("response lost after broker acceptance")
        return accepted


class _LossFillRaceBroker(MockBroker):
    reconciliation_key = "loss-fill-risk-writer"

    def get_fill_activities(self, after=None):
        return [
            BrokerFill(
                broker_fill_id="loss-fill-risk-writer-activity",
                broker_order_id="loss-fill-risk-writer-order",
                ticker="AAPL",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("1"),
                filled_at=utcnow(),
            )
        ]

    def get_order_status(self, order_id):
        assert order_id == "loss-fill-risk-writer-order"
        return OrderResult(
            idempotency_key="loss-fill-risk-writer-client",
            broker_order_id=order_id,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("1"),
        )

    def get_open_orders(self):
        return []


class _DriftRaceBroker(MockBroker):
    reconciliation_key = "drift-risk-writer"

    def get_open_orders(self):
        return [
            OrderResult(
                idempotency_key="unknown-remote-client",
                broker_order_id="unknown-remote-order",
                status=OrderStatus.SUBMITTED,
            )
        ]


class _ObservedPositionQty:
    def __init__(self, drift_observed, release_drift) -> None:
        self.drift_observed = drift_observed
        self.release_drift = release_drift

    def __str__(self) -> str:
        self.drift_observed.set()
        if not self.release_drift.wait(timeout=10):
            raise TimeoutError("test did not release position drift comparison")
        return "10"


class _PositionDriftRaceBroker(MockBroker):
    reconciliation_key = "position-drift-risk-writer"

    def __init__(self, drift_observed, release_drift) -> None:
        super().__init__(prices={"AAPL": Decimal("100")})
        self.drift_observed = drift_observed
        self.release_drift = release_drift

    def get_positions(self):
        return [
            Position(
                "AAPL",
                _ObservedPositionQty(
                    self.drift_observed,
                    self.release_drift,
                ),
                Decimal("100"),
                Decimal("100"),
            )
        ]


class _CancelLossRaceBroker(MockBroker):
    reconciliation_key = "cancel-loss-risk-writer"

    def __init__(self, broker_order_id, client_order_id) -> None:
        super().__init__(prices={"AAPL": Decimal("100")})
        self.broker_order_id = broker_order_id
        self.client_order_id = client_order_id
        self.canceled = OrderResult(
            client_order_id,
            broker_order_id,
            OrderStatus.CANCELED,
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("1"),
        )

    def cancel_order(self, order_id):
        assert order_id == self.broker_order_id
        return self.canceled

    def get_fill_activities(self, after=None):
        return [
            BrokerFill(
                broker_fill_id="cancel-loss-closing-fill",
                broker_order_id=self.broker_order_id,
                ticker="AAPL",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("1"),
                filled_at=utcnow(),
            )
        ]

    def get_order_status(self, order_id):
        assert order_id == self.broker_order_id
        return self.canceled

    def get_open_orders(self):
        return []


class _ProcessStubAgent:
    def chat(self, message):
        return {"reply": message, "tool_calls": []}


def _process_risk_config() -> RiskConfig:
    return RiskConfig(
        ticker_allowlist=["AAPL"],
        max_notional_per_order=500,
        max_position_per_ticker=2000,
        max_portfolio_exposure=10000,
        daily_realized_loss_limit=500,
        price_sanity_pct=5,
        reject_when_market_closed=True,
        proposal_ttl_minutes=15,
    )


def _risk_writer_submission_process(
    db_url,
    order_id,
    snapshot_evaluated,
    release_evaluation,
    broker_entered,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    repository = OrderRepository(factory)
    broker = _QueuedSnapshotBroker(
        broker_entered,
        disconnect_after_acceptance=False,
    )
    config = _process_risk_config().model_copy(
        update={
            "max_daily_total_loss": 50,
            "max_account_drawdown_pct": 10,
        }
    )
    breakers = BreakerService(factory)
    snapshot_service = PortfolioSnapshotService(
        factory,
        broker,
        lambda _asset_class: FakeClock(),
        lambda: {},
        lambda _asset_class: config,
        breakers,
        utcnow,
    )

    class _PauseAfterFirstEvaluation:
        def __init__(self):
            self.calls = 0
            self.engine = RiskEngine(config)

        def check(self, order, snapshot):
            result = self.engine.check(order, snapshot)
            self.calls += 1
            if self.calls == 1:
                snapshot_evaluated.set()
                if not release_evaluation.wait(timeout=10):
                    raise TimeoutError("test did not release risk evaluation")
            return result

    risk = _PauseAfterFirstEvaluation()
    service = OrderSubmissionService(
        repository,
        factory,
        broker,
        snapshot_service,
        lambda _symbol: risk,
        utcnow,
    )
    try:
        result = service.submit(order_id)
        outcome.put(
            ("ok", result.status.value, tuple(result.risk_reasons))
        )
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))


def _loss_fill_writer_process(db_url, started, finished, outcome):
    factory = make_session_factory(create_db_engine(db_url))
    started.set()
    try:
        report = ReconciliationService(
            factory,
            _LossFillRaceBroker(),
            OrderRepository(factory),
        ).reconcile()
        outcome.put(("ok", report.inserted_fills))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _higher_hwm_writer_process(db_url, started, finished, outcome):
    factory = make_session_factory(create_db_engine(db_url))
    config = _process_risk_config()
    broker = MockBroker(
        prices={"AAPL": Decimal("100")},
        buying_power=Decimal("200000"),
    )
    snapshot_service = PortfolioSnapshotService(
        factory,
        broker,
        lambda _asset_class: FakeClock(),
        lambda: {},
        lambda _asset_class: config,
        BreakerService(factory),
        utcnow,
    )
    started.set()
    try:
        snapshot = snapshot_service.assemble_for_execution("AAPL")
        outcome.put(("ok", str(snapshot.account_high_water_mark)))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _drift_reconciliation_process(
    db_url,
    before_writer_release,
    release_writer,
    finished,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    reconciliation = ReconciliationService(
        factory,
        _DriftRaceBroker(),
        OrderRepository(factory),
    )
    original_hold_writer = reconciliation.submission_barrier.hold_writer

    @contextmanager
    def observed_hold_writer():
        with original_hold_writer():
            yield
            before_writer_release.set()
            if not release_writer.wait(timeout=10):
                raise TimeoutError("test did not release reconciliation writer")

    reconciliation.submission_barrier.hold_writer = observed_hold_writer
    try:
        report = reconciliation.reconcile()
        outcome.put(("ok", tuple(report.broker_drift)))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _daily_loss_enforcement_process(
    db_url,
    app_config,
    loss_observed,
    release_loss_writer,
    finished,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    service = TradingService(
        MockBroker(prices={"AAPL": Decimal("100")}),
        factory,
        app_config,
        FakeClock(),
    )
    original_realized = service._realized_pnl_today
    paused = False

    def observed_realized(session, asset_class=AssetClass.EQUITY):
        nonlocal paused
        pnl = original_realized(session, asset_class)
        if asset_class is AssetClass.EQUITY and not paused:
            paused = True
            loss_observed.set()
            if not release_loss_writer.wait(timeout=10):
                raise TimeoutError("test did not release daily-loss writer")
        return pnl

    service._realized_pnl_today = observed_realized
    try:
        outcome.put(("ok", service.enforce_daily_loss_limits()))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _position_reconciliation_process(
    db_url,
    app_config,
    entrypoint,
    drift_observed,
    release_drift,
    finished,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    service = TradingService(
        _PositionDriftRaceBroker(drift_observed, release_drift),
        factory,
        app_config,
        FakeClock(),
    )
    try:
        if entrypoint == "http":
            from fastapi.testclient import TestClient

            from trading_assistant.app.main import create_app

            response = TestClient(
                create_app(
                    service=service,
                    agent=_ProcessStubAgent(),
                    planning=object(),
                    api_token="",
                )
            ).post("/reconcile")
            response.raise_for_status()
            result = response.json()
        else:
            from trading_assistant.daemon.monitor import Monitor
            from trading_assistant.notifications.base import NullNotifier

            result = Monitor(service, NullNotifier()).reconcile()[
                "position_reconciliation"
            ]
        outcome.put(("ok", result))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _immediate_submission_process(
    db_url,
    order_id,
    broker_entered,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    service = OrderSubmissionService(
        OrderRepository(factory),
        factory,
        _ImmediateProcessBroker(broker_entered),
        _StaticSnapshotService(),
        lambda _symbol: _AlwaysApproves(),
        utcnow,
    )
    try:
        result = service.submit(order_id)
        outcome.put(("ok", result.status.value))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))


def _cancel_loss_process(
    db_url,
    app_config,
    cancel_order_id,
    cancel_observed,
    release_cancel,
    finished,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    with factory() as session:
        order = session.get(Order, cancel_order_id)
        broker_order_id = order.broker_order_id
        client_order_id = order.idempotency_key
    service = TradingService(
        _CancelLossRaceBroker(broker_order_id, client_order_id),
        factory,
        app_config,
        FakeClock(),
    )
    original_sync = service.sync_open_orders

    def paused_sync():
        cancel_observed.set()
        if not release_cancel.wait(timeout=10):
            raise TimeoutError("test did not release cancel reconciliation")
        return original_sync()

    service.sync_open_orders = paused_sync
    try:
        outcome.put(("ok", service.cancel_live_order(cancel_order_id)))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _loss_checked_submission_process(
    db_url,
    order_id,
    broker_entered,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    config = _process_risk_config().model_copy(
        update={"max_daily_total_loss": 50}
    )
    broker = _QueuedSnapshotBroker(
        broker_entered,
        disconnect_after_acceptance=False,
    )
    breakers = BreakerService(factory)
    snapshot_service = PortfolioSnapshotService(
        factory,
        broker,
        lambda _asset_class: FakeClock(),
        lambda: {},
        lambda _asset_class: config,
        breakers,
        utcnow,
    )
    service = OrderSubmissionService(
        OrderRepository(factory),
        factory,
        broker,
        snapshot_service,
        lambda _symbol: RiskEngine(config),
        utcnow,
    )
    try:
        result = service.submit(order_id)
        outcome.put(
            ("ok", result.status.value, tuple(result.risk_reasons))
        )
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))


def _writer_intent_path(db_url):
    factory = make_session_factory(create_db_engine(db_url))
    barrier_path = SubmissionBarrier(factory).path
    return barrier_path.with_name(f"{barrier_path.name}.intent")


def _wait_for_writer_intent(intent_path, writer_finished) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        descriptor = os.open(intent_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                return True
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        if writer_finished.is_set():
            return False
        time.sleep(0.01)
    return False


def _observe_barrier_lock(
    barrier,
    path,
    operation,
    observed,
    *,
    after_acquire=False,
) -> None:
    """Signal an exact child-process flock call without changing lock ordering."""
    original_open = barrier._open
    observed_descriptors: set[int] = set()

    def observed_open(open_path):
        descriptor = original_open(open_path)
        if open_path == path:
            observed_descriptors.add(descriptor)
        return descriptor

    original_flock = fcntl.flock

    def observed_flock(descriptor, lock_operation):
        matched = (
            descriptor in observed_descriptors
            and lock_operation == operation
        )
        if matched and not after_acquire:
            observed.set()
        result = original_flock(descriptor, lock_operation)
        if matched and after_acquire:
            observed.set()
        return result

    barrier._open = observed_open
    fcntl.flock = observed_flock


def _counted_submission_process(
    db_url,
    order_id,
    pause_after_first_evaluation,
    snapshot_evaluated,
    release_evaluation,
    main_lock_attempted,
    broker_entered,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    repository = OrderRepository(factory)

    class _CountingRisk:
        def __init__(self):
            self.calls = 0

        def check(self, order, snapshot):
            self.calls += 1
            if self.calls == 1:
                snapshot_evaluated.set()
                if (
                    pause_after_first_evaluation
                    and not release_evaluation.wait(timeout=10)
                ):
                    raise TimeoutError("test did not release risk evaluation")
            return RiskResult(approved=True)

    risk = _CountingRisk()
    service = OrderSubmissionService(
        repository,
        factory,
        _ImmediateProcessBroker(broker_entered),
        _StaticSnapshotService(),
        lambda _symbol: risk,
        utcnow,
    )
    _observe_barrier_lock(
        service.submission_barrier,
        service.submission_barrier.path,
        fcntl.LOCK_EX,
        main_lock_attempted,
    )
    try:
        result = service.submit(order_id)
        outcome.put(("ok", result.status.value, risk.calls))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}", risk.calls))


def _observed_breaker_writer_process(
    db_url,
    started,
    intent_acquired,
    finished,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    breakers = BreakerService(factory)
    _observe_barrier_lock(
        breakers.submission_barrier,
        breakers.submission_barrier.intent_path,
        fcntl.LOCK_SH,
        intent_acquired,
        after_acquire=True,
    )
    started.set()
    try:
        breakers.trip(
            BreakerScope.data(AssetClass.EQUITY),
            "writer priority regression",
            "daemon:writer-priority",
        )
        outcome.put(("ok", "breaker"))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _queued_snapshot_submission_process(
    db_url,
    order_id,
    pause_before_claim,
    claim_entered,
    release_claim,
    submission_started,
    snapshot_completed,
    broker_entered,
    disconnect_after_acceptance,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    repository = OrderRepository(factory)
    if pause_before_claim:
        original_claim = repository.claim_submission

        def paused_claim(*args, **kwargs):
            claim_entered.set()
            if not release_claim.wait(timeout=10):
                raise TimeoutError("test did not release pre-claim pause")
            return original_claim(*args, **kwargs)

        repository.claim_submission = paused_claim

    broker = _QueuedSnapshotBroker(
        broker_entered,
        disconnect_after_acceptance=disconnect_after_acceptance,
    )
    config = _process_risk_config()
    breakers = BreakerService(factory)
    snapshot_service = PortfolioSnapshotService(
        factory,
        broker,
        lambda _asset_class: FakeClock(),
        lambda: {},
        lambda _asset_class: config,
        breakers,
        utcnow,
    )
    original_assemble = snapshot_service.assemble_for_execution

    def observed_assemble(*args, **kwargs):
        snapshot = original_assemble(*args, **kwargs)
        snapshot_completed.set()
        return snapshot

    snapshot_service.assemble_for_execution = observed_assemble
    service = OrderSubmissionService(
        repository,
        factory,
        broker,
        snapshot_service,
        lambda _symbol: RiskEngine(config),
        utcnow,
    )
    try:
        submission_started.set()
        result = service.submit(order_id)
        outcome.put(
            ("ok", result.status.value, tuple(result.risk_reasons))
        )
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))


def _submission_process(
    db_url,
    order_id,
    pause_after_claim,
    claim_committed,
    release_claim,
    broker_entered,
    release_broker,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    repository = OrderRepository(factory)
    if pause_after_claim:
        original_claim = repository.claim_submission

        def observed_claim(*args, **kwargs):
            claimed = original_claim(*args, **kwargs)
            if claimed:
                claim_committed.set()
                if not release_claim.wait(timeout=10):
                    raise TimeoutError("test did not release committed claim")
            return claimed

        repository.claim_submission = observed_claim
    service = OrderSubmissionService(
        repository,
        factory,
        _ProcessBroker(broker_entered, release_broker),
        _StaticSnapshotService(),
        lambda _symbol: _AlwaysApproves(),
        utcnow,
    )
    try:
        result = service.submit(order_id)
        outcome.put(("ok", result.status.value))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))


def _writer_process(db_url, writer_kind, started, finished, outcome):
    factory = make_session_factory(create_db_engine(db_url))
    repository = OrderRepository(factory)
    breakers = BreakerService(factory)
    started.set()
    try:
        if writer_kind == "breaker":
            breakers.trip(
                BreakerScope.data(AssetClass.EQUITY),
                "cross-process feed fault",
                "daemon:process-test",
            )
        elif writer_kind == "panic":
            from trading_assistant.broker.mock import MockBroker

            ReconciliationService(
                factory,
                MockBroker(),
                repository,
                breakers,
            ).panic("operator:process-test", "cross-process panic")
        else:
            with factory() as session:
                KillSwitch.trip(
                    session,
                    "cross-process compatibility trip",
                    AssetClass.EQUITY,
                )
        outcome.put(("ok", writer_kind))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _active_transaction_compatibility_writer_process(
    db_url,
    sqlite_locked,
    finished,
    outcome,
):
    factory = make_session_factory(create_db_engine(db_url))
    try:
        with factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            sqlite_locked.set()
            try:
                KillSwitch.trip(
                    session,
                    "must reject lock inversion",
                    AssetClass.EQUITY,
                )
            except RuntimeError as exc:
                outcome.put(("rejected", str(exc)))
            else:
                outcome.put(("unexpected", "trip committed"))
    except BaseException as exc:
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _approved_order(service) -> int:
    order_id = service.propose_order(
        "AAPL", "buy", "market", notional="100"
    )["order_id"]
    service.order_application.approve(
        ApprovalCommand(
            order_id,
            "operator:avi",
            "reviewed",
            utcnow(),
        )
    )
    return order_id


def _join(process, *release_events) -> None:
    for event in release_events:
        event.set()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail(f"process {process.name} did not terminate")
    assert process.exitcode == 0


@pytest.mark.parametrize(
    ("writer_kind", "scope"),
    [
        ("breaker", BreakerScope.data(AssetClass.EQUITY)),
        ("panic", BreakerScope.operator_global()),
        ("compatibility", BreakerScope.loss(AssetClass.EQUITY)),
    ],
)
def test_writer_committed_before_claim_blocks_cross_process_broker_send(
    make_service,
    db_url,
    writer_kind,
    scope,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_id = _approved_order(service)
    writer_started = context.Event()
    writer_finished = context.Event()
    writer_outcome = context.Queue()
    writer = context.Process(
        target=_writer_process,
        args=(
            db_url,
            writer_kind,
            writer_started,
            writer_finished,
            writer_outcome,
        ),
    )
    writer.start()
    assert writer_started.wait(timeout=10)
    assert writer_finished.wait(timeout=10)
    _join(writer)
    assert writer_outcome.get(timeout=2) == ("ok", writer_kind)
    assert service.breakers.is_tripped(scope) is True

    claim_committed = context.Event()
    release_claim = context.Event()
    broker_entered = context.Event()
    release_broker = context.Event()
    submission_outcome = context.Queue()
    submission = context.Process(
        target=_submission_process,
        args=(
            db_url,
            order_id,
            False,
            claim_committed,
            release_claim,
            broker_entered,
            release_broker,
            submission_outcome,
        ),
    )
    submission.start()
    _join(submission, release_broker)

    assert submission_outcome.get(timeout=2) == (
        "ok",
        OrderStatus.APPROVAL_RECORDED.value,
    )
    assert broker_entered.is_set() is False


@pytest.mark.parametrize(
    ("writer_kind", "scope"),
    [
        ("breaker", BreakerScope.data(AssetClass.EQUITY)),
        ("panic", BreakerScope.operator_global()),
        ("compatibility", BreakerScope.loss(AssetClass.EQUITY)),
    ],
)
def test_writer_requested_after_claim_waits_until_cross_process_broker_send_finishes(
    make_service,
    db_url,
    writer_kind,
    scope,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_id = _approved_order(service)
    claim_committed = context.Event()
    release_claim = context.Event()
    broker_entered = context.Event()
    release_broker = context.Event()
    submission_outcome = context.Queue()
    submission = context.Process(
        target=_submission_process,
        args=(
            db_url,
            order_id,
            True,
            claim_committed,
            release_claim,
            broker_entered,
            release_broker,
            submission_outcome,
        ),
    )
    submission.start()
    assert claim_committed.wait(timeout=10)

    writer_started = context.Event()
    writer_finished = context.Event()
    writer_outcome = context.Queue()
    writer = context.Process(
        target=_writer_process,
        args=(
            db_url,
            writer_kind,
            writer_started,
            writer_finished,
            writer_outcome,
        ),
    )
    writer.start()
    assert writer_started.wait(timeout=10)
    try:
        assert writer_finished.wait(timeout=0.5) is False
        assert service.breakers.is_tripped(scope) is False

        release_claim.set()
        assert broker_entered.wait(timeout=10)
        assert writer_finished.wait(timeout=0.5) is False
        assert service.breakers.is_tripped(scope) is False
    finally:
        _join(submission, release_claim, release_broker)
        _join(writer)

    assert submission_outcome.get(timeout=2) == (
        "ok",
        OrderStatus.SUBMITTED.value,
    )
    assert writer_outcome.get(timeout=2) == ("ok", writer_kind)
    assert service.breakers.is_tripped(scope) is True
    with service.session_factory() as session:
        assert session.get(Order, order_id).status == OrderStatus.SUBMITTED.value


def test_compatibility_writer_rejects_sqlite_lock_before_waiting_for_barrier(
    make_service,
    db_url,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_id = _approved_order(service)
    claim_committed = context.Event()
    release_claim = context.Event()
    broker_entered = context.Event()
    release_broker = context.Event()
    submission_outcome = context.Queue()
    submission = context.Process(
        target=_submission_process,
        args=(
            db_url,
            order_id,
            False,
            claim_committed,
            release_claim,
            broker_entered,
            release_broker,
            submission_outcome,
        ),
    )
    submission.start()
    assert broker_entered.wait(timeout=10)

    sqlite_locked = context.Event()
    writer_finished = context.Event()
    writer_outcome = context.Queue()
    writer = context.Process(
        target=_active_transaction_compatibility_writer_process,
        args=(
            db_url,
            sqlite_locked,
            writer_finished,
            writer_outcome,
        ),
    )
    writer.start()
    assert sqlite_locked.wait(timeout=10)
    finished_before_barrier_release = writer_finished.wait(timeout=1)
    if not finished_before_barrier_release:
        writer.terminate()
        writer.join(timeout=5)
    else:
        _join(writer)
    _join(submission, release_broker)

    assert finished_before_barrier_release is True
    outcome_kind, detail = writer_outcome.get(timeout=2)
    assert outcome_kind == "rejected"
    assert "active transaction" in detail
    assert submission_outcome.get(timeout=2) == (
        "ok",
        OrderStatus.SUBMITTED.value,
    )


def test_queued_submission_rechecks_acceptance_unknown_under_process_barrier(
    make_service,
    db_url,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    predecessor_id = _approved_order(service)
    follower_id = _approved_order(service)

    predecessor_claim_entered = context.Event()
    release_predecessor_claim = context.Event()
    predecessor_submission_started = context.Event()
    predecessor_snapshot_completed = context.Event()
    predecessor_broker_entered = context.Event()
    predecessor_outcome = context.Queue()
    predecessor = context.Process(
        target=_queued_snapshot_submission_process,
        args=(
            db_url,
            predecessor_id,
            True,
            predecessor_claim_entered,
            release_predecessor_claim,
            predecessor_submission_started,
            predecessor_snapshot_completed,
            predecessor_broker_entered,
            True,
            predecessor_outcome,
        ),
    )
    predecessor.start()
    assert predecessor_claim_entered.wait(timeout=10)

    follower_claim_entered = context.Event()
    unused_release_follower_claim = context.Event()
    follower_submission_started = context.Event()
    follower_snapshot_completed = context.Event()
    follower_broker_entered = context.Event()
    follower_outcome = context.Queue()
    follower = context.Process(
        target=_queued_snapshot_submission_process,
        args=(
            db_url,
            follower_id,
            False,
            follower_claim_entered,
            unused_release_follower_claim,
            follower_submission_started,
            follower_snapshot_completed,
            follower_broker_entered,
            False,
            follower_outcome,
        ),
    )
    follower.start()
    assert follower_submission_started.wait(timeout=10)

    # Before the fix, the follower completes this snapshot before waiting on
    # the barrier. After the fix, it blocks on the barrier and reads only after
    # the predecessor persists its unresolved acceptance state.
    snapshot_completed_before_release = follower_snapshot_completed.wait(
        timeout=2
    )
    release_predecessor_claim.set()
    _join(predecessor, release_predecessor_claim)
    _join(follower)

    assert snapshot_completed_before_release is False
    assert predecessor_broker_entered.is_set() is True
    assert predecessor_outcome.get(timeout=2) == (
        "ok",
        OrderStatus.ACCEPTANCE_UNKNOWN.value,
        (),
    )
    assert follower_outcome.get(timeout=2) == (
        "ok",
        OrderStatus.REJECTED.value,
        ("broker reconciliation is not current",),
    )
    assert follower_broker_entered.is_set() is False
    with service.session_factory() as session:
        assert (
            session.get(Order, predecessor_id).status
            == OrderStatus.ACCEPTANCE_UNKNOWN.value
        )
        assert (
            session.get(Order, follower_id).status
            == OrderStatus.REJECTED.value
        )


def test_reconciliation_drift_is_durable_before_writer_interval_release(
    make_service,
    db_url,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_id = _approved_order(service)
    before_writer_release = context.Event()
    release_writer = context.Event()
    writer_finished = context.Event()
    writer_outcome = context.Queue()
    writer = context.Process(
        target=_drift_reconciliation_process,
        args=(
            db_url,
            before_writer_release,
            release_writer,
            writer_finished,
            writer_outcome,
        ),
    )
    writer.start()
    assert before_writer_release.wait(timeout=10)
    drift_was_durable = service.breakers.is_tripped(
        BreakerScope.broker_drift()
    )

    claim_committed = context.Event()
    unused_release_claim = context.Event()
    broker_entered = context.Event()
    release_broker = context.Event()
    submission_outcome = context.Queue()
    submission = context.Process(
        target=_submission_process,
        args=(
            db_url,
            order_id,
            False,
            claim_committed,
            unused_release_claim,
            broker_entered,
            release_broker,
            submission_outcome,
        ),
    )
    submission.start()
    broker_entered_before_release = broker_entered.wait(timeout=1)
    try:
        release_writer.set()
    finally:
        _join(writer, release_writer)
        _join(submission, release_broker)

    assert drift_was_durable is True
    assert broker_entered_before_release is False
    writer_result = writer_outcome.get(timeout=2)
    assert writer_result[0] == "ok"
    assert any("has no local order" in item for item in writer_result[1])
    assert submission_outcome.get(timeout=2) == (
        "ok",
        OrderStatus.APPROVAL_RECORDED.value,
    )
    assert broker_entered.is_set() is False


def test_daily_loss_observation_and_trip_are_one_writer_interval(
    make_service,
    app_config,
    db_url,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_id = _approved_order(service)
    now = utcnow()
    with service.session_factory() as session:
        session.add_all(
            [
                Fill(
                    ticker="AAPL",
                    side="buy",
                    qty=Decimal("10"),
                    price=Decimal("100"),
                    broker_fill_id="daily-loss-open",
                    filled_at=now,
                ),
                Fill(
                    ticker="AAPL",
                    side="sell",
                    qty=Decimal("10"),
                    price=Decimal("1"),
                    broker_fill_id="daily-loss-close",
                    filled_at=now,
                ),
            ]
        )
        session.commit()

    loss_observed = context.Event()
    release_loss_writer = context.Event()
    loss_writer_finished = context.Event()
    loss_writer_outcome = context.Queue()
    loss_writer = context.Process(
        target=_daily_loss_enforcement_process,
        args=(
            db_url,
            app_config,
            loss_observed,
            release_loss_writer,
            loss_writer_finished,
            loss_writer_outcome,
        ),
    )
    loss_writer.start()
    assert loss_observed.wait(timeout=10)

    claim_committed = context.Event()
    unused_release_claim = context.Event()
    broker_entered = context.Event()
    release_broker = context.Event()
    submission_outcome = context.Queue()
    submission = context.Process(
        target=_submission_process,
        args=(
            db_url,
            order_id,
            False,
            claim_committed,
            unused_release_claim,
            broker_entered,
            release_broker,
            submission_outcome,
        ),
    )
    submission.start()
    broker_entered_before_trip = broker_entered.wait(timeout=1)
    try:
        release_loss_writer.set()
    finally:
        release_broker.set()
        _join(submission, release_broker)
        _join(loss_writer, release_loss_writer)

    assert broker_entered_before_trip is False
    assert loss_writer_outcome.get(timeout=2) == (
        "ok",
        {"equity": True, "crypto": False},
    )
    assert service.breakers.is_tripped(
        BreakerScope.loss(AssetClass.EQUITY)
    ) is True
    assert submission_outcome.get(timeout=2) == (
        "ok",
        OrderStatus.APPROVAL_RECORDED.value,
    )
    assert broker_entered.is_set() is False


def test_cancel_loss_reconciliation_blocks_concurrent_submission(
    make_service,
    db_url,
    app_config,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    follower_id = _approved_order(service)
    with service.session_factory() as session:
        session.add(
            Fill(
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id="cancel-loss-opening-fill",
                filled_at=utcnow(),
            )
        )
        cancel_order = Order(
            idempotency_key="cancel-loss-client-order",
            ticker="AAPL",
            side="sell",
            order_type="market",
            qty=Decimal("1"),
            status=OrderStatus.SUBMITTED.value,
            broker_order_id="cancel-loss-broker-order",
            acceptance_state="submitted",
        )
        session.add(cancel_order)
        session.commit()
        cancel_order_id = cancel_order.id

    cancel_observed = context.Event()
    release_cancel = context.Event()
    cancel_finished = context.Event()
    cancel_outcome = context.Queue()
    cancel = context.Process(
        target=_cancel_loss_process,
        args=(
            db_url,
            app_config,
            cancel_order_id,
            cancel_observed,
            release_cancel,
            cancel_finished,
            cancel_outcome,
        ),
    )
    cancel.start()
    assert cancel_observed.wait(timeout=10)

    broker_entered = context.Event()
    submission_outcome = context.Queue()
    submission = context.Process(
        target=_loss_checked_submission_process,
        args=(
            db_url,
            follower_id,
            broker_entered,
            submission_outcome,
        ),
    )
    submission.start()
    try:
        assert broker_entered.wait(timeout=0.75) is False
        assert cancel_finished.is_set() is False
    finally:
        release_cancel.set()
        _join(cancel, release_cancel)
        _join(submission)

    outcome_kind, result = cancel_outcome.get(timeout=2)
    assert outcome_kind == "ok"
    assert result["status"] == OrderStatus.CANCELED.value
    submission_result = submission_outcome.get(timeout=2)
    assert submission_result[0:2] == (
        "ok",
        OrderStatus.REJECTED.value,
    )
    assert "daily total-loss limit reached" in submission_result[2]
    assert broker_entered.is_set() is False
    with service.session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(Fill)
            .where(
                Fill.broker_fill_id == "cancel-loss-closing-fill"
            )
        ) == 1


@pytest.mark.parametrize(
    ("entrypoint", "expected_actor"),
    [
        ("http", "operator:api-token"),
        ("startup", "daemon:startup"),
    ],
)
def test_position_drift_entrypoint_blocks_submission_until_durable_trip(
    make_service,
    db_url,
    app_config,
    entrypoint,
    expected_actor,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_id = _approved_order(service)

    drift_observed = context.Event()
    release_drift = context.Event()
    reconciliation_finished = context.Event()
    reconciliation_outcome = context.Queue()
    reconciliation = context.Process(
        target=_position_reconciliation_process,
        args=(
            db_url,
            app_config,
            entrypoint,
            drift_observed,
            release_drift,
            reconciliation_finished,
            reconciliation_outcome,
        ),
    )
    reconciliation.start()
    assert drift_observed.wait(timeout=10)

    broker_entered = context.Event()
    submission_outcome = context.Queue()
    submission = context.Process(
        target=_immediate_submission_process,
        args=(
            db_url,
            order_id,
            broker_entered,
            submission_outcome,
        ),
    )
    submission.start()
    try:
        assert broker_entered.wait(timeout=0.75) is False
        assert reconciliation_finished.is_set() is False
    finally:
        release_drift.set()
        _join(reconciliation, release_drift)
        _join(submission)

    outcome_kind, result = reconciliation_outcome.get(timeout=2)
    assert outcome_kind == "ok"
    assert result["reconciled"] is False
    assert result["drift"]["AAPL"] == {"broker": "10", "local": "0"}
    assert submission_outcome.get(timeout=2) == (
        "ok",
        OrderStatus.APPROVAL_RECORDED.value,
    )
    assert broker_entered.is_set() is False
    state = service.breakers.get(BreakerScope.broker_drift())
    assert state is not None and state.tripped is True
    assert state.actor == expected_actor


def test_loss_fill_writer_queued_after_snapshot_prevents_broker_send(
    make_service,
    db_url,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_id = _approved_order(service)
    with service.session_factory() as session:
        session.add(
            Fill(
                ticker="AAPL",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                broker_fill_id="loss-fill-opening-lot",
                filled_at=utcnow(),
            )
        )
        session.add(
            Order(
                idempotency_key="loss-fill-risk-writer-client",
                ticker="AAPL",
                side="sell",
                order_type="market",
                qty=Decimal("1"),
                status=OrderStatus.SUBMITTED.value,
                broker_order_id="loss-fill-risk-writer-order",
                acceptance_state="submitted",
            )
        )
        session.commit()

    snapshot_evaluated = context.Event()
    release_evaluation = context.Event()
    broker_entered = context.Event()
    submission_outcome = context.Queue()
    submission = context.Process(
        target=_risk_writer_submission_process,
        args=(
            db_url,
            order_id,
            snapshot_evaluated,
            release_evaluation,
            broker_entered,
            submission_outcome,
        ),
    )
    submission.start()
    assert snapshot_evaluated.wait(timeout=10)

    writer_started = context.Event()
    writer_finished = context.Event()
    writer_outcome = context.Queue()
    writer = context.Process(
        target=_loss_fill_writer_process,
        args=(
            db_url,
            writer_started,
            writer_finished,
            writer_outcome,
        ),
    )
    writer.start()
    assert writer_started.wait(timeout=10)
    intent_observed = _wait_for_writer_intent(
        _writer_intent_path(db_url),
        writer_finished,
    )
    try:
        release_evaluation.set()
    finally:
        _join(submission, release_evaluation)
        _join(writer)

    assert intent_observed is True
    assert writer_outcome.get(timeout=2) == ("ok", 1)
    result = submission_outcome.get(timeout=2)
    assert result[0:2] == ("ok", OrderStatus.REJECTED.value)
    assert "daily total-loss limit reached" in result[2]
    assert broker_entered.is_set() is False


def test_higher_hwm_writer_queued_after_snapshot_prevents_broker_send(
    make_service,
    db_url,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_id = _approved_order(service)

    snapshot_evaluated = context.Event()
    release_evaluation = context.Event()
    broker_entered = context.Event()
    submission_outcome = context.Queue()
    submission = context.Process(
        target=_risk_writer_submission_process,
        args=(
            db_url,
            order_id,
            snapshot_evaluated,
            release_evaluation,
            broker_entered,
            submission_outcome,
        ),
    )
    submission.start()
    assert snapshot_evaluated.wait(timeout=10)

    writer_started = context.Event()
    writer_finished = context.Event()
    writer_outcome = context.Queue()
    writer = context.Process(
        target=_higher_hwm_writer_process,
        args=(
            db_url,
            writer_started,
            writer_finished,
            writer_outcome,
        ),
    )
    writer.start()
    assert writer_started.wait(timeout=10)
    intent_observed = _wait_for_writer_intent(
        _writer_intent_path(db_url),
        writer_finished,
    )
    try:
        release_evaluation.set()
    finally:
        _join(submission, release_evaluation)
        _join(writer)

    assert intent_observed is True
    assert writer_outcome.get(timeout=2) == ("ok", "200000.000000")
    result = submission_outcome.get(timeout=2)
    assert result[0:2] == ("ok", OrderStatus.REJECTED.value)
    assert "account drawdown limit reached" in result[2]
    assert broker_entered.is_set() is False


@pytest.mark.parametrize("follower_count", [1, 3])
def test_submission_waiters_make_progress_without_invalidating_active_submission(
    make_service,
    db_url,
    follower_count,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_ids = [
        _approved_order(service) for _index in range(follower_count + 1)
    ]
    release_active = context.Event()
    processes = []
    snapshots = []
    main_attempts = []
    broker_calls = []
    outcomes = []

    for index, order_id in enumerate(order_ids):
        snapshot_evaluated = context.Event()
        main_lock_attempted = context.Event()
        broker_entered = context.Event()
        outcome = context.Queue()
        process = context.Process(
            target=_counted_submission_process,
            args=(
                db_url,
                order_id,
                index == 0,
                snapshot_evaluated,
                release_active,
                main_lock_attempted,
                broker_entered,
                outcome,
            ),
        )
        processes.append(process)
        snapshots.append(snapshot_evaluated)
        main_attempts.append(main_lock_attempted)
        broker_calls.append(broker_entered)
        outcomes.append(outcome)

    processes[0].start()
    assert snapshots[0].wait(timeout=10)
    for process in processes[1:]:
        process.start()
    followers_reached_main = [
        event.wait(timeout=2) for event in main_attempts[1:]
    ]
    try:
        release_active.set()
    finally:
        for process in processes:
            _join(process, release_active)

    assert all(followers_reached_main)
    assert [queue.get(timeout=2) for queue in outcomes] == [
        ("ok", OrderStatus.SUBMITTED.value, 1)
        for _order_id in order_ids
    ]
    assert all(event.is_set() for event in broker_calls)


@pytest.mark.parametrize("follower_count", [1, 3])
def test_real_writer_has_priority_over_queued_submission_waiters_without_deadlock(
    make_service,
    db_url,
    follower_count,
):
    context: SpawnContext = __import__("multiprocessing").get_context("spawn")
    service = make_service()
    order_ids = [
        _approved_order(service) for _index in range(follower_count + 1)
    ]
    release_active = context.Event()
    submissions = []
    snapshots = []
    main_attempts = []
    broker_calls = []
    submission_outcomes = []

    for index, order_id in enumerate(order_ids):
        snapshot_evaluated = context.Event()
        main_lock_attempted = context.Event()
        broker_entered = context.Event()
        outcome = context.Queue()
        process = context.Process(
            target=_counted_submission_process,
            args=(
                db_url,
                order_id,
                index == 0,
                snapshot_evaluated,
                release_active,
                main_lock_attempted,
                broker_entered,
                outcome,
            ),
        )
        submissions.append(process)
        snapshots.append(snapshot_evaluated)
        main_attempts.append(main_lock_attempted)
        broker_calls.append(broker_entered)
        submission_outcomes.append(outcome)

    submissions[0].start()
    assert snapshots[0].wait(timeout=10)
    for submission in submissions[1:]:
        submission.start()
    followers_reached_main = [
        event.wait(timeout=2) for event in main_attempts[1:]
    ]

    writer_started = context.Event()
    writer_intent_acquired = context.Event()
    writer_finished = context.Event()
    writer_outcome = context.Queue()
    writer = context.Process(
        target=_observed_breaker_writer_process,
        args=(
            db_url,
            writer_started,
            writer_intent_acquired,
            writer_finished,
            writer_outcome,
        ),
    )
    writer.start()
    assert writer_started.wait(timeout=10)
    writer_advertised_before_release = writer_intent_acquired.wait(timeout=2)
    try:
        release_active.set()
    finally:
        for submission in submissions:
            _join(submission, release_active)
        _join(writer)

    assert all(followers_reached_main)
    assert writer_advertised_before_release is True
    assert writer_outcome.get(timeout=2) == ("ok", "breaker")
    assert writer_finished.is_set() is True
    assert service.breakers.is_tripped(
        BreakerScope.data(AssetClass.EQUITY)
    ) is True
    assert [queue.get(timeout=2)[0:2] for queue in submission_outcomes] == [
        ("ok", OrderStatus.APPROVAL_RECORDED.value)
        for _order_id in order_ids
    ]
    assert all(event.is_set() is False for event in broker_calls)
