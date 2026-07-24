"""Cross-process ordering between durable claims and breaker/panic writers."""

from __future__ import annotations

from decimal import Decimal
from multiprocessing.context import SpawnContext

import pytest

from trading_assistant.assets import AssetClass
from trading_assistant.broker.models import OrderResult, OrderStatus
from trading_assistant.db.models import Order, utcnow
from trading_assistant.db.session import create_db_engine, make_session_factory
from trading_assistant.orders.application import ApprovalCommand
from trading_assistant.orders.reconciliation import ReconciliationService
from trading_assistant.orders.repository import OrderRepository
from trading_assistant.orders.submission import OrderSubmissionService
from trading_assistant.risk.breakers import BreakerScope, BreakerService
from trading_assistant.risk.clock import FakeClock
from trading_assistant.risk.engine import RiskResult
from trading_assistant.risk.killswitch import KillSwitch


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
