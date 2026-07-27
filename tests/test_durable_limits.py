from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError

import trading_assistant.app.limits as limits_module
from trading_assistant.app.limits import (
    ConcurrencyLeaseService,
    DurableRateLimiter,
    LeaseDecision,
    LimitDecision,
    LimitSpec,
    LimitStoreUnavailable,
)
from trading_assistant.db.models import AuditEvent, ConcurrencyLease, RateWindow


UTC = timezone.utc


def _at_noon() -> datetime:
    return datetime(2026, 7, 27, 12, tzinfo=UTC)


def _shorten_sqlite_busy_timeout(engine) -> None:
    def set_short_timeout(dbapi_connection, _connection_record, _proxy) -> None:
        dbapi_connection.execute("PRAGMA busy_timeout=1")

    event.listen(engine, "checkout", set_short_timeout)


class _CountingSessionFactory:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.delegate()


class _LifecycleFailureContext:
    def __init__(self, delegate, phase: str, error: Exception) -> None:
        self.delegate = delegate
        self.phase = phase
        self.error = error

    def __enter__(self):
        if self.phase == "enter":
            raise self.error
        session = self.delegate.__enter__()
        if self.phase == "body":
            return _LifecycleFailureSession(session, self.error)
        return session

    def __exit__(self, exc_type, exc_value, traceback):
        result = self.delegate.__exit__(exc_type, exc_value, traceback)
        if self.phase == "exit":
            raise self.error
        return result


class _LifecycleFailureSession:
    def __init__(self, delegate, error: Exception) -> None:
        self.delegate = delegate
        self.error = error

    def execute(self, *_args, **_kwargs):
        raise self.error

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class _LifecycleFailureFactory:
    def __init__(self, delegate, phase: str, error: Exception) -> None:
        self.delegate = delegate
        self.phase = phase
        self.error = error

    def __call__(self):
        if self.phase == "factory":
            raise self.error
        return _LifecycleFailureContext(
            self.delegate(),
            self.phase,
            self.error,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("principal_requests", 0),
        ("principal_requests", -1),
        ("global_requests", 0),
        ("global_requests", -1),
        ("window_seconds", 0),
        ("window_seconds", -1),
        ("principal_daily_requests", 0),
        ("principal_daily_requests", -1),
        ("global_daily_requests", 0),
        ("global_daily_requests", -1),
    ],
)
def test_non_positive_limit_spec_is_rejected_before_session_or_write(
    field,
    value,
    session_factory,
):
    values = {
        "principal_requests": 2,
        "global_requests": 3,
        "window_seconds": 60,
        "principal_daily_requests": 4,
        "global_daily_requests": 5,
    }
    values[field] = value
    counting_factory = _CountingSessionFactory(session_factory)
    limiter = DurableRateLimiter(counting_factory)

    with pytest.raises(ValueError):
        spec = LimitSpec("invalid", **values)
        limiter.consume_pair(spec, principal="operator", now=_at_noon())

    assert counting_factory.calls == 0
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(RateWindow)
        ) == 0


@pytest.mark.parametrize("ttl_seconds", [0, -1])
def test_non_positive_lease_ttl_is_rejected_before_session_or_write(
    ttl_seconds,
    session_factory,
):
    counting_factory = _CountingSessionFactory(session_factory)
    service = ConcurrencyLeaseService(counting_factory)

    with pytest.raises(ValueError):
        service.acquire(
            "invalid:lease",
            owner="worker",
            ttl_seconds=ttl_seconds,
            now=_at_noon(),
        )

    assert counting_factory.calls == 0
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ConcurrencyLease)
        ) == 0


def test_limit_survives_new_service_instance(session_factory):
    now = _at_noon()
    spec = LimitSpec(
        "chat",
        principal_requests=2,
        global_requests=3,
        window_seconds=60,
    )
    first = DurableRateLimiter(session_factory)
    assert first.consume_pair(spec, principal="operator", now=now).allowed
    assert first.consume_pair(spec, principal="operator", now=now).allowed

    restarted = DurableRateLimiter(session_factory)
    denied = restarted.consume_pair(spec, principal="operator", now=now)

    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.retry_after_seconds == 60
    assert denied.reset_at == now + timedelta(seconds=60)


def test_parallel_consumers_cannot_overspend(session_factory):
    limiter_a = DurableRateLimiter(session_factory)
    limiter_b = DurableRateLimiter(session_factory)
    spec = LimitSpec(
        "analysis",
        principal_requests=1,
        global_requests=1,
        window_seconds=60,
    )
    barrier = threading.Barrier(2)

    def consume(limiter):
        barrier.wait()
        return limiter.consume_pair(spec, principal="same").allowed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, (limiter_a, limiter_b)))

    assert sorted(results) == [False, True]


def test_parallel_principals_fail_closed_while_real_insert_holds_lock(
    engine,
    session_factory,
):
    limiter_a = DurableRateLimiter(session_factory)
    limiter_b = DurableRateLimiter(session_factory)
    spec = LimitSpec(
        "broker_read",
        principal_requests=2,
        global_requests=1,
        window_seconds=60,
    )
    role = threading.local()
    holder_insert_executed = threading.Event()
    second_attempted_begin = threading.Event()
    release_holder = threading.Event()
    connection_ids: dict[str, int] = {}
    busy_timeouts: dict[str, int] = {}

    def is_begin_immediate(statement: str) -> bool:
        return statement.strip().upper() == "BEGIN IMMEDIATE"

    def is_rate_window_insert(statement: str) -> bool:
        return statement.lstrip().upper().startswith(
            "INSERT INTO RATE_WINDOWS"
        )

    def configure_checked_out_connection(
        dbapi_connection,
        _connection_record,
        _connection_proxy,
    ):
        current_role = getattr(role, "name", "")
        timeout_ms = 1 if current_role == "contender" else 5000
        dbapi_connection.execute(
            f"PRAGMA busy_timeout={timeout_ms}"
        )
        if current_role in {"holder", "contender"}:
            connection_ids[current_role] = id(dbapi_connection)
            busy_timeouts[current_role] = dbapi_connection.execute(
                "PRAGMA busy_timeout"
            ).fetchone()[0]

    def before_cursor_execute(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        if not is_begin_immediate(statement):
            return
        current_role = getattr(role, "name", "")
        if not current_role:
            return
        if current_role == "contender":
            second_attempted_begin.set()

    def after_cursor_execute(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        if (
            is_rate_window_insert(statement)
            and getattr(role, "name", "") == "holder"
        ):
            holder_insert_executed.set()
            if not release_holder.wait(timeout=5):
                raise AssertionError(
                    "holder was not released after contender attempt"
                )

    event.listen(engine, "checkout", configure_checked_out_connection)
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    event.listen(engine, "after_cursor_execute", after_cursor_execute)

    def consume(limiter, principal, current_role):
        role.name = current_role
        return limiter.consume_pair(spec, principal=principal).allowed

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                holder = pool.submit(
                    consume,
                    limiter_a,
                    "operator-a",
                    "holder",
                )
                assert holder_insert_executed.wait(timeout=5)
                contender = pool.submit(
                    consume,
                    limiter_b,
                    "operator-b",
                    "contender",
                )
                with pytest.raises(LimitStoreUnavailable):
                    contender.result(timeout=5)
            finally:
                release_holder.set()
            holder_allowed = holder.result(timeout=10)

        role.name = "post_commit_retry"
        post_commit_retry = DurableRateLimiter(
            session_factory
        ).consume_pair(
            spec,
            principal="operator-b",
        )

        role.name = "inspect"
        with session_factory() as session:
            persisted = {
                window.bucket_key: window.hits
                for window in session.scalars(
                    select(RateWindow).where(
                        RateWindow.policy_name == "broker_read"
                    )
                )
            }
    finally:
        release_holder.set()
        event.remove(
            engine,
            "checkout",
            configure_checked_out_connection,
        )
        event.remove(
            engine,
            "before_cursor_execute",
            before_cursor_execute,
        )
        event.remove(
            engine,
            "after_cursor_execute",
            after_cursor_execute,
        )

    expected_keys = {
        hashlib.sha256(
            b"broker_read\0principal_window\0operator-a"
        ).hexdigest(),
        hashlib.sha256(
            b"broker_read\0global_window\0"
        ).hexdigest(),
    }
    assert holder_allowed is True
    assert second_attempted_begin.is_set()
    assert connection_ids["holder"] != connection_ids["contender"]
    assert busy_timeouts == {"holder": 5000, "contender": 1}
    assert persisted == {key: 1 for key in expected_keys}
    assert post_commit_retry.allowed is False


def test_global_denial_rolls_back_principal_increment(
    session_factory,
):
    now = _at_noon()
    spec = LimitSpec(
        "paired",
        principal_requests=2,
        global_requests=1,
        window_seconds=60,
    )
    limiter = DurableRateLimiter(session_factory)
    assert limiter.consume_pair(spec, principal="first", now=now).allowed

    denied = limiter.consume_pair(spec, principal="second", now=now)

    assert denied.allowed is False
    assert denied.retry_after_seconds == 60
    with session_factory() as session:
        windows = session.scalars(
            select(RateWindow).where(RateWindow.policy_name == "paired")
        ).all()
    assert len(windows) == 2
    assert sorted(window.hits for window in windows) == [1, 1]


def test_expired_fixed_window_resets_to_one_hit(session_factory):
    now = _at_noon()
    spec = LimitSpec(
        "reset",
        principal_requests=1,
        global_requests=1,
        window_seconds=60,
    )
    limiter = DurableRateLimiter(session_factory)
    assert limiter.consume_pair(spec, principal="operator", now=now).allowed

    reset = limiter.consume_pair(
        spec,
        principal="operator",
        now=now + timedelta(seconds=60),
    )

    assert reset.allowed is True
    assert reset.remaining == 0
    assert reset.reset_at == now + timedelta(seconds=120)
    with session_factory() as session:
        windows = session.scalars(
            select(RateWindow).where(RateWindow.policy_name == "reset")
        ).all()
    assert len(windows) == 2
    assert {window.hits for window in windows} == {1}


def test_principal_and_global_daily_windows_survive_restart(session_factory):
    now = _at_noon()
    next_midnight = datetime(2026, 7, 28, tzinfo=UTC)
    spec = LimitSpec(
        "daily",
        principal_requests=10,
        global_requests=10,
        window_seconds=60,
        principal_daily_requests=1,
        global_daily_requests=2,
    )
    first = DurableRateLimiter(session_factory)
    assert first.consume_pair(spec, principal="first", now=now).allowed

    restarted = DurableRateLimiter(session_factory)
    principal_denied = restarted.consume_pair(
        spec,
        principal="first",
        now=now,
    )
    assert principal_denied.allowed is False
    assert principal_denied.reset_at == next_midnight
    assert principal_denied.retry_after_seconds == 12 * 60 * 60

    assert restarted.consume_pair(
        spec,
        principal="second",
        now=now,
    ).allowed
    global_denied = DurableRateLimiter(session_factory).consume_pair(
        spec,
        principal="third",
        now=now,
    )
    assert global_denied.allowed is False
    assert global_denied.reset_at == next_midnight

    next_day = DurableRateLimiter(session_factory).consume_pair(
        spec,
        principal="first",
        now=next_midnight,
    )
    assert next_day.allowed is True


def test_successful_tied_bucket_uses_later_utc_day_reset(session_factory):
    local_offset = timezone(timedelta(hours=5, minutes=30))
    local_now = datetime(
        2026,
        7,
        28,
        1,
        30,
        tzinfo=local_offset,
    )
    spec = LimitSpec(
        "tie-reset",
        principal_requests=1,
        global_requests=10,
        window_seconds=60,
        principal_daily_requests=1,
    )

    decision = DurableRateLimiter(session_factory).consume_pair(
        spec,
        principal="operator",
        now=local_now,
    )

    assert decision.allowed is True
    assert decision.remaining == 0
    assert decision.reset_at == datetime(2026, 7, 28, tzinfo=UTC)


def test_fractional_retry_rounds_up_without_truncating(session_factory):
    started_at = datetime(
        2026,
        7,
        27,
        12,
        0,
        0,
        100_000,
        tzinfo=UTC,
    )
    spec = LimitSpec(
        "fractional-retry",
        principal_requests=1,
        global_requests=1,
        window_seconds=2,
    )
    limiter = DurableRateLimiter(session_factory)
    assert limiter.consume_pair(
        spec,
        principal="operator",
        now=started_at,
    ).allowed

    denied = limiter.consume_pair(
        spec,
        principal="operator",
        now=started_at + timedelta(microseconds=999_999),
    )

    assert denied.allowed is False
    assert denied.retry_after_seconds == 2
    assert denied.reset_at == started_at + timedelta(seconds=2)


def test_bucket_keys_are_sha256_hashes_without_raw_principal(session_factory):
    now = _at_noon()
    principal = "raw-session-token-and-principal"
    spec = LimitSpec(
        "hash-check",
        principal_requests=2,
        global_requests=2,
        window_seconds=60,
    )
    assert DurableRateLimiter(session_factory).consume_pair(
        spec,
        principal=principal,
        now=now,
    ).allowed

    with session_factory() as session:
        keys = set(
            session.scalars(
                select(RateWindow.bucket_key).where(
                    RateWindow.policy_name == "hash-check"
                )
            )
        )

    expected = {
        hashlib.sha256(
            f"hash-check\0principal_window\0{principal}".encode()
        ).hexdigest(),
        hashlib.sha256(b"hash-check\0global_window\0").hexdigest(),
    }
    assert keys == expected
    assert all(len(key) == 64 for key in keys)
    assert all(principal not in key for key in keys)


def test_sqlite_lock_is_store_unavailable_and_never_grants(
    engine,
    session_factory,
):
    _shorten_sqlite_busy_timeout(engine)
    limiter = DurableRateLimiter(session_factory)
    spec = LimitSpec(
        "locked",
        principal_requests=1,
        global_requests=1,
        window_seconds=60,
    )

    with engine.connect() as blocker:
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(LimitStoreUnavailable):
            limiter.consume_pair(spec, principal="operator", now=_at_noon())
        blocker.rollback()

    assert limiter.consume_pair(
        spec,
        principal="operator",
        now=_at_noon(),
    ).allowed


def test_limit_decisions_and_specs_are_immutable():
    spec = LimitSpec("immutable", 1, 1, 60)
    decision = LimitDecision(True, 0, 0, _at_noon())

    with pytest.raises(FrozenInstanceError):
        spec.window_seconds = 30
    with pytest.raises(FrozenInstanceError):
        decision.allowed = False


def test_same_lease_owner_renews_and_restart_observes_lease(session_factory):
    now = _at_noon()
    service = ConcurrencyLeaseService(session_factory)
    first = service.acquire(
        "backtest:global",
        owner="worker-a",
        ttl_seconds=30,
        now=now,
    )
    renewed = service.acquire(
        "backtest:global",
        owner="worker-a",
        ttl_seconds=90,
        now=now + timedelta(seconds=10),
    )

    assert first.acquired is True
    assert first.expires_at == now + timedelta(seconds=30)
    assert renewed.acquired is True
    assert renewed.expires_at == now + timedelta(seconds=100)
    assert renewed.generation == first.generation + 1

    observed = ConcurrencyLeaseService(session_factory).inspect(
        "backtest:global",
        now=now + timedelta(seconds=20),
    )
    assert observed.acquired is True
    assert observed.owner == "worker-a"
    assert observed.expires_at == renewed.expires_at
    assert observed.generation == renewed.generation


def test_fenced_lease_renewal_extends_only_the_observed_generation(
    session_factory,
):
    now = _at_noon()
    service = ConcurrencyLeaseService(session_factory)
    first = service.acquire(
        "route:long-handler",
        owner="request-a",
        ttl_seconds=30,
        now=now,
    )

    renewed = service.renew(
        "route:long-handler",
        owner=first.owner,
        generation=first.generation,
        ttl_seconds=30,
        now=now + timedelta(seconds=20),
    )

    assert renewed.acquired is True
    assert renewed.owner == "request-a"
    assert renewed.generation == first.generation
    assert renewed.expires_at == now + timedelta(seconds=50)
    assert service.acquire(
        "route:long-handler",
        owner="request-b",
        ttl_seconds=30,
        now=now + timedelta(seconds=31),
    ).acquired is False


def test_stale_generation_cannot_renew_or_release_replacement_lease(
    session_factory,
):
    now = _at_noon()
    service = ConcurrencyLeaseService(session_factory)
    first = service.acquire(
        "route:fenced",
        owner="request-a",
        ttl_seconds=10,
        now=now,
    )
    replacement = service.acquire(
        "route:fenced",
        owner="request-b",
        ttl_seconds=30,
        now=now + timedelta(seconds=10),
    )

    stale_renewal = service.renew(
        "route:fenced",
        owner=first.owner,
        generation=first.generation,
        ttl_seconds=30,
        now=now + timedelta(seconds=11),
    )

    assert stale_renewal.acquired is False
    assert stale_renewal.owner == replacement.owner
    assert stale_renewal.generation == replacement.generation
    assert service.release(
        "route:fenced",
        owner=first.owner,
        generation=first.generation,
    ) is False
    observed = service.inspect(
        "route:fenced",
        now=now + timedelta(seconds=11),
    )
    assert observed.acquired is True
    assert observed.owner == replacement.owner
    assert observed.generation == replacement.generation


@pytest.mark.parametrize("operation", ["renew", "release"])
def test_lease_clock_is_observed_after_blocked_transaction_begins(
    engine,
    session_factory,
    operation,
):
    service = ConcurrencyLeaseService(session_factory)
    acquired = service.acquire(
        f"route:blocked-{operation}",
        owner="request-a",
        ttl_seconds=1,
    )
    begin_attempted = threading.Event()

    with engine.connect() as blocker:
        blocker.exec_driver_sql("BEGIN IMMEDIATE")

        def observe_begin(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            if statement == "BEGIN IMMEDIATE":
                begin_attempted.set()

        event.listen(engine, "before_cursor_execute", observe_begin)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                if operation == "renew":
                    future = pool.submit(
                        service.renew,
                        f"route:blocked-{operation}",
                        owner=acquired.owner,
                        generation=acquired.generation,
                        ttl_seconds=30,
                    )
                else:
                    future = pool.submit(
                        service.release,
                        f"route:blocked-{operation}",
                        owner=acquired.owner,
                        generation=acquired.generation,
                    )
                assert begin_attempted.wait(timeout=2)
                time.sleep(1.1)
                blocker.rollback()
                stale_result = future.result(timeout=2)
        finally:
            event.remove(engine, "before_cursor_execute", observe_begin)

    if operation == "renew":
        assert stale_result.acquired is False
    else:
        assert stale_result is False

    replacement = service.acquire(
        f"route:blocked-{operation}",
        owner="request-b",
        ttl_seconds=30,
    )
    assert replacement.acquired is True
    assert service.release(
        f"route:blocked-{operation}",
        owner=acquired.owner,
        generation=acquired.generation,
    ) is False
    observed = service.inspect(f"route:blocked-{operation}")
    assert observed.acquired is True
    assert observed.owner == replacement.owner
    assert observed.generation == replacement.generation


def test_nonexpiring_interlock_requires_exact_settlement_or_reconciliation(
    session_factory,
):
    service_type = getattr(
        limits_module,
        "MutationInterlockService",
        None,
    )
    assert service_type is not None
    service = service_type(session_factory)

    claimed = service.claim(
        "route:" + "a" * 64 + ":0",
        owner="internal-owner-a",
        generation=7,
        operation="order_approve",
    )
    denied = service.claim(
        "route:" + "a" * 64 + ":0",
        owner="internal-owner-b",
        generation=8,
        operation="order_approve",
    )

    assert claimed.acquired is True
    assert denied.acquired is False
    assert denied.owner == "internal-owner-a"
    assert denied.generation == 7
    assert service.mark_uncertain(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        outcome_code="lease_renewal_unproven",
        worker_finished=False,
    )
    assert service.reconcile_clear(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        actor="operator:test",
        request_id="reconcile-too-early",
        evidence_code="broker_truth_reconciled",
        worker_termination_proven=False,
    ) is False
    assert service.mark_uncertain(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        outcome_code="request_cancelled",
        worker_finished=True,
    )
    assert service.reconcile_clear(
        claimed.resource_key,
        owner="stale-owner",
        generation=claimed.generation,
        actor="operator:test",
        request_id="reconcile-stale-owner",
        evidence_code="broker_truth_reconciled",
        worker_termination_proven=True,
    ) is False
    assert service.reconcile_clear(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        actor="operator:test",
        request_id="reconcile-exact-owner",
        evidence_code="broker_truth_reconciled",
        worker_termination_proven=False,
    ) is True
    assert service.inspect(claimed.resource_key) is None
    with session_factory() as session:
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.request_id == "reconcile-exact-owner"
            )
        )
        assert audit is not None
        assert audit.action == "mutation_interlock.reconcile"
        assert audit.result_code == "cleared"


def test_settled_interlock_clears_only_with_exact_unexpired_release(
    session_factory,
):
    service_type = getattr(
        limits_module,
        "MutationInterlockService",
        None,
    )
    assert service_type is not None
    interlocks = service_type(session_factory)
    leases = ConcurrencyLeaseService(session_factory)
    now = _at_noon()
    resource_key = "route:" + "c" * 64 + ":0"
    lease = leases.acquire(
        resource_key,
        owner="internal-settled-owner",
        ttl_seconds=30,
        now=now,
    )
    claimed = interlocks.claim(
        resource_key,
        owner=lease.owner,
        generation=lease.generation,
        operation="order_approve",
    )
    assert interlocks.settle(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        outcome_code="handler_completed",
    )

    assert interlocks.release_settled(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        now=now + timedelta(seconds=30),
    ) is False
    assert interlocks.inspect(claimed.resource_key) is not None
    replacement = leases.acquire(
        claimed.resource_key,
        owner="replacement-owner",
        ttl_seconds=30,
        now=now + timedelta(seconds=30),
    )
    assert replacement.acquired is True
    assert interlocks.release_settled(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        now=now + timedelta(seconds=31),
    ) is False
    observed = leases.inspect(
        claimed.resource_key,
        now=now + timedelta(seconds=31),
    )
    assert observed.owner == replacement.owner
    assert interlocks.inspect(claimed.resource_key) is not None

    assert interlocks.reconcile_clear(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        actor="operator:test",
        request_id="reconcile-settled-release",
        evidence_code="broker_truth_reconciled",
        worker_termination_proven=False,
    ) is True


def test_exact_unexpired_release_and_settled_clear_are_atomic(
    session_factory,
):
    service_type = getattr(
        limits_module,
        "MutationInterlockService",
        None,
    )
    assert service_type is not None
    interlocks = service_type(session_factory)
    leases = ConcurrencyLeaseService(session_factory)
    now = _at_noon()
    resource_key = "route:" + "d" * 64 + ":0"
    lease = leases.acquire(
        resource_key,
        owner="internal-clear-owner",
        ttl_seconds=30,
        now=now,
    )
    claimed = interlocks.claim(
        resource_key,
        owner=lease.owner,
        generation=lease.generation,
        operation="plan_cancel",
    )
    assert interlocks.settle(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        outcome_code="handler_completed",
    )

    assert interlocks.release_settled(
        claimed.resource_key,
        owner=claimed.owner,
        generation=claimed.generation,
        now=now + timedelta(seconds=1),
    ) is True

    assert interlocks.inspect(claimed.resource_key) is None
    observed = leases.inspect(
        claimed.resource_key,
        now=now + timedelta(seconds=1),
    )
    assert observed.acquired is False
    assert observed.generation == lease.generation + 1


def test_second_lease_owner_is_denied_until_expiry(session_factory):
    now = _at_noon()
    service = ConcurrencyLeaseService(session_factory)
    assert service.acquire(
        "analysis:global",
        owner="worker-a",
        ttl_seconds=30,
        now=now,
    ).acquired

    denied = service.acquire(
        "analysis:global",
        owner="worker-b",
        ttl_seconds=30,
        now=now + timedelta(seconds=29),
    )
    assert denied.acquired is False
    assert denied.owner == "worker-a"
    assert denied.retry_after_seconds == 1

    acquired = service.acquire(
        "analysis:global",
        owner="worker-b",
        ttl_seconds=30,
        now=now + timedelta(seconds=30),
    )
    assert acquired.acquired is True
    assert acquired.owner == "worker-b"
    assert acquired.expires_at == now + timedelta(seconds=60)


def test_lease_release_is_owner_guarded(session_factory):
    now = _at_noon()
    service = ConcurrencyLeaseService(session_factory)
    acquired = service.acquire(
        "panic:account",
        owner="owner-a",
        ttl_seconds=60,
        now=now,
    )
    assert acquired.acquired

    assert service.release(
        "panic:account",
        owner="owner-b",
        generation=acquired.generation,
        now=now,
    ) is False
    assert service.acquire(
        "panic:account",
        owner="owner-b",
        ttl_seconds=60,
        now=now,
    ).acquired is False

    assert service.release(
        "panic:account",
        owner="owner-a",
        generation=acquired.generation,
        now=now,
    ) is True
    assert service.acquire(
        "panic:account",
        owner="owner-b",
        ttl_seconds=60,
        now=now,
    ).acquired is True


def test_lease_lock_failure_is_store_unavailable(
    engine,
    session_factory,
):
    _shorten_sqlite_busy_timeout(engine)
    service = ConcurrencyLeaseService(session_factory)

    with engine.connect() as blocker:
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(LimitStoreUnavailable):
            service.acquire(
                "locked:lease",
                owner="worker",
                ttl_seconds=60,
                now=_at_noon(),
            )
        blocker.rollback()

    assert service.acquire(
        "locked:lease",
        owner="worker",
        ttl_seconds=60,
        now=_at_noon(),
    ).acquired


def _invoke_public_db_method(method_name: str, session_factory) -> None:
    now = _at_noon()
    if method_name == "consume_pair":
        DurableRateLimiter(session_factory).consume_pair(
            LimitSpec("lifecycle", 2, 2, 60),
            principal="operator",
            now=now,
        )
    elif method_name == "rate_prune":
        DurableRateLimiter(session_factory).prune_expired(now)
    elif method_name == "acquire":
        ConcurrencyLeaseService(session_factory).acquire(
            "lifecycle:lease",
            owner="worker",
            ttl_seconds=60,
            now=now,
        )
    elif method_name == "renew":
        ConcurrencyLeaseService(session_factory).renew(
            "lifecycle:lease",
            owner="worker",
            generation=1,
            ttl_seconds=60,
            now=now,
        )
    elif method_name == "release":
        ConcurrencyLeaseService(session_factory).release(
            "lifecycle:lease",
            owner="worker",
            generation=1,
        )
    elif method_name == "inspect":
        ConcurrencyLeaseService(session_factory).inspect(
            "lifecycle:lease",
            now=now,
        )
    elif method_name == "lease_prune":
        ConcurrencyLeaseService(session_factory).prune_expired(now)
    else:
        raise AssertionError(f"unknown method {method_name}")


@pytest.mark.parametrize(
    "method_name",
    [
        "consume_pair",
        "rate_prune",
        "acquire",
        "renew",
        "release",
        "inspect",
        "lease_prune",
    ],
)
@pytest.mark.parametrize(
    ("phase", "error_type"),
    [
        ("factory", SQLAlchemyError),
        ("enter", OSError),
        ("body", OSError),
        ("exit", SQLAlchemyError),
    ],
)
def test_public_db_methods_normalize_entire_session_lifecycle(
    method_name,
    phase,
    error_type,
    session_factory,
):
    failing_factory = _LifecycleFailureFactory(
        session_factory,
        phase,
        error_type(f"{phase} failed"),
    )

    with pytest.raises(LimitStoreUnavailable):
        _invoke_public_db_method(method_name, failing_factory)


def test_programmer_error_from_session_factory_is_not_normalized():
    class ProgrammerErrorFactory:
        def __call__(self):
            raise TypeError("programmer error")

    with pytest.raises(TypeError, match="programmer error"):
        ConcurrencyLeaseService(ProgrammerErrorFactory()).inspect(
            "programmer-error",
            now=_at_noon(),
        )


def test_rate_window_pruning_is_bounded_and_preserves_live_rows(
    session_factory,
):
    now = _at_noon()
    with session_factory() as session:
        session.add_all(
            [
                RateWindow(
                    bucket_key=character * 64,
                    policy_name="prune",
                    window_started_at=now - timedelta(minutes=2),
                    expires_at=expires_at,
                    hits=1,
                )
                for character, expires_at in (
                    ("a", now - timedelta(seconds=2)),
                    ("b", now - timedelta(seconds=1)),
                    ("c", now),
                    ("d", now + timedelta(seconds=1)),
                )
            ]
        )
        session.commit()

    service = DurableRateLimiter(session_factory)
    assert service.prune_expired(now, limit=2) == 2
    with session_factory() as session:
        remaining = set(
            session.scalars(
                select(RateWindow.bucket_key).where(
                    RateWindow.policy_name == "prune"
                )
            )
        )
    assert "d" * 64 in remaining
    assert len(remaining) == 2

    assert service.prune_expired(now, limit=2) == 1
    with session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(RateWindow)
            .where(RateWindow.policy_name == "prune")
        ) == 1
        assert session.get(RateWindow, "d" * 64) is not None


def test_lease_pruning_is_bounded_and_preserves_live_rows(session_factory):
    now = _at_noon()
    with session_factory() as session:
        session.add_all(
            [
                ConcurrencyLease(
                    resource_key=f"prune:{index}",
                    owner="worker",
                    expires_at=expires_at,
                )
                for index, expires_at in enumerate(
                    (
                        now - timedelta(seconds=2),
                        now - timedelta(seconds=1),
                        now,
                        now + timedelta(seconds=1),
                    )
                )
            ]
        )
        session.commit()

    service = ConcurrencyLeaseService(session_factory)
    assert service.prune_expired(now, limit=2) == 2
    with session_factory() as session:
        remaining = set(
            session.scalars(select(ConcurrencyLease.resource_key))
        )
    assert "prune:3" in remaining
    assert len(remaining) == 2

    assert service.prune_expired(now, limit=2) == 1
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ConcurrencyLease)
        ) == 1
        assert session.get(ConcurrencyLease, "prune:3") is not None


def test_lease_decisions_are_immutable():
    decision = LeaseDecision(
        acquired=True,
        owner="worker",
        expires_at=_at_noon(),
        generation=1,
        retry_after_seconds=0,
    )

    with pytest.raises(FrozenInstanceError):
        decision.acquired = False
