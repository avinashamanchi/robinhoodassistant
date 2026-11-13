"""Stop-loss and re-entry cooldown: one policy, applied by the backtester and the autopilot."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pandas as pd
import pytest

from tests.test_autopilot import (
    FLAT_AAPL,
    LONG_AAPL,
    _autopilot,
    _features,
    _holding,
    _reasons,
    _record_fill,
    _trip_breaker,
)
from trading_assistant.backtest.data import DataSource
from trading_assistant.backtest.engine import run_backtest
from trading_assistant.config import BacktestConfig
from trading_assistant.strategies.base import Signal, SignalAction, Strategy
from trading_assistant.strategies.exit_policy import ExitPolicy


# ── the policy itself ─────────────────────────────────────────────────────────
def test_stop_triggers_at_or_below_the_stop_price_only():
    policy = ExitPolicy(stop_loss_pct=10)
    assert policy.stop_price(100.0) == pytest.approx(90.0)
    assert policy.stop_triggered(100.0, 90.0)
    assert policy.stop_triggered(100.0, 80.0)
    assert not policy.stop_triggered(100.0, 90.01)
    assert not policy.stop_triggered(None, 50.0)          # nothing held
    assert not ExitPolicy().stop_triggered(100.0, 1.0)    # no stop configured


def test_cooldown_counts_calendar_days_from_the_exit_session():
    policy = ExitPolicy(reentry_cooldown_days=5)
    exit_day = date(2026, 10, 1)
    assert policy.cooling_down(exit_day, date(2026, 10, 5))
    assert not policy.cooling_down(exit_day, date(2026, 10, 6))
    assert not policy.cooling_down(None, date(2026, 10, 2))
    assert not ExitPolicy().cooling_down(exit_day, exit_day)


@pytest.mark.parametrize("bad", [0, 100, -5])
def test_impossible_stop_percentages_are_refused(bad):
    with pytest.raises(ValueError):
        ExitPolicy(stop_loss_pct=bad)


# ── the backtester applies it ─────────────────────────────────────────────────
class _AlwaysLong(Strategy):
    name = "always_long"

    def on_bar(self, features):
        return Signal(SignalAction.BUY, reason="test")


def _path(closes):
    index = pd.date_range("2024-01-01", periods=len(closes), freq="D", tz="UTC", name="ts")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1e6] * len(closes)},
        index=index,
    )


# 10 days at 100, a fall to 85 for 5 days, then a recovery to 100.
CLOSES = [100.0] * 10 + [85.0] * 5 + [100.0] * 20


def _sides(result):
    return [(fill.side, fill.ts.date()) for fill in result.fills]


def test_without_a_policy_an_always_long_rule_rides_the_drawdown():
    result = run_backtest(_AlwaysLong(), DataSource({"AAPL": _path(CLOSES)}), "AAPL",
                          backtest_config=BacktestConfig())
    assert [side for side, _ in _sides(result)] == ["buy"]


def test_the_stop_exits_and_the_cooldown_delays_re_entry():
    policy = ExitPolicy(stop_loss_pct=10, reentry_cooldown_days=7)
    result = run_backtest(_AlwaysLong(), DataSource({"AAPL": _path(CLOSES)}), "AAPL",
                          backtest_config=BacktestConfig(), exit_policy=policy)

    sides = _sides(result)
    assert [side for side, _ in sides] == ["buy", "sell", "buy"]
    sold, rebought = sides[1][1], sides[2][1]
    assert sold == date(2024, 1, 12)                 # decided on the first 85 close, filled next open
    assert (rebought - sold).days >= 7               # not before the cooldown ends


def test_without_a_cooldown_the_rule_re_enters_the_next_session():
    policy = ExitPolicy(stop_loss_pct=10)
    result = run_backtest(_AlwaysLong(), DataSource({"AAPL": _path(CLOSES)}), "AAPL",
                          backtest_config=BacktestConfig(), exit_policy=policy)
    sides = _sides(result)
    assert [side for side, _ in sides][:3] == ["buy", "sell", "buy"]
    assert (sides[2][1] - sides[1][1]).days <= 2


# ── the autopilot applies it ──────────────────────────────────────────────────
def test_the_autopilot_stops_out_its_own_position_even_while_the_trend_holds(make_service):
    service = _holding(make_service)               # broker holds 5 AAPL
    _record_fill(service, "AAPL", "buy", "5")      # bought by the autopilot at $100
    falling = {"AAPL": _features("AAPL", 105, 100, sma200=80, last_close=88)}
    ap = _autopilot(service, falling, exit_policy=ExitPolicy(stop_loss_pct=10))

    results = ap.run_once()

    assert [r["side"] for r in results] == ["sell"]
    assert _reasons(ap) == ["stop_loss"]


def test_the_stop_never_sells_a_position_the_autopilot_did_not_buy(make_service):
    service = _holding(make_service)
    _record_fill(service, "AAPL", "buy", "5", actor="operator:local")
    falling = {"AAPL": _features("AAPL", 105, 100, sma200=80, last_close=50)}
    ap = _autopilot(service, falling, exit_policy=ExitPolicy(stop_loss_pct=10))

    assert ap.run_once() == []
    assert _reasons(ap) == ["position_managed_elsewhere"]


def test_above_the_stop_the_autopilot_keeps_its_position(make_service):
    service = _holding(make_service)
    _record_fill(service, "AAPL", "buy", "5")
    steady = {"AAPL": _features("AAPL", 105, 100, sma200=80, last_close=95)}
    ap = _autopilot(service, steady, exit_policy=ExitPolicy(stop_loss_pct=10))

    assert ap.run_once() == []
    assert _reasons(ap) == ["already_long"]


def test_the_autopilot_waits_out_the_cooldown_before_buying_again(make_service):
    service = make_service()                       # flat at the broker
    _record_fill(service, "AAPL", "buy", "5")
    _record_fill(service, "AAPL", "sell", "5")     # exited two days ago
    ap = _autopilot(service, LONG_AAPL, exit_policy=ExitPolicy(reentry_cooldown_days=5))

    assert ap.run_once() == []
    assert service.broker.submit_calls == 0
    assert _reasons(ap) == ["reentry_cooldown"]

    later = _autopilot(service, LONG_AAPL, exit_policy=ExitPolicy(reentry_cooldown_days=5),
                       now=lambda: datetime.now(timezone.utc) + timedelta(days=4))
    later.run_once(session=(datetime.now(timezone.utc) + timedelta(days=4)).date())
    assert _reasons(later) == ["enter_long"]


def _rejected_autopilot_order(service, symbol="AAPL", side="buy", *, age=timedelta(hours=20)):
    from trading_assistant.db.models import Order
    from trading_assistant.security.sensitive_fields import persist_sensitive

    with service.session_factory() as s:
        order = Order(idempotency_key="ap-" + uuid4().hex[:40], ticker=symbol, side=side,
                      order_type="market", notional=Decimal("100"), status="rejected",
                      created_at=datetime.now(timezone.utc) - age)
        persist_sensitive(s, order, {"approval_reason": "fixture: refused by risk"})
        s.commit()


def test_a_refused_entry_is_not_proposed_again_during_the_back_off(make_service):
    service = make_service()
    _rejected_autopilot_order(service)
    ap = _autopilot(service, LONG_AAPL, retry_after_rejection_days=3)

    assert ap.run_once() == []
    assert service.broker.submit_calls == 0
    assert _reasons(ap) == ["recent_rejection_backoff"]


def test_an_old_refusal_no_longer_blocks(make_service):
    service = make_service()
    _rejected_autopilot_order(service, age=timedelta(days=4))
    ap = _autopilot(service, LONG_AAPL, retry_after_rejection_days=3)

    assert [r["side"] for r in ap.run_once()] == ["buy"]


def test_nothing_is_proposed_while_a_blocking_latch_is_tripped(make_service):
    from trading_assistant.db.models import Order
    from trading_assistant.risk.breakers import BreakerScope

    service = make_service()
    _trip_breaker(service, BreakerScope.broker_drift())
    ap = _autopilot(service, LONG_AAPL)

    assert ap.run_once() == []
    assert _reasons(ap) == ["safety_latch"]
    with service.session_factory() as s:
        assert s.query(Order).count() == 0         # no rejected-order trail


def test_a_flat_signal_still_exits_normally_with_a_policy(make_service):
    service = _holding(make_service)
    _record_fill(service, "AAPL", "buy", "5")
    ap = _autopilot(service, FLAT_AAPL, exit_policy=ExitPolicy(stop_loss_pct=10, reentry_cooldown_days=5))

    assert [r["side"] for r in ap.run_once()] == ["sell"]
    assert _reasons(ap) == ["exit_long"]


# ── evidence binds to the policy ──────────────────────────────────────────────
def test_changing_the_policy_changes_the_configuration_fingerprint(app_config):
    from trading_assistant.autopilot.identity import config_fingerprint, decision_code_fingerprint

    base = config_fingerprint(app_config)
    stopped = app_config.model_copy(
        update={"autopilot": app_config.autopilot.model_copy(update={"stop_loss_pct": 8.0})}
    )
    assert config_fingerprint(stopped) != base
    from trading_assistant.autopilot.identity import _decision_sources

    assert any(path.name == "exit_policy.py" for path in _decision_sources("sma_trend"))
    assert decision_code_fingerprint("sma_trend")
