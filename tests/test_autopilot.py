"""Autopilot: deterministic decisions + paper-only autonomous execution.

These prove the opt-in autopilot both decides (deterministic strategy shared with
the backtester) and executes (through the real propose -> approve -> risk-engine
path) without a human, while refusing to run outside paper mode, honouring the
daily cap, acting only on fresh features, never double-submitting while an order
is in flight, and never trading a position another workflow owns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_assistant.assets import AssetClass
from trading_assistant.autopilot import (
    DEGRADED_REASONS,
    STRATEGIES,
    Autopilot,
    AutopilotDisabled,
    require_paper,
    strategy_decision,
)
from trading_assistant.autopilot.cli import build_container_with_retry
from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import Position
from trading_assistant.config import AutopilotConfig, TradingMode
from trading_assistant.orders.startup import StartupReconciliationFailed
from trading_assistant.signals.models import MarketFeatures
from trading_assistant.strategies.sma_trend import SmaTrend


def _features(symbol, sma20, sma50, *, sma200=None, last_close=None, as_of=None):
    return MarketFeatures(
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        as_of=as_of or datetime.now(timezone.utc) - timedelta(hours=1),
        sma_20=sma20,
        sma_50=sma50,
        sma_200=sma200,
        last_close=last_close,
    )


LONG_AAPL = {"AAPL": _features("AAPL", 105, 100, sma200=90, last_close=100)}
FLAT_AAPL = {"AAPL": _features("AAPL", 95, 100)}


def _provider(mapping):
    def prov(symbol):
        return mapping[symbol.upper()]

    return prov


def _autopilot(service, features, **overrides):
    options = dict(
        universe=["AAPL"],
        notional_per_trade=Decimal("100"),
        max_orders_per_day=8,
    )
    options.update(overrides)
    return Autopilot(service, _provider(features), **options)


def _record_fill(service, symbol, side, qty, *, actor="autopilot:sma_trend"):
    """Put a completed, broker-identified fill on the local ledger."""
    from trading_assistant.db.models import Fill, Order
    from trading_assistant.security.sensitive_fields import persist_sensitive

    approved = datetime.now(timezone.utc) - timedelta(days=2)
    with service.session_factory() as s:
        order = Order(
            idempotency_key=uuid4().hex,
            ticker=symbol,
            side=side,
            order_type="market",
            qty=Decimal(qty),
            status="filled",
            approval_actor=actor,
            approved_at=approved,
        )
        persist_sensitive(s, order, {"approval_reason": "ledger fixture"})
        s.flush()
        s.add(
            Fill(
                order_id=order.id,
                ticker=symbol,
                side=side,
                qty=Decimal(qty),
                price=Decimal("100"),
                broker_fill_id=f"fill-{uuid4().hex}",
                filled_at=approved,
            )
        )
        s.commit()


def _holding(make_service, qty="5"):
    broker = MockBroker(
        positions=[Position("AAPL", Decimal(qty), Decimal("100"), Decimal("100"))]
    )
    broker.set_price("AAPL", Decimal("100"))
    return make_service(broker=broker)


def _reasons(ap):
    return [d.reason for d in ap.last_decisions]


# ── deterministic strategy (shared with the backtester) ───────────────────────
def test_decision_long_when_fast_leads_and_above_trend():
    f = _features("AAPL", 105, 100, sma200=90, last_close=110)
    assert strategy_decision(SmaTrend(), f) == "long"


def test_decision_flat_when_fast_below_slow():
    assert strategy_decision(SmaTrend(), _features("AAPL", 95, 100)) == "flat"


def test_decision_flat_when_below_long_term_trend():
    f = _features("AAPL", 105, 100, sma200=120, last_close=110)
    assert strategy_decision(SmaTrend(), f) == "flat"


def test_decision_holds_on_incomplete_data():
    """Missing averages must never be read as an exit signal."""
    assert strategy_decision(SmaTrend(), _features("AAPL", None, None)) == "hold"


def test_autopilot_strategies_are_the_backtested_classes():
    from trading_assistant.backtest.runner import STRATEGIES as BACKTESTED

    backtested = {factory().name: factory for factory in BACKTESTED}
    for name, factory in STRATEGIES.items():
        assert backtested[name] is factory
    assert AutopilotConfig().strategy == "sma_trend"


def test_unknown_strategy_is_rejected(make_service):
    with pytest.raises(ValueError):
        _autopilot(make_service(), LONG_AAPL, strategy="rsi_reversion")


# ── paper-only guard ──────────────────────────────────────────────────────────
def test_require_paper_rejects_non_paper():
    live = SimpleNamespace(trading=SimpleNamespace(mode=TradingMode.LIVE))
    with pytest.raises(AutopilotDisabled):
        require_paper(live)


def test_require_paper_allows_paper():
    paper = SimpleNamespace(trading=SimpleNamespace(mode=TradingMode.PAPER))
    require_paper(paper)  # does not raise


# ── autonomous execution through the real risk path ───────────────────────────
def test_run_once_buys_on_long_signal(make_service):
    service = make_service()  # SpyBroker, AAPL @ $100, market open, paper mock
    ap = _autopilot(service, LONG_AAPL)
    results = ap.run_once()
    assert len(results) == 1
    assert results[0]["side"] == "buy"
    assert results[0]["executed"] is True
    assert service.broker.submit_calls == 1
    assert _reasons(ap) == ["enter_long"]


def test_run_once_does_not_rebuy_its_own_position(make_service):
    service = _holding(make_service)
    _record_fill(service, "AAPL", "buy", "5")
    ap = _autopilot(service, LONG_AAPL)
    assert ap.run_once() == []
    assert _reasons(ap) == ["already_long"]


def test_run_once_exits_its_own_position_on_flat_signal(make_service):
    service = _holding(make_service)
    _record_fill(service, "AAPL", "buy", "5")
    ap = _autopilot(service, FLAT_AAPL)
    results = ap.run_once()
    assert len(results) == 1
    assert results[0]["side"] == "sell"
    assert _reasons(ap) == ["exit_long"]
    assert ap.last_decisions[0].owned == Decimal("5")


def test_flat_signal_never_sells_a_position_it_did_not_buy(make_service):
    """A plan- or human-owned position is not the autopilot's to liquidate."""
    service = _holding(make_service)
    _record_fill(service, "AAPL", "buy", "5", actor="operator:local")
    ap = _autopilot(service, FLAT_AAPL)
    assert ap.run_once() == []
    assert _reasons(ap) == ["position_managed_elsewhere"]


def test_flat_signal_sells_only_the_autopilot_share(make_service):
    service = _holding(make_service, qty="8")
    _record_fill(service, "AAPL", "buy", "3")
    _record_fill(service, "AAPL", "buy", "5", actor="operator:local")
    ap = _autopilot(service, FLAT_AAPL, dry_run=True)
    results = ap.run_once()
    assert len(results) == 1
    assert results[0]["side"] == "sell"
    assert Decimal(results[0]["qty"]) == Decimal("3")
    assert results[0]["dry_run"] is True


def test_long_signal_does_not_buy_into_another_workflows_position(make_service):
    service = _holding(make_service)
    ap = _autopilot(service, LONG_AAPL)
    assert ap.run_once() == []
    assert _reasons(ap) == ["position_managed_elsewhere"]


def test_hold_signal_never_exits(make_service):
    service = _holding(make_service)
    _record_fill(service, "AAPL", "buy", "5")
    ap = _autopilot(service, {"AAPL": _features("AAPL", None, None)})
    assert ap.run_once() == []
    assert _reasons(ap) == ["signal_hold"]


def test_in_flight_order_blocks_a_second_submission(make_service):
    """A buy the broker has not filled yet must not be repeated next cycle."""
    service = make_service()
    ap = _autopilot(service, LONG_AAPL)
    assert len(ap.run_once()) == 1
    assert ap.run_once() == []
    assert _reasons(ap) == ["order_in_flight"]
    assert service.broker.submit_calls == 1


def test_stale_features_are_skipped(make_service):
    service = make_service()
    stale = {
        "AAPL": _features(
            "AAPL",
            105,
            100,
            sma200=90,
            last_close=100,
            as_of=datetime.now(timezone.utc) - timedelta(days=30),
        )
    }
    ap = _autopilot(service, stale, max_feature_age=timedelta(hours=120))
    assert ap.run_once() == []
    assert _reasons(ap) == ["features_stale"]
    assert "features_stale" in DEGRADED_REASONS
    assert service.broker.submit_calls == 0


def test_order_sync_failure_skips_the_cycle(make_service):
    service = make_service()

    def unavailable(**_kwargs):
        raise RuntimeError("broker down")

    service.sync_open_orders = unavailable
    ap = _autopilot(service, LONG_AAPL)
    assert ap.run_once() == []
    assert _reasons(ap) == ["order_sync_unavailable"]
    assert service.broker.submit_calls == 0


def test_run_once_honours_daily_cap(make_service):
    service = make_service()
    service.broker.set_price("MSFT", Decimal("100"))
    feats = {
        "AAPL": _features("AAPL", 105, 100, sma200=90, last_close=100),
        "MSFT": _features("MSFT", 105, 100, sma200=90, last_close=100),
    }
    ap = _autopilot(service, feats, universe=["AAPL", "MSFT"], max_orders_per_day=1)
    results = ap.run_once()
    assert len(results) == 1
    assert service.broker.submit_calls == 1
    assert _reasons(ap) == ["enter_long", "daily_cap_reached"]


def test_dry_run_decides_but_places_no_orders(make_service):
    service = make_service()
    ap = _autopilot(service, LONG_AAPL, dry_run=True)
    results = ap.run_once()
    assert len(results) == 1
    assert results[0]["dry_run"] is True
    assert results[0]["side"] == "buy"
    assert service.broker.submit_calls == 0


def test_every_symbol_decision_is_logged(make_service, caplog):
    service = make_service()
    ap = _autopilot(service, FLAT_AAPL)
    with caplog.at_level("INFO", logger="trading_assistant.autopilot"):
        ap.run_once()
    assert (
        "autopilot decision symbol=AAPL signal=flat action=none "
        "reason=already_flat"
    ) in caplog.text


def _trip_breaker(service, scope):
    from trading_assistant.risk.breakers import trip_in_session

    with service.session_factory() as s:
        trip_in_session(
            s, scope, "test trip", "test", request_id=uuid4().hex
        )
        s.commit()


def test_self_heal_clears_transient_liquidity_breaker(make_service):
    from trading_assistant.risk.breakers import BreakerScope

    service = make_service()  # market open, AAPL @ $100
    scope = BreakerScope.liquidity("AAPL")
    _trip_breaker(service, scope)
    ap = _autopilot(service, FLAT_AAPL)
    ap._heal_transient_breakers()
    state = service.breakers.get(scope)
    assert state is None or not state.tripped


def test_self_heal_leaves_closed_market_breakers_alone(make_service):
    from trading_assistant.risk.breakers import BreakerScope

    service = make_service(market_open=False)
    scope = BreakerScope.liquidity("AAPL")
    _trip_breaker(service, scope)
    ap = _autopilot(service, FLAT_AAPL)
    ap._heal_transient_breakers()
    assert service.breakers.get(scope).tripped is True


def test_self_heal_never_touches_broker_drift(make_service):
    from trading_assistant.risk.breakers import BreakerScope

    service = make_service()
    drift = BreakerScope.broker_drift()
    _trip_breaker(service, drift)
    ap = _autopilot(service, FLAT_AAPL)
    ap._heal_transient_breakers()
    # broker_drift is a real safety latch — the autopilot must leave it tripped.
    assert service.breakers.get(drift).tripped is True


def test_run_once_skips_all_when_market_closed(make_service):
    service = make_service(market_open=False)
    ap = _autopilot(service, LONG_AAPL)
    assert ap.run_once() == []
    assert service.broker.submit_calls == 0
    assert _reasons(ap) == ["market_closed"]


def test_run_once_skips_flat_names_without_position(make_service):
    service = make_service()
    ap = _autopilot(service, FLAT_AAPL)
    assert ap.run_once() == []
    assert service.broker.submit_calls == 0


# ── startup resilience ────────────────────────────────────────────────────────
def test_startup_retries_only_transient_broker_outages():
    attempts = []
    sleeps = []

    def build():
        attempts.append(1)
        if len(attempts) < 3:
            raise StartupReconciliationFailed(
                "broker_reconciliation_dependency_unavailable"
            )
        return "container"

    assert (
        build_container_with_retry(
            build, attempts=3, retry_seconds=5, sleep=sleeps.append
        )
        == "container"
    )
    assert len(attempts) == 3
    assert sleeps == [5, 5]


def test_startup_gives_up_after_the_last_attempt():
    def build():
        raise StartupReconciliationFailed(
            "broker_reconciliation_dependency_unavailable"
        )

    with pytest.raises(StartupReconciliationFailed):
        build_container_with_retry(
            build, attempts=2, retry_seconds=0, sleep=lambda _s: None
        )


def test_startup_never_retries_real_drift():
    attempts = []

    def build():
        attempts.append(1)
        raise StartupReconciliationFailed("broker_position_drift")

    with pytest.raises(StartupReconciliationFailed):
        build_container_with_retry(
            build, attempts=5, retry_seconds=0, sleep=lambda _s: None
        )
    assert len(attempts) == 1


# ── runtime tenure ────────────────────────────────────────────────────────────
class _Guard:
    """Tenure guard double: owned for ``owned_checks`` checks, then lost."""

    def __init__(self, owned_checks):
        self.owned_checks = owned_checks
        self.checks = 0

    def ensure_owned(self):
        from trading_assistant.ops.tenure import TenureLost

        self.checks += 1
        if self.checks > self.owned_checks:
            raise TenureLost()


def test_lost_tenure_stops_the_cycle_before_any_work(make_service):
    from trading_assistant.ops.tenure import TenureLost

    service = make_service()
    ap = _autopilot(service, LONG_AAPL, tenure_guard=_Guard(owned_checks=0))
    with pytest.raises(TenureLost):
        ap.run_once()
    assert service.broker.submit_calls == 0


def test_tenure_lost_mid_cycle_blocks_the_order(make_service):
    """A lease that lapses while features are fetched must not submit."""
    from trading_assistant.ops.tenure import TenureLost

    service = make_service()
    guard = _Guard(owned_checks=1)
    ap = _autopilot(service, LONG_AAPL, tenure_guard=guard)
    with pytest.raises(TenureLost):
        ap.run_once()
    assert guard.checks == 2
    assert service.broker.submit_calls == 0


def test_owned_tenure_is_checked_before_each_order(make_service):
    service = make_service()
    guard = _Guard(owned_checks=10)
    ap = _autopilot(service, LONG_AAPL, tenure_guard=guard)
    assert len(ap.run_once()) == 1
    # cycle start, before recording intent (propose), before approving
    assert guard.checks == 3


# ── startup configuration checks ──────────────────────────────────────────────
def _with_autopilot(app_config, **autopilot):
    return app_config.model_copy(
        update={
            "autopilot": app_config.autopilot.model_copy(update=autopilot)
        }
    )


def test_checked_in_autopilot_profile_has_no_config_problems():
    from trading_assistant.autopilot import autopilot_config_problems
    from trading_assistant.config import load_config

    assert autopilot_config_problems(load_config()) == []


def test_config_problems_name_every_cycle_long_failure(app_config):
    from trading_assistant.autopilot import autopilot_config_problems

    config = _with_autopilot(
        app_config,
        universe=["AAPL", "BTC/USD", "ZZZZ"],
        notional_per_trade=Decimal("10000"),
    )
    problems = autopilot_config_problems(config)

    assert any(p.startswith("BTC/USD:") for p in problems)
    assert any(p.startswith("ZZZZ:") for p in problems)
    assert any("notional_per_trade" in p for p in problems)
    assert not any(p.startswith("AAPL:") for p in problems)


# ── live data refresh, as the autopilot runtime performs it ───────────────────
def _daily_bars(last_day, count=260, start_price=100.0):
    import pandas as pd

    index = pd.bdate_range(end=last_day, periods=count, tz="UTC")
    closes = [start_price + i * 0.5 for i in range(count)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * count,
        },
        index=index.rename("ts"),
    )


def test_autopilot_refreshes_a_stale_bar_cache_into_fresh_features(
    app_config, make_service, tmp_path
):
    """The production cache had July bars in October; the refresh must win."""
    import os

    from trading_assistant.analyst.live_features import (
        build_live_feature_provider,
    )
    from trading_assistant.app.limits import DurableRateLimiter
    _RUNTIME_ROLE = "daemon"  # the daemon hosts the autopilot
    from trading_assistant.backtest import data as backtest_data
    from trading_assistant.config import Secrets
    from trading_assistant.security.outbound import require_origin

    # The runtime role must be allowed to reach the historical-data origin.
    assert require_origin(
        _RUNTIME_ROLE, "alpaca.historical", "https://data.alpaca.markets"
    )

    today = datetime.now(timezone.utc).date()
    stale_day = today - timedelta(days=90)
    for symbol in ("AAPL", "SPY"):
        path = backtest_data.cache_path(tmp_path, symbol, "1Day")
        backtest_data.write_parquet_atomic(_daily_bars(stale_day), path)
        old = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp()
        os.utime(path, (old, old))

    downloads = []

    class FakeHistory:
        def get_stock_bars(self, request):
            downloads.append(request.symbol_or_symbols)
            frame = _daily_bars(today - timedelta(days=1))
            return SimpleNamespace(df=frame)

    service = make_service()
    provider = build_live_feature_provider(
        app_config,
        Secrets(alpaca_api_key="test-key", alpaca_secret_key="test-secret"),
        scheduled_service=service,
        rate_limiter=DurableRateLimiter(service.session_factory),
        alpaca_client_factory=lambda *_args: FakeHistory(),
        cache_dir=tmp_path,
        runtime_role=_RUNTIME_ROLE,
    )

    features = provider("AAPL")

    assert sorted(downloads) == ["AAPL", "SPY"]
    assert features.as_of.date() >= today - timedelta(days=4)
    assert features.sma_20 is not None and features.sma_200 is not None
    ap = _autopilot(service, {"AAPL": features})
    assert not ap._features_stale(features)
    assert provider is not None
