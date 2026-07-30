"""Phase 2 integration: propose creates a PENDING order and NEVER executes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from trading_assistant.broker.mock import MockBroker
from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from trading_assistant.assets import AssetClass
from trading_assistant.db.models import (
    AuditEvent,
    Order,
    RiskEvent,
    Rule,
    RuleGroup,
)
from trading_assistant.security.sensitive_fields import persist_sensitive
from trading_assistant.risk.clock import FakeClock
from trading_assistant.risk.breakers import BreakerScope
from trading_assistant.risk.engine import (
    BreakerTripIntent,
    RiskEngine,
    RiskResult,
)
from trading_assistant.service import TradingService


class SpyBroker(MockBroker):
    """MockBroker that records whether an order was ever sent to the broker."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.submit_calls = 0

    def submit_order(self, order):
        self.submit_calls += 1
        return super().submit_order(order)


def _service(app_config, session_factory, broker=None, market_open=True):
    broker = broker or SpyBroker()
    broker.set_price("AAPL", Decimal("100"))
    return TradingService(
        broker, session_factory, app_config, FakeClock(is_open=market_open)
    )


def _context(reason):
    return {
        "actor": "operator:test",
        "reason": reason,
        "request_id": f"service-test-{reason.replace(' ', '-')}",
    }


def test_legacy_killswitch_helpers_are_not_part_of_trading_service():
    assert not hasattr(TradingService, "_killswitch_for_symbol")
    assert not hasattr(TradingService, "_risk_is_blocked")


def test_propose_creates_pending_and_does_not_execute(app_config, session_factory):
    svc = _service(app_config, session_factory)
    res = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="400",
        **_context("pending proposal"),
    )

    assert res["status"] == "proposed"
    assert res["approved_by_risk"] is True
    assert res["executed"] is False
    # The core Phase-2 guarantee: nothing was sent to the broker.
    assert svc.broker.submit_calls == 0

    open_orders = svc.get_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0]["status"] == "proposed"


def test_proposal_risk_fault_persists_scoped_data_breaker(
    app_config,
    session_factory,
):
    class StaleQuoteBroker(SpyBroker):
        def get_quote(self, ticker):
            quote = super().get_quote(ticker)
            stale_at = datetime.now(timezone.utc) - timedelta(
                minutes=5
            )
            return replace(
                quote,
                as_of=stale_at,
                book_as_of=stale_at,
                trade_as_of=stale_at,
            )

    svc = _service(
        app_config,
        session_factory,
        broker=StaleQuoteBroker(),
    )

    result = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        **_context("persist proposal data fault"),
    )

    assert result["status"] == OrderStatus.REJECTED.value
    assert svc.breakers.is_tripped(
        BreakerScope.data(AssetClass.EQUITY)
    )


def test_execution_risk_fault_persists_before_submission_returns(
    app_config,
    session_factory,
):
    svc = _service(app_config, session_factory)
    proposal = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        **_context("execution breaker proposal"),
    )
    scope = BreakerScope.loss(AssetClass.EQUITY)
    svc.order_submission._risk_check = lambda *_args: RiskResult(
        approved=False,
        reasons=["daily total-loss limit reached"],
        breaker_trips=(
            BreakerTripIntent(
                scope,
                "daily total-loss limit reached",
            ),
        ),
    )

    result = svc.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="persist execution breaker before return",
        request_id="service-execution-breaker",
    )

    assert result["status"] == OrderStatus.REJECTED.value
    assert svc.breakers.is_tripped(scope)
    assert svc.broker.submit_calls == 0


def test_reduce_only_execution_latches_breach_and_still_reduces(
    app_config,
    session_factory,
    make_snapshot,
):
    broker = SpyBroker()
    broker._positions["AAPL"] = Position(
        "AAPL",
        Decimal("5"),
        Decimal("100"),
        Decimal("100"),
    )
    svc = _service(
        app_config,
        session_factory,
        broker=broker,
    )
    proposal = svc.propose_order(
        "AAPL",
        "sell",
        "market",
        qty="1",
        **_context("reduce-only breaker proposal"),
    )
    scope = BreakerScope.loss(AssetClass.EQUITY)
    risk = RiskEngine(app_config.risk).check(
        OrderRequest(
            ticker="AAPL",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            idempotency_key="real-reduce-only-risk",
            qty=Decimal("1"),
        ),
        make_snapshot(
            prices={"AAPL": Decimal("100")},
            positions=[
                Position(
                    "AAPL",
                    Decimal("5"),
                    Decimal("100"),
                    Decimal("100"),
                )
            ],
            realized_pnl_today=Decimal("-10000"),
        ),
    )
    assert risk.approved
    assert risk.bypassed_breakers == (scope.key,)
    svc.order_submission._risk_check = lambda *_args: risk

    result = svc.approve_order(
        proposal["order_id"],
        actor="operator:test",
        reason="reduce exposure while latching loss breaker",
        request_id="service-reduce-only-breaker",
    )

    assert result["status"] == OrderStatus.SUBMITTED.value
    assert svc.breakers.is_tripped(scope)
    assert broker.submit_calls == 1


def test_get_open_orders_includes_every_nonterminal_outbox_state(
    app_config,
    session_factory,
):
    svc = _service(app_config, session_factory)
    nonterminal = [
        OrderStatus.PROPOSED,
        OrderStatus.APPROVED,
        OrderStatus.APPROVAL_RECORDED,
        OrderStatus.SUBMITTING,
        OrderStatus.ACCEPTANCE_UNKNOWN,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
    ]
    with session_factory() as session:
        for index, status in enumerate(
            [*nonterminal, OrderStatus.FILLED],
            start=1,
        ):
            persist_sensitive(
                session,
                Order(
                    idempotency_key=f"open-state-{index}",
                    ticker="AAPL",
                    side="buy",
                    order_type="market",
                    notional=Decimal("10"),
                    status=status.value,
                ),
                {"approval_reason": "test fixture"},
            )
        session.commit()

    listed = {row["status"] for row in svc.get_open_orders()}

    assert listed == {status.value for status in nonterminal}


def test_rejected_order_is_persisted_with_reason(app_config, session_factory):
    svc = _service(app_config, session_factory)
    res = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="600",
        **_context("rejected proposal"),
    )  # > $500 limit

    assert res["status"] == "rejected"
    assert res["approved_by_risk"] is False
    assert any("per order" in r for r in res["risk_reasons"])
    assert svc.broker.submit_calls == 0

    with session_factory() as s:
        assert s.execute(select(func.count()).select_from(RiskEvent)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(Order)).scalar_one() == 1


def test_disallowed_ticker_rejected(app_config, session_factory):
    svc = _service(app_config, session_factory)
    svc.broker.set_price("TSLA", Decimal("100"))
    res = svc.propose_order(
        "TSLA",
        "buy",
        "market",
        notional="100",
        **_context("disallowed ticker"),
    )
    assert res["status"] == "rejected"
    assert any("allowlist" in r for r in res["risk_reasons"])
    assert svc.broker.submit_calls == 0


def test_unknown_ticker_rejects_cleanly_not_crash(app_config, session_factory):
    """A ticker the broker can't quote (e.g. a typo Alpaca doesn't recognize) must
    reject cleanly via the risk engine, not crash propose_order with a KeyError."""
    class NoQuoteBroker(SpyBroker):
        def get_quote(self, ticker):
            if ticker.upper() == "FOObar".upper():
                raise KeyError(ticker.upper())   # mirrors Alpaca's missing-symbol KeyError
            return super().get_quote(ticker)

    svc = _service(app_config, session_factory, broker=NoQuoteBroker())
    res = svc.propose_order(
        "FOOBAR",
        "buy",
        "market",
        notional="100",
        **_context("unknown ticker"),
    )  # off-allowlist + unquotable
    assert res["status"] == "rejected"
    assert any("allowlist" in r for r in res["risk_reasons"])
    assert svc.broker.submit_calls == 0


def test_market_closed_rejects(app_config, session_factory):
    svc = _service(app_config, session_factory, market_open=False)
    res = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="100",
        **_context("closed market"),
    )
    assert res["status"] == "rejected"
    assert any("market is closed" in r for r in res["risk_reasons"])


def test_snapshot_uses_broker_positions(app_config, session_factory):
    broker = SpyBroker(
        positions=[Position("AAPL", Decimal("19"), Decimal("100"), Decimal("100"))]
    )
    svc = _service(app_config, session_factory, broker=broker)
    # Existing $1900 position + $500 order -> $2400 > $2000 per-ticker limit.
    res = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="500",
        **_context("position limit"),
    )
    assert res["status"] == "rejected"
    assert any("per ticker" in r for r in res["risk_reasons"])


def test_second_order_counts_first_outstanding_order(app_config, session_factory):
    config = app_config.model_copy(
        update={
            "risk": app_config.risk.model_copy(
                update={"max_notional_per_order": 2000}
            )
        }
    )
    svc = _service(config, session_factory)
    first = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="1800",
        **_context("first outstanding order"),
    )
    assert first["status"] == "proposed"
    assert svc.approve_order(
        first["order_id"],
        actor="operator:test",
        reason="service test",
        request_id="service-test-approval",
    )["status"] == "submitted"

    second = svc.propose_order(
        "AAPL",
        "buy",
        "market",
        notional="400",
        **_context("second outstanding order"),
    )

    assert second["status"] == "rejected"
    assert any("per ticker" in reason for reason in second["risk_reasons"])


def test_conditional_rule_crud(app_config, session_factory):
    svc = _service(app_config, session_factory)
    created = svc.create_conditional_rule(
        "AAPL",
        {"price_below": 175},
        {"side": "buy", "notional": "50"},
        **_context("conditional rule create"),
    )
    assert created["state"] == "active"
    assert svc.list_rules()[0]["condition"] == {
        "type": "price",
        "direction": "below",
        "price": "175",
    }

    canceled = svc.cancel_rule(
        created["rule_id"],
        **_context("conditional rule cancel"),
    )
    assert canceled["canceled"] is True
    assert svc.list_rules()[0]["state"] == "canceled"
    with session_factory() as session:
        rule = session.get(Rule, created["rule_id"])
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.request_id
                == "service-test-conditional-rule-cancel"
            )
        ).all()
    assert {
        (audit.action, audit.target_id)
        for audit in audits
    } == {
        ("rule.cancel", str(created["rule_id"])),
        ("rule_group.cancel", str(rule.group_id)),
    }


def test_cancel_rule_keeps_group_active_until_processing_sibling_is_canceled(
    app_config, session_factory
):
    svc = _service(app_config, session_factory)
    first = svc.create_conditional_rule(
        "AAPL",
        {"price_below": 90},
        {"side": "buy", "notional": "50"},
        group_key="processing-sibling",
        **_context("first sibling rule"),
    )
    second = svc.create_conditional_rule(
        "AAPL",
        {"price_above": 110},
        {"side": "buy", "notional": "50"},
        group_key="processing-sibling",
        **_context("second sibling rule"),
    )
    with session_factory() as session:
        first_rule = session.get(Rule, first["rule_id"])
        second_rule = session.get(Rule, second["rule_id"])
        second_rule.state = "processing"
        group = session.get(RuleGroup, first_rule.group_id)
        initial_version = group.version
        session.commit()

    assert svc.cancel_rule(
        first["rule_id"],
        **_context("cancel first sibling"),
    )["canceled"] is True

    with session_factory() as session:
        first_rule = session.get(Rule, first["rule_id"])
        second_rule = session.get(Rule, second["rule_id"])
        group = session.get(RuleGroup, first_rule.group_id)
        assert first_rule.state == "canceled"
        assert second_rule.state == "processing"
        assert group.state == "active"
        assert group.version == initial_version

    assert svc.cancel_rule(
        second["rule_id"],
        **_context("cancel second sibling"),
    )["canceled"] is True

    with session_factory() as session:
        second_rule = session.get(Rule, second["rule_id"])
        group = session.get(RuleGroup, second_rule.group_id)
        assert second_rule.state == "canceled"
        assert group.state == "canceled"
        assert group.version == initial_version + 1


def test_cancel_rule_can_cancel_fill_activated_pending_rule_and_group(
    app_config,
    session_factory,
):
    svc = _service(app_config, session_factory)
    rule_id = svc.rule_application.create_rule(
        {
            "ticker": "AAPL",
            "kind": "stop",
            "condition": {
                "type": "price",
                "direction": "below",
                "price": "90",
            },
            "action": {
                "side": "sell",
                "order_type": "market",
                "qty": "1",
            },
            "activation": "on_entry_fill",
        },
        **_context("pending exit rule"),
    )

    result = svc.cancel_rule(
        rule_id,
        **_context("cancel pending exit rule"),
    )

    assert result["canceled"] is True
    with session_factory() as session:
        rule = session.get(Rule, rule_id)
        group = session.get(RuleGroup, rule.group_id)
        assert rule.state == "canceled"
        assert group.state == "canceled"


def test_existing_group_rejects_mixed_persisted_activation_policy(
    app_config,
    session_factory,
):
    svc = _service(app_config, session_factory)
    immediate = {
        "ticker": "AAPL",
        "kind": "price",
        "condition": {
            "type": "price",
            "direction": "above",
            "price": "110",
        },
        "action": {
            "side": "buy",
            "order_type": "market",
            "qty": "1",
        },
        "group_key": "persisted-activation-policy",
    }
    svc.rule_application.create_rule(
        immediate,
        **_context("create immediate activation group"),
    )

    with pytest.raises(
        ValueError,
        match="persisted activation policy",
    ):
        svc.rule_application.create_rule(
            {
                "ticker": "AAPL",
                "kind": "stop",
                "condition": {
                    "type": "price",
                    "direction": "below",
                    "price": "90",
                },
                "action": {
                    "side": "sell",
                    "order_type": "market",
                    "qty": "1",
                },
                "group_key": "persisted-activation-policy",
                "activation": "on_entry_fill",
            },
            **_context("reject mixed activation group"),
        )


@pytest.mark.parametrize("terminal_state", ["triggered", "failed"])
def test_cancel_rule_terminal_winner_is_explicit_noop(
    app_config, session_factory, terminal_state
):
    svc = _service(app_config, session_factory)
    created = svc.create_conditional_rule(
        "AAPL",
        {"price_below": 90},
        {"side": "buy", "notional": "50"},
        **_context("terminal rule setup"),
    )
    with session_factory() as session:
        rule = session.get(Rule, created["rule_id"])
        group = session.get(RuleGroup, rule.group_id)
        rule.state = terminal_state
        group.state = terminal_state
        group.terminal_rule_id = rule.id
        group.version = 7
        session.commit()

    result = svc.cancel_rule(
        created["rule_id"],
        **_context("terminal rule cancel"),
    )

    assert result["canceled"] is False
    assert "terminal" in result["error"]
    with session_factory() as session:
        rule = session.get(Rule, created["rule_id"])
        group = session.get(RuleGroup, rule.group_id)
        assert rule.state == terminal_state
        assert group.state == terminal_state
        assert group.terminal_rule_id == rule.id
        assert group.version == 7


def test_market_data_and_account_summary(app_config, session_factory):
    svc = _service(app_config, session_factory)
    md = svc.get_market_data("AAPL")
    assert md["ticker"] == "AAPL"
    assert md["last"] == "100"

    summary = svc.get_account_summary()
    assert "buying_power" in summary
    assert isinstance(summary["positions"], list)
