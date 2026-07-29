from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_assistant.assets import AssetClass
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderType,
)
from trading_assistant.risk import killswitch as killswitch_module
from trading_assistant.risk import rules
from trading_assistant.risk.breakers import (
    BreakerScope,
    _now,
    reset_in_session,
    trip_in_session,
)
from trading_assistant.risk.clock import CryptoClock, FakeClock
from trading_assistant.risk.engine import RiskEngine, RiskResult
from trading_assistant.risk.killswitch import KillSwitch, _scope
from trading_assistant.risk.pnl import (
    most_recent_regular_open,
    most_recent_utc_midnight,
    realized_pnl,
)
from trading_assistant.risk.staleness import is_stale


def _order(*, order_type: OrderType = OrderType.MARKET) -> OrderRequest:
    return OrderRequest(
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=order_type,
        idempotency_key=f"risk-edge-{order_type.value}",
        notional=Decimal("100"),
        limit_price=(
            Decimal("100") if order_type is OrderType.LIMIT else None
        ),
    )


def test_naive_quote_and_observation_times_are_normalized_to_utc():
    observed_at = datetime(2026, 7, 29, 12)

    assert is_stale(observed_at, now=observed_at) is False


def test_clock_boundaries_cover_default_and_naive_paths():
    crypto = CryptoClock()
    before = datetime.now(timezone.utc)
    default_open = crypto.next_open()
    after = datetime.now(timezone.utc)

    assert before <= default_open <= after
    assert crypto.next_close().year == 9999
    assert FakeClock().next_open().tzinfo is timezone.utc
    assert FakeClock().next_close().tzinfo is timezone.utc
    assert FakeClock().most_recent_open(datetime(2026, 7, 29, 12)) == (
        datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    )


def test_naive_pnl_boundaries_and_filter_are_normalized_to_utc():
    naive = datetime(2026, 7, 29, 12)

    assert most_recent_regular_open(naive).tzinfo is timezone.utc
    assert most_recent_utc_midnight(naive) == datetime(
        2026,
        7,
        29,
        tzinfo=timezone.utc,
    )
    assert realized_pnl([], since=naive) == Decimal(0)


def test_incomplete_effective_exposure_fails_position_rules_closed(
    risk_config,
    make_snapshot,
):
    snapshot = make_snapshot(
        prices={"AAPL": Decimal("100")},
        pending_signed_notional={"AAPL": Decimal("NaN")},
    )
    order = _order()

    assert rules.check_max_position(
        order,
        snapshot,
        risk_config,
    ) == "position exposure snapshot is incomplete"
    assert rules.check_portfolio_exposure(
        order,
        snapshot,
        risk_config,
    ) == "portfolio exposure snapshot is incomplete"
    assert (
        rules.check_cross_broker_concentration(
            order,
            snapshot,
            risk_config,
        )
        is None
    )


def test_limit_price_sanity_fails_closed_without_a_usable_reference(
    risk_config,
    make_snapshot,
):
    order = _order(order_type=OrderType.LIMIT)

    assert "no quote available" in rules.check_price_sanity(
        order,
        make_snapshot(),
        risk_config,
    )
    assert "is zero" in rules.check_price_sanity(
        order,
        make_snapshot(prices={"AAPL": Decimal(0)}),
        risk_config,
    )


def test_disabled_cross_broker_warning_path_stays_non_blocking(
    risk_config,
    make_snapshot,
):
    config = risk_config.model_copy(
        update={"warn_on_cross_broker_concentration": False}
    )

    result = RiskEngine(config).check(
        _order(),
        make_snapshot(prices={"AAPL": Decimal("100")}),
    )

    assert result.approved is True
    assert result.warnings == []
    assert RiskResult(True, warnings=["review"]).warning_text() == "review"


@pytest.mark.parametrize(
    ("reason", "actor", "request_id", "audit_reason"),
    [
        ("", "daemon", "request", None),
        ("reason", "", "request", None),
        ("reason", "daemon", "", None),
        ("reason", "daemon", "request", " "),
    ],
)
def test_breaker_trip_rejects_incomplete_audit_identity(
    reason,
    actor,
    request_id,
    audit_reason,
):
    with pytest.raises(ValueError):
        trip_in_session(
            None,
            BreakerScope.operator_global(),
            reason,
            actor,
            request_id=request_id,
            audit_reason=audit_reason,
        )


@pytest.mark.parametrize(
    (
        "actor",
        "reason",
        "prior_health",
        "generation",
        "request_id",
    ),
    [
        ("", "healthy", {"ok": True}, 1, "request"),
        ("operator", "", {"ok": True}, 1, "request"),
        ("operator", "healthy", {}, 1, "request"),
        ("operator", "healthy", {"ok": True}, 1, ""),
        ("operator", "healthy", {"ok": True}, True, "request"),
        ("operator", "healthy", {"ok": True}, 0, "request"),
    ],
)
def test_breaker_reset_rejects_unproven_operator_inputs(
    actor,
    reason,
    prior_health,
    generation,
    request_id,
):
    with pytest.raises(ValueError):
        reset_in_session(
            None,
            BreakerScope.operator_global(),
            actor,
            reason,
            prior_health,
            expected_generation=generation,
            request_id=request_id,
        )


def test_breaker_time_and_legacy_scope_normalize_compatibility_inputs():
    naive = datetime(2026, 7, 29, 12)

    assert _now(naive) == naive.replace(tzinfo=timezone.utc)
    assert _scope("equity") == BreakerScope.loss(AssetClass.EQUITY)


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def in_transaction(self) -> bool:
        return False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _FakeBarrier:
    @contextmanager
    def hold_writer(self):
        yield


def test_legacy_trip_skips_duplicate_event_but_commits(
    monkeypatch,
):
    session = _FakeSession()
    persisted = []
    monkeypatch.setattr(
        killswitch_module,
        "SubmissionBarrier",
        lambda _session: _FakeBarrier(),
    )
    monkeypatch.setattr(
        killswitch_module,
        "trip_in_session",
        lambda *_args, **_kwargs: (object(), False),
    )
    monkeypatch.setattr(
        killswitch_module,
        "persist_sensitive",
        lambda *_args, **_kwargs: persisted.append(True),
    )

    KillSwitch.trip(
        session,
        "already tripped",
        actor="daemon",
        request_id="legacy-duplicate",
    )

    assert session.commits == 1
    assert session.rollbacks == 0
    assert persisted == []


def test_legacy_trip_rolls_back_when_durable_trip_fails(
    monkeypatch,
):
    session = _FakeSession()
    monkeypatch.setattr(
        killswitch_module,
        "SubmissionBarrier",
        lambda _session: _FakeBarrier(),
    )

    def fail_trip(*_args, **_kwargs):
        raise RuntimeError("synthetic durable failure")

    monkeypatch.setattr(
        killswitch_module,
        "trip_in_session",
        fail_trip,
    )

    with pytest.raises(RuntimeError, match="synthetic durable failure"):
        KillSwitch.trip(
            session,
            "must roll back",
            actor="daemon",
            request_id="legacy-failure",
        )

    assert session.commits == 0
    assert session.rollbacks == 1
