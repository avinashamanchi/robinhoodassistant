from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select

from trading_assistant.app.limits import (
    ConcurrencyLeaseService,
    DurableRateLimiter,
    LeaseDecision,
    LimitDecision,
    LimitSpec,
    LimitStoreUnavailable,
)
from trading_assistant.db.models import ConcurrencyLease, RateWindow


UTC = timezone.utc


def _at_noon() -> datetime:
    return datetime(2026, 7, 27, 12, tzinfo=UTC)


def _shorten_sqlite_busy_timeout(engine) -> None:
    def set_short_timeout(dbapi_connection, _connection_record, _proxy) -> None:
        dbapi_connection.execute("PRAGMA busy_timeout=1")

    event.listen(engine, "checkout", set_short_timeout)


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


def test_parallel_principals_cannot_exceed_global_window(session_factory):
    limiter_a = DurableRateLimiter(session_factory)
    limiter_b = DurableRateLimiter(session_factory)
    spec = LimitSpec(
        "broker_read",
        principal_requests=2,
        global_requests=1,
        window_seconds=60,
    )
    barrier = threading.Barrier(2)

    def consume(args):
        limiter, principal = args
        barrier.wait()
        return limiter.consume_pair(spec, principal=principal).allowed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                consume,
                ((limiter_a, "operator-a"), (limiter_b, "operator-b")),
            )
        )

    assert sorted(results) == [False, True]


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
    assert service.acquire(
        "panic:account",
        owner="owner-a",
        ttl_seconds=60,
        now=now,
    ).acquired

    assert service.release("panic:account", owner="owner-b") is False
    assert service.acquire(
        "panic:account",
        owner="owner-b",
        ttl_seconds=60,
        now=now,
    ).acquired is False

    assert service.release("panic:account", owner="owner-a") is True
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
