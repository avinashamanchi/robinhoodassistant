"""TradingService — the orchestration core shared by the MCP server and (Phase 3)
the FastAPI host.

Responsibilities:
* Assemble a :class:`PortfolioSnapshot` from the broker + DB (the A1 "caller").
* Run the risk engine at proposal time and record the outcome.
* Persist proposals. **It never calls ``broker.submit_order``** — execution is a
  separate, human-gated step added in Phase 3. This is the structural guarantee
  that the LLM can only propose.

Every public method returns plain dicts so it maps cleanly onto MCP tool results.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from .broker.base import BrokerClient
from .broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderTimeInForce,
    OrderType,
    PortfolioSnapshot,
    Position,
)
from .assets import AssetClass
from .config import AppConfig, BrokerKind, TradingMode
from .db.models import (
    AuditEvent,
    CircuitBreakerState,
    FILL_RECONCILIATION_REQUIRED,
    Fill,
    LLMDecision,
    Order,
    OrderStateMachine,
    NONTERMINAL_STATES,
    PLAN_CANCEL_INDETERMINATE,
    PLAN_CANCEL_REQUESTED,
    PLAN_CANCEL_SETTLED,
    Proposal,
    RiskEvent,
    Rule,
    RuleGroup,
    TERMINAL_STATES,
    fill_has_trusted_identity,
    utcnow,
)
from .db.lifecycle_proofs import augment_lifecycle_detail_json
from .dependencies import RequiredDependencyUnavailable
from .orders.application import (
    ApprovalCommand,
    ApprovalConflict as ApprovalApplicationConflict,
    OrderApplicationService,
)
from .orders.snapshot import PortfolioSnapshotService
from .orders.reconciliation import ReconciliationService
from .orders.safety_state import (
    read_persisted_safety_truth,
    unknown_persisted_safety_truth,
)
from .orders.submission import OrderSubmissionService, order_to_request
from .orders.startup import (
    StartupReconciliationFailed,
    StartupReconciliationGate,
)
from .risk.breakers import BreakerScope, BreakerService, trip_in_session
from .risk.clock import CryptoClock, MarketClock
from .risk.engine import RiskEngine
from .risk.pnl import FillLike, realized_pnl_today
from .risk.submission_barrier import (
    SubmissionBarrier,
    serialized_writer,
)
from .security.sensitive_fields import persist_sensitive, sensitive_store

# Every nonterminal state must remain visible to operators and MCP callers,
# especially indeterminate outbox states that require reconciliation.
_OPEN_STATUSES = tuple(status.value for status in NONTERMINAL_STATES)

_EXIT_RESERVATION_STATUSES = (
    OrderStatus.APPROVED.value,
    OrderStatus.APPROVAL_RECORDED.value,
    OrderStatus.SUBMITTING.value,
    OrderStatus.ACCEPTANCE_UNKNOWN.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)

_RESUMABLE_RULE_STATES = ("pending", "active", "processing")
_RESUMABLE_RULE_GROUP_STATES = ("pending", "active")

log = logging.getLogger(__name__)


def _require_mutation_context(
    actor: str,
    reason: str,
    request_id: str,
) -> tuple[str, str, str]:
    actor = actor.strip()
    reason = reason.strip()
    request_id = request_id.strip()
    if not actor or not reason or not request_id:
        raise ValueError(
            "mutation actor, reason, and request_id must be non-empty"
        )
    return actor, reason, request_id


def _persist_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    request_id: str,
    reason: str,
    result_code: str,
    idempotency_key: str = "",
    detail_json: str = "{}",
    created_at=None,
    lifecycle_proof: bool = True,
) -> AuditEvent:
    if lifecycle_proof:
        detail_json = augment_lifecycle_detail_json(
            session,
            target_type=target_type,
            target_id=target_id,
            detail_json=detail_json,
        )
    event = AuditEvent(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_id=request_id,
        idempotency_key=idempotency_key,
        result_code=result_code,
        created_at=created_at or utcnow(),
    )
    persist_sensitive(
        session,
        event,
        {"reason": reason, "detail_json": detail_json},
    )
    return event


def _persist_risk(
    session: Session,
    *,
    order_id: int | None = None,
    event_type: str,
    reason: str,
) -> RiskEvent:
    event = RiskEvent(order_id=order_id, event_type=event_type)
    persist_sensitive(session, event, {"reason": reason})
    return event


class TradingService:
    def __init__(
        self,
        broker: BrokerClient,
        session_factory: sessionmaker[Session],
        config: AppConfig,
        clock: MarketClock,
        crypto_clock: Optional[MarketClock] = None,
        external_source=None,
        *,
        require_startup_reconciliation: bool = False,
    ) -> None:
        self.broker = broker
        self.session_factory = session_factory
        self.config = config
        self.external_source = external_source  # read-only; may be None
        # Equity attributes kept for backward compatibility with existing callers.
        self.clock = clock
        self.risk = RiskEngine(config.risk)
        # Per-asset-class routing (Phase 7). Crypto falls back to equity limits if
        # no crypto_risk section is configured.
        crypto_cfg = config.crypto_risk or config.risk
        self._clocks: dict[AssetClass, MarketClock] = {
            AssetClass.EQUITY: clock,
            AssetClass.CRYPTO: crypto_clock or CryptoClock(),
        }
        self._risk: dict[AssetClass, RiskEngine] = {
            AssetClass.EQUITY: self.risk,
            AssetClass.CRYPTO: RiskEngine(crypto_cfg),
        }
        self.submission_barrier = SubmissionBarrier(session_factory)
        self.breakers = BreakerService(session_factory)
        self.startup_reconciliation = StartupReconciliationGate(
            session_factory,
            broker.reconciliation_key,
            enabled=require_startup_reconciliation,
        )
        self.snapshot_service = PortfolioSnapshotService(
            session_factory,
            broker,
            self._clock_for,
            self._external_positions_map,
            lambda asset_class: (
                (self.config.crypto_risk or self.config.risk)
                if asset_class is AssetClass.CRYPTO
                else self.config.risk
            ),
            self.breakers,
            startup_reconciliation_key=(
                broker.reconciliation_key
                if require_startup_reconciliation
                else None
            ),
        )
        self.order_application = OrderApplicationService(session_factory)
        self.order_submission = OrderSubmissionService(
            self.order_application.repository,
            session_factory,
            broker,
            self.snapshot_service,
            lambda symbol: self._risk_for(self._asset_class(symbol)),
            utcnow,
        )
        self.reconciliation = ReconciliationService(
            session_factory,
            broker,
            self.order_application.repository,
            self.breakers,
        )
        from .rules.application import RuleApplicationService
        from .rules.repository import RuleRepository

        self.rule_repository = RuleRepository(
            session_factory, owner=f"rule-worker-{uuid.uuid4().hex}"
        )
        self.rule_application = RuleApplicationService(
            self, self.rule_repository
        )
        self.order_submission.reconcile_immediate_fill = (
            self._reconcile_immediate_submission_fill
        )
        self.order_submission.validate_plan_exit = (
            self._validate_plan_exit_submission
        )

    def _audit_dependency_failure(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict[str, object],
    ) -> None:
        """Persist exact failed-mutation provenance without provider detail."""
        with self.session_factory() as session:
            _persist_audit(
                    session,
                    actor=actor,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    request_id=request_id,
                    reason=reason,
                    result_code="dependency_unavailable",
                    detail_json=json.dumps(detail, sort_keys=True),
                    lifecycle_proof=False,
            )
            session.commit()

    # ── asset-class routing helpers ────────────────────────────
    @staticmethod
    def _asset_class(symbol: str) -> AssetClass:
        return AssetClass.for_symbol(symbol)

    def _clock_for(self, ac: AssetClass) -> MarketClock:
        return self._clocks[ac]

    def _risk_for(self, ac: AssetClass) -> RiskEngine:
        return self._risk[ac]

    def _loss_limit_for(self, ac: AssetClass) -> Decimal:
        cfg = self.config.crypto_risk if ac is AssetClass.CRYPTO else self.config.risk
        cfg = cfg or self.config.risk
        return Decimal(str(cfg.daily_realized_loss_limit))

    def market_is_open(self, ticker: str) -> bool:
        """Return the configured broker clock state for a symbol's asset class."""
        ac = self._asset_class(ticker)
        return self._clock_for(ac).is_open()

    # ── snapshot assembly (A1) ─────────────────────────────────
    def _realized_pnl_today(
        self, session: Session, asset_class: AssetClass = AssetClass.EQUITY
    ) -> Decimal:
        rows = session.execute(select(Fill)).scalars().all()
        # Only this asset class's fills count toward its daily boundary/limit.
        fills = [
            FillLike(r.ticker, r.side, r.qty, r.price, r.filled_at)
            for r in rows
            if (
                AssetClass.for_symbol(r.ticker) is asset_class
                and fill_has_trusted_identity(r)
            )
        ]
        boundary = self._clock_for(asset_class).most_recent_open()
        return realized_pnl_today(
            fills,
            asset_class=asset_class,
            boundary=boundary,
        )

    def assemble_snapshot(
        self,
        session: Session,
        tickers: list[str],
        asset_class: AssetClass = AssetClass.EQUITY,
        exclude_order_id: int | None = None,
        quote_overrides: dict[str, object] | None = None,
        required_dependencies: bool = False,
    ) -> PortfolioSnapshot:
        return self.snapshot_service.assemble(
            session,
            tickers,
            asset_class,
            exclude_order_id=exclude_order_id,
            quote_overrides=quote_overrides,
            required_dependencies=required_dependencies,
        )

    def _external_positions_map(self) -> dict:
        """Read-only external holdings keyed by ticker (empty if no source/down)."""
        if self.external_source is None:
            return {}
        try:
            return {p.ticker.upper(): p for p in self.external_source.get_positions()}
        except Exception:  # graceful degradation — never break the trading path
            return {}

    # ── read-only tools ────────────────────────────────────────
    def get_market_data(self, ticker: str) -> dict[str, Any]:
        q = self.broker.get_quote(ticker)
        change = q.day_change_pct
        return {
            "ticker": q.ticker,
            "last": str(q.last),
            "bid": str(q.bid),
            "ask": str(q.ask),
            "day_change_pct": None if change is None else f"{change:.2f}",
        }

    def _read_valid_broker_positions(self) -> list[Position]:
        positions = self.broker.get_positions()
        seen_tickers: set[str] = set()
        for position in positions:
            if not position.risk_values_valid:
                raise RequiredDependencyUnavailable
            ticker_key = position.ticker.strip().upper()
            if ticker_key in seen_tickers:
                raise RequiredDependencyUnavailable
            seen_tickers.add(ticker_key)
        return positions

    def get_account_summary(self) -> dict[str, Any]:
        try:
            acct = self.broker.get_account()
            if not acct.is_valid:
                raise RequiredDependencyUnavailable
            broker_positions = self._read_valid_broker_positions()
            positions = [
                {
                    "ticker": p.ticker,
                    "qty": str(p.qty),
                    "avg_entry_price": str(p.avg_entry_price),
                    "current_price": str(p.current_price),
                    "market_value": str(p.market_value),
                }
                for p in broker_positions
            ]
            gross_exposure = sum(
                (abs(position.market_value) for position in broker_positions),
                Decimal(0),
            )
        except Exception:
            raise RequiredDependencyUnavailable from None
        return {
            "observed_at": utcnow().isoformat(),
            "buying_power": str(acct.buying_power),
            "equity": str(acct.equity),
            "cash": str(acct.cash),
            "gross_exposure": str(gross_exposure),
            "positions": positions,
        }

    def get_open_orders(self) -> list[dict[str, Any]]:
        with self.session_factory() as s:
            rows = (
                s.execute(select(Order).where(Order.status.in_(_OPEN_STATUSES)))
                .scalars()
                .all()
            )
            return [self._order_dict(o) for o in rows]

    def get_order_status(self, order_id: int) -> Optional[dict[str, Any]]:
        with self.session_factory() as s:
            o = s.get(Order, order_id)
            return self._order_dict(o) if o else None

    # ── propose (NEVER executes) ───────────────────────────────
    @serialized_writer
    def propose_order(
        self,
        ticker: str,
        side: str,
        order_type: str,
        qty: Optional[str] = None,
        notional: Optional[str] = None,
        limit_price: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        time_in_force: str = OrderTimeInForce.DAY.value,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Create a PENDING proposal after a risk pre-check. Does NOT trade.

        A rejected order is still persisted (as REJECTED with a logged reason) so
        the UI can show why. An accepted order becomes PROPOSED, awaiting human
        approval — which will re-run the risk engine at execution time (A6/Phase 3).
        """
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        key = idempotency_key or uuid.uuid4().hex
        if idempotency_key is not None:
            with self.session_factory() as s:
                existing = s.execute(
                    select(Order).where(Order.idempotency_key == key)
                ).scalar_one_or_none()
                if existing is not None:
                    return {
                        "order_id": existing.id,
                        "status": existing.status,
                        "approved_by_risk": (
                            existing.status != OrderStatus.REJECTED.value
                        ),
                        "risk_reasons": [],
                        "risk_warnings": [],
                        "executed": False,
                        "idempotent_replay": True,
                    }

        order_req = OrderRequest(
            ticker=ticker.upper(),
            side=OrderSide(side.lower()),
            order_type=OrderType(order_type.lower()),
            idempotency_key=key,
            qty=Decimal(qty) if qty is not None else None,
            notional=Decimal(notional) if notional is not None else None,
            limit_price=Decimal(limit_price) if limit_price is not None else None,
            time_in_force=OrderTimeInForce(time_in_force.lower()),
        )

        ac = self._asset_class(order_req.ticker)
        with self.session_factory() as read_session:
            snapshot = self.assemble_snapshot(
                read_session, [order_req.ticker], ac
            )
            result = self._risk_for(ac).check(order_req, snapshot)

        with self.session_factory() as s:
            order = Order(
                idempotency_key=order_req.idempotency_key,
                ticker=order_req.ticker,
                side=order_req.side.value,
                order_type=order_req.order_type.value,
                qty=order_req.qty,
                notional=order_req.notional,
                limit_price=order_req.limit_price,
                status=OrderStatus.PROPOSED.value,
                submission_payload_json=(
                    json.dumps(
                        {
                            "time_in_force": (
                                order_req.time_in_force.value
                            )
                        },
                        sort_keys=True,
                    )
                    if order_req.time_in_force is not OrderTimeInForce.DAY
                    else "{}"
                ),
            )
            persist_sensitive(
                s,
                order,
                {"approval_reason": "approval pending"},
            )
            risk_cfg = self.config.crypto_risk if ac is AssetClass.CRYPTO else self.config.risk
            ttl = (risk_cfg or self.config.risk).proposal_ttl_minutes
            persist_sensitive(
                s,
                Proposal(
                    order_id=order.id,
                    ttl_minutes=ttl,
                    expires_at=utcnow() + timedelta(minutes=ttl),
                ),
                {"reasoning": reason},
            )

            if result.rejected:
                OrderStateMachine.transition(order, OrderStatus.REJECTED)
                _persist_risk(
                        s,
                        order_id=order.id,
                        event_type="rejection",
                        reason=result.reason_text(),
                )
            # Non-blocking warnings (e.g. cross-broker concentration) are logged
            # but never change the outcome.
            for warning in result.warnings:
                _persist_risk(
                    s,
                    order_id=order.id,
                    event_type="warning",
                    reason=warning,
                )
            for intent in result.breaker_trips:
                trip_in_session(
                    s,
                    intent.scope,
                    intent.reason,
                    actor,
                    request_id=request_id,
                    audit_reason=reason,
                )
            _persist_audit(
                    s,
                    actor=actor,
                    action="order.propose",
                    target_type="order",
                    target_id=str(order.id),
                    request_id=request_id,
                    reason=reason,
                    result_code=order.status,
            )

            s.commit()
            return {
                "order_id": order.id,
                "status": order.status,
                "approved_by_risk": result.approved,
                "risk_reasons": result.reasons,
                "risk_warnings": result.warnings,
                "executed": False,  # invariant: proposing never executes
            }

    # ── execution (human-gated) ────────────────────────────────
    def _order_request_from(self, order: Order) -> OrderRequest:
        return order_to_request(order)

    def approve_order(
        self,
        order_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Record an identified approval, then send through the durable outbox."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        try:
            approval = self.order_application.approve(
                ApprovalCommand(
                    order_id,
                    actor,
                    reason,
                    utcnow(),
                    request_id,
                )
            )
        except KeyError:
            return {"order_id": order_id, "error": "not found", "executed": False}
        except ApprovalApplicationConflict:
            with self.session_factory() as session:
                current = session.get(Order, order_id)
                if (
                    current is not None
                    and current.status
                    == OrderStatus.APPROVAL_RECORDED.value
                ):
                    approval = None
                else:
                    return {
                        "order_id": order_id,
                        "status": current.status if current else None,
                        "executed": False,
                        "error": "order not in PROPOSED state (already decided?)",
                    }
        if (
            approval is not None
            and approval.status is OrderStatus.EXPIRED
        ):
            return {
                "order_id": order_id,
                "status": OrderStatus.EXPIRED.value,
                "executed": False,
                "error": "proposal expired",
            }

        try:
            result = self.order_submission.submit(
                order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
        except RequiredDependencyUnavailable:
            self._audit_dependency_failure(
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="order.submit",
                target_type="order",
                target_id=str(order_id),
                detail={"stage": "execution_health"},
            )
            raise RequiredDependencyUnavailable from None
        return {
            "order_id": order_id,
            "status": result.status.value,
            "executed": result.status
            in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED},
            "broker_order_id": result.broker_order_id,
            "risk_reasons": list(result.risk_reasons),
        }

    def propose_bracket_order(
        self,
        order_req: OrderRequest,
        take_profit,
        stop_loss,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Persist a bracket proposal; only ``approve_order`` may execute it."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        if not hasattr(self.broker, "submit_bracket"):
            return {"error": "broker does not support bracket orders", "executed": False}
        try:
            take_profit = Decimal(str(take_profit))
            stop_loss = Decimal(str(stop_loss))
        except Exception as exc:
            raise ValueError("bracket prices must be valid decimals") from exc
        if take_profit <= 0 or stop_loss <= 0:
            raise ValueError("bracket prices must be positive")
        with self.submission_barrier.hold_writer():
            with self.session_factory() as s:
                existing = s.execute(
                    select(Order).where(
                        Order.idempotency_key
                        == order_req.idempotency_key
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    order_id = existing.id
                else:
                    order = Order(
                        idempotency_key=order_req.idempotency_key,
                        ticker=order_req.ticker,
                        side=order_req.side.value,
                        order_type=order_req.order_type.value,
                        qty=order_req.qty,
                        notional=order_req.notional,
                        limit_price=order_req.limit_price,
                        status=OrderStatus.PROPOSED.value,
                        submission_kind="bracket",
                        submission_payload_json=json.dumps(
                            {
                                "take_profit": str(take_profit),
                                "stop_loss": str(stop_loss),
                            }
                        ),
                    )
                    persist_sensitive(
                        s,
                        order,
                        {"approval_reason": "approval pending"},
                    )
                    risk_cfg = (
                        self.config.crypto_risk
                        if self._asset_class(order_req.ticker)
                        is AssetClass.CRYPTO
                        else self.config.risk
                    )
                    ttl = (
                        risk_cfg or self.config.risk
                    ).proposal_ttl_minutes
                    persist_sensitive(
                        s,
                        Proposal(
                            order_id=order.id,
                            ttl_minutes=ttl,
                            expires_at=(
                                utcnow()
                                + timedelta(minutes=ttl)
                            ),
                        ),
                        {"reasoning": reason},
                    )
                    _persist_audit(
                            s,
                            actor=actor,
                            action="order.propose",
                            target_type="order",
                            target_id=str(order.id),
                            request_id=request_id,
                            reason=reason,
                            result_code=(
                                OrderStatus.PROPOSED.value
                            ),
                            detail_json=json.dumps(
                                {
                                    "submission_kind": (
                                        "bracket"
                                    )
                                },
                                sort_keys=True,
                            ),
                    )
                    s.commit()
                    order_id = order.id

            with self.session_factory() as session:
                current = session.get(Order, order_id)
                assert current is not None
                current_status = OrderStatus(current.status)
                broker_order_id = current.broker_order_id
        return {
            "executed": False,
            "bracket": True,
            "order_id": order_id,
            "status": current_status.value,
            "broker_order_id": broker_order_id,
            "risk_reasons": [],
        }

    @serialized_writer
    def reject_order(
        self,
        order_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        with self.session_factory() as s:
            order = s.get(Order, order_id)
            if order is None:
                return {"order_id": order_id, "error": "not found"}
            if order.status != OrderStatus.PROPOSED.value:
                return {
                    "order_id": order_id,
                    "status": order.status,
                    "error": "only PROPOSED orders can be rejected",
                }
            OrderStateMachine.transition(order, OrderStatus.REJECTED)
            _persist_risk(
                    s,
                    order_id=order.id,
                    event_type="rejection",
                    reason=reason,
            )
            _persist_audit(
                    s,
                    actor=actor,
                    action="order.reject",
                    target_type="order",
                    target_id=str(order_id),
                    request_id=request_id,
                    reason=reason,
                    result_code=OrderStatus.REJECTED.value,
            )
            s.commit()
            return {"order_id": order_id, "status": order.status}

    def write_heartbeat(self, source: str = "daemon") -> None:
        from .db.models import Heartbeat
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        source = source.strip()
        if not source or len(source) > 24:
            raise ValueError("heartbeat source is invalid")
        with self.session_factory() as s:
            if s.get_bind().dialect.name == "sqlite":
                statement = sqlite_insert(Heartbeat).values(
                    source=source,
                    at=utcnow(),
                )
                statement = statement.on_conflict_do_update(
                    index_elements=[Heartbeat.source],
                    set_={"at": statement.excluded.at},
                )
                s.execute(statement)
            else:
                heartbeat = s.scalar(
                    select(Heartbeat).where(
                        Heartbeat.source == source
                    )
                )
                if heartbeat is None:
                    s.add(Heartbeat(source=source))
                else:
                    heartbeat.at = utcnow()
            s.commit()

    def health(self, *, safety=None) -> dict[str, Any]:
        """Authenticated operational health and durable local safety truth."""
        if safety is None:
            safety = read_persisted_safety_truth(
                self.session_factory
            )
        observed_at = safety.observed_at
        operating_context = {
            "broker": (
                "Alpaca"
                if self.config.trading.broker is BrokerKind.ALPACA
                else self.config.trading.broker.value.title()
            ),
            "mode": self.config.trading.mode.value,
            "observed_at": observed_at.isoformat(),
        }
        try:
            age = (
                observed_at - safety.heartbeat_at
            ).total_seconds() if safety.heartbeat_at else None
            eq_state = safety.breaker(
                BreakerScope.loss(AssetClass.EQUITY).key
            )
            cr_state = safety.breaker(
                BreakerScope.loss(AssetClass.CRYPTO).key
            )
            response = {
                **operating_context,
                "db_ok": safety.complete,
                "heartbeat_age_seconds": (
                    round(age, 1)
                    if age is not None
                    else None
                ),
                "daemon_alive": (
                    age is not None
                    and age
                    < self.config.daemon.heartbeat_stale_seconds
                ),
                "killswitch": {
                    "equity": bool(
                        eq_state and eq_state.tripped
                    ),
                    "crypto": bool(
                        cr_state and cr_state.tripped
                    ),
                },
                "killswitch_generation": {
                    "equity": (
                        eq_state.generation
                        if eq_state is not None
                        else 0
                    ),
                    "crypto": (
                        cr_state.generation
                        if cr_state is not None
                        else 0
                    ),
                },
                "active_breakers": [
                    breaker.as_active_dict()
                    for breaker in safety.active_breakers
                ],
                "safety": safety.as_dict(),
            }
            if not safety.complete:
                response["error"] = "database_unavailable"
            return response
        except Exception:
            return {
                **operating_context,
                "db_ok": False,
                "error": "database_unavailable",
                "safety": (
                    unknown_persisted_safety_truth(
                        observed_at=observed_at
                    ).as_dict()
                ),
            }

    def panic(
        self,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Latch and execute panic, returning only confirmed broker/local truth."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        report = self.reconciliation.panic(
            actor,
            reason,
            request_id=request_id,
        )
        return {
            "safe": report.safe,
            "local_enumeration": report.local_enumeration,
            "remote_enumeration": report.remote_enumeration,
            "confirmed_canceled": list(report.confirmed_canceled),
            "unconfirmed_order_ids": list(report.unconfirmed_order_ids),
            "remote_open_order_ids": list(report.remote_open_order_ids),
            "unsafe_local_state": report.unsafe_local_state.as_dict(),
            "message": report.message,
        }

    def trip_all_killswitches(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> None:
        """Fail closed across asset classes for an operational safety fault."""
        actor, reason, request_id = _require_mutation_context(
            actor, reason, request_id
        )
        self.breakers.trip(
            BreakerScope.operator_global(),
            reason,
            actor,
            request_id=request_id,
            audit_reason=reason,
        )

    def _reset_symbols(
        self,
        asset_class: AssetClass,
    ) -> list[str]:
        config = (
            self.config.crypto_risk or self.config.risk
            if asset_class is AssetClass.CRYPTO
            else self.config.risk
        )
        return sorted(
            {
                symbol.upper()
                for symbol in config.ticker_allowlist
                if self._asset_class(symbol) is asset_class
            }
        )

    def _reset_snapshot(
        self,
        symbols: list[str],
        asset_class: AssetClass,
    ) -> PortfolioSnapshot:
        if not symbols:
            raise RequiredDependencyUnavailable
        if len(symbols) == 1:
            return self.snapshot_service.assemble_for_execution(
                symbols[0]
            )
        with self.session_factory() as session:
            return self.assemble_snapshot(
                session,
                symbols,
                asset_class,
                required_dependencies=True,
            )

    def _collect_scope_reset_health(
        self,
        scope: BreakerScope,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        if scope.kind.value in {"loss", "drawdown", "data"}:
            asset_class = AssetClass(scope.target)
            symbols = self._reset_symbols(asset_class)
            snapshot = self._reset_snapshot(
                (
                    symbols
                    if scope.kind.value == "data"
                    else symbols[:1]
                ),
                asset_class,
            )
            base = {
                "captured_at": snapshot.as_of.isoformat(),
                "asset_class": asset_class.value,
                "symbols": symbols,
                "broker_reconciled": snapshot.broker_reconciled,
            }
            if scope.kind.value == "data":
                quotes_valid = (
                    snapshot.quote_fresh is True
                    and set(symbols).issubset(snapshot.quotes)
                    and all(
                        snapshot.quotes[symbol].is_valid
                        for symbol in symbols
                    )
                )
                if not quotes_valid:
                    raise RequiredDependencyUnavailable
                return {
                    **base,
                    "quote_fresh": True,
                    "quote_count": len(symbols),
                }
            if scope.kind.value == "loss":
                daily_total = (
                    snapshot.realized_pnl_today
                    + snapshot.unrealized_pnl_today
                )
                config = (
                    self.config.crypto_risk or self.config.risk
                    if asset_class is AssetClass.CRYPTO
                    else self.config.risk
                )
                healthy = (
                    snapshot.daily_pnl_complete is True
                    and snapshot.broker_reconciled is True
                    and snapshot.account_complete is True
                    and snapshot.pending_exposure_complete is True
                    and snapshot.quote_fresh is True
                    and snapshot.market_clock_complete is True
                    and daily_total.is_finite()
                    and snapshot.realized_pnl_today.is_finite()
                    and daily_total
                    > -Decimal(str(config.max_daily_total_loss))
                    and snapshot.realized_pnl_today
                    > -Decimal(
                        str(config.daily_realized_loss_limit)
                    )
                )
                if not healthy:
                    raise RequiredDependencyUnavailable
                return {
                    **base,
                    "daily_pnl_complete": True,
                    "daily_total_pnl": str(daily_total),
                    "realized_pnl_today": str(
                        snapshot.realized_pnl_today
                    ),
                    "account_equity": str(
                        snapshot.account_equity
                    ),
                    "quote_fresh": snapshot.quote_fresh,
                }
            config = (
                self.config.crypto_risk or self.config.risk
                if asset_class is AssetClass.CRYPTO
                else self.config.risk
            )
            drawdown = (
                (
                    snapshot.account_high_water_mark
                    - snapshot.account_equity
                )
                / snapshot.account_high_water_mark
                * Decimal(100)
                if (
                    snapshot.account_complete
                    and snapshot.account_high_water_mark > 0
                )
                else Decimal("Infinity")
            )
            if (
                not snapshot.account_complete
                or not snapshot.broker_reconciled
                or not drawdown.is_finite()
                or drawdown
                >= Decimal(
                    str(config.max_account_drawdown_pct)
                )
            ):
                raise RequiredDependencyUnavailable
            return {
                **base,
                "account_equity": str(snapshot.account_equity),
                "account_high_water_mark": str(
                    snapshot.account_high_water_mark
                ),
                "drawdown_pct": str(drawdown),
            }

        if scope.kind.value == "liquidity":
            symbol = scope.target
            asset_class = self._asset_class(symbol)
            config = (
                self.config.crypto_risk or self.config.risk
                if asset_class is AssetClass.CRYPTO
                else self.config.risk
            )
            if symbol not in {
                item.upper() for item in config.ticker_allowlist
            }:
                raise RequiredDependencyUnavailable
            snapshot = self._reset_snapshot(
                [symbol],
                asset_class,
            )
            spread = snapshot.spread_pct_by_ticker.get(symbol)
            if (
                not snapshot.quote_fresh
                or not snapshot.broker_reconciled
                or spread is None
                or not spread.is_finite()
                or spread
                > Decimal(str(config.max_spread_pct))
            ):
                raise RequiredDependencyUnavailable
            return {
                "captured_at": snapshot.as_of.isoformat(),
                "symbol": symbol,
                "quote_fresh": True,
                "spread_pct": str(spread),
                "broker_reconciled": True,
            }

        order_report = self.sync_open_orders(
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        position_report = self.reconcile_positions(
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        clean_reconciliation = (
            order_report["failed"] == 0
            and order_report["plan_order_cancel_failures"] == 0
            and not order_report["broker_drift"]
            and position_report["reconciled"] is True
            and not position_report["drift"]
        )
        if not clean_reconciliation:
            raise RequiredDependencyUnavailable
        if scope.kind.value == "broker_drift":
            return {
                "captured_at": utcnow().isoformat(),
                "order_reconciliation": "clean",
                "position_reconciliation": "clean",
            }

        try:
            remote_open_orders = self.broker.get_open_orders()
        except Exception:
            raise RequiredDependencyUnavailable from None
        safety = read_persisted_safety_truth(
            self.session_factory
        )
        unsafe = safety.unsafe_local_state.as_dict()
        unsafe_ids = [
            value
            for key, value in unsafe.items()
            if key != "enumeration" and isinstance(value, list)
        ]
        other_active = [
            breaker.scope
            for breaker in safety.active_breakers
            if breaker.scope != scope.key
        ]
        if (
            remote_open_orders
            or not safety.complete
            or safety.unsafe_local_state.enumeration
            != "confirmed"
            or any(unsafe_ids)
            or other_active
        ):
            raise RequiredDependencyUnavailable
        return {
            "captured_at": safety.observed_at.isoformat(),
            "remote_open_orders": 0,
            "local_enumeration": "confirmed",
            "other_active_breakers": [],
            "order_reconciliation": "clean",
            "position_reconciliation": "clean",
        }

    def reset_killswitch(
        self,
        scope: BreakerScope | AssetClass | str,
        *,
        actor: str,
        reason: str,
        expected_generation: int,
        request_id: str,
    ) -> dict[str, Any]:
        parsed_scope = (
            scope
            if isinstance(scope, BreakerScope)
            else (
                BreakerScope.loss(scope)
                if isinstance(scope, AssetClass)
                else BreakerScope.parse(scope)
            )
        )
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        try:
            prior_health = self._collect_scope_reset_health(
                parsed_scope,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
        except RequiredDependencyUnavailable:
            self._audit_dependency_failure(
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="circuit_breaker.reset",
                target_type="circuit_breaker",
                target_id=parsed_scope.key,
                detail={
                    "scope": parsed_scope.key,
                    "expected_generation": expected_generation,
                    "stage": "health_collection",
                },
            )
            raise RequiredDependencyUnavailable from None
        state = self.breakers.reset(
            parsed_scope,
            actor=actor,
            reason=reason,
            prior_health=prior_health,
            expected_generation=expected_generation,
            request_id=request_id,
        )
        return {
            "killswitch": "reset",
            "scope": parsed_scope.key,
            "kind": parsed_scope.kind.value,
            "target": parsed_scope.target,
            "asset_class": (
                parsed_scope.target
                if parsed_scope.kind.value
                in {"loss", "drawdown", "data"}
                else None
            ),
            "tripped": state.tripped,
            "generation": state.generation,
        }

    @staticmethod
    def serialize_reconciliation_report(report) -> dict[str, Any]:
        """Serialize the new report while retaining legacy monitor keys."""
        failed = len(report.unresolved_unknown) + len(report.broker_drift)
        return {
            "resolved_unknown": report.resolved_unknown,
            "unresolved_unknown": list(report.unresolved_unknown),
            "synced_orders": report.synced_orders,
            "inserted_fills": report.inserted_fills,
            "broker_drift": list(report.broker_drift),
            "synced": report.synced_orders,
            "newly_filled": report.inserted_fills,
            "failed": failed,
            "fills_repaired": 0,
        }

    def require_startup_reconciliation(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> int:
        """Invalidate prior process-start proof before a runtime can serve."""
        return self.startup_reconciliation.require(
            actor=actor,
            reason=reason,
            request_id=request_id,
        )

    def reconcile_startup_epoch(
        self,
        generation: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Reconcile all broker truth and atomically unlock the newest epoch."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        with self.submission_barrier.hold_writer():
            target_generation = (
                self.startup_reconciliation.current_generation()
            )
            if target_generation <= 0:
                raise StartupReconciliationFailed(
                    "startup reconciliation generation is missing"
                )
            if self.startup_reconciliation.is_current(
                target_generation
            ):
                return {
                    "ready": True,
                    "generation": target_generation,
                    "superseded_generation": (
                        generation
                        if generation != target_generation
                        else None
                    ),
                }
            evidence: dict[str, Any] = {
                "requested_generation": generation,
                "generation": target_generation,
            }
            try:
                order_sync = self.sync_open_orders(
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                positions = self.reconcile_positions(
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
            except RequiredDependencyUnavailable:
                evidence["dependency"] = "unavailable"
                failure_code = "broker_reconciliation_dependency_unavailable"
                self.startup_reconciliation.fail(
                    target_generation,
                    failure_code,
                    evidence=evidence,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                raise StartupReconciliationFailed(failure_code) from None
            except Exception:
                self.startup_reconciliation.fail(
                    target_generation,
                    "broker_reconciliation_failed",
                    evidence=evidence,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                raise StartupReconciliationFailed(
                    "broker_reconciliation_failed"
                ) from None

            evidence.update(
                {
                    "order_sync": order_sync,
                    "position_reconciliation": positions,
                }
            )
            faults = list(order_sync.get("broker_drift", []))
            unresolved = order_sync.get("unresolved_unknown", [])
            if unresolved:
                faults.append(
                    f"unresolved acceptance orders {unresolved}"
                )
            if not positions.get("reconciled", False):
                faults.append(
                    "position reconciliation drift "
                    f"{positions.get('drift', {})}"
                )
            if order_sync.get("failed", 0) and not faults:
                faults.append("broker order reconciliation failed")
            remaining_plan_cancels = order_sync.get(
                "remaining_plan_cancel_intents",
                [],
            )
            if remaining_plan_cancels:
                faults.append(
                    "unresolved plan cancellation intents "
                    f"{remaining_plan_cancels}"
                )
            if faults:
                self.startup_reconciliation.fail(
                    target_generation,
                    "broker_reconciliation_failed",
                    evidence=evidence,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                raise StartupReconciliationFailed(
                    "broker_reconciliation_failed"
                )

            if not self.startup_reconciliation.complete(
                target_generation,
                evidence=evidence,
                actor=actor,
                reason=reason,
                request_id=request_id,
            ):
                raise StartupReconciliationFailed(
                    "startup reconciliation generation changed before commit"
                )
            return {
                "ready": True,
                "generation": target_generation,
                "superseded_generation": (
                    generation
                    if generation != target_generation
                    else None
                ),
                "order_sync": order_sync,
                "position_reconciliation": positions,
            }

    def sync_open_orders(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Compatibility facade for callers that still consume dictionary reports."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        try:
            result = self.serialize_reconciliation_report(
                self.reconciliation.reconcile(
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
            )
        except RequiredDependencyUnavailable:
            self._audit_dependency_failure(
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="orders.sync",
                target_type="broker_orders",
                target_id="all",
                detail={"stage": "broker_order_sync"},
            )
            raise RequiredDependencyUnavailable from None
        before_cancellation = (
            self.rule_repository.refresh_fill_activated_rules(
                now=utcnow(),
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
        )
        plan_cancellation = (
            self._cancel_plan_orders_after_exit_fill(
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
        )
        after_cancellation = self.rule_repository.refresh_fill_activated_rules(
            now=utcnow(),
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        remaining_plan_cancel_intents = (
            self.rule_repository.plan_cancellation_intent_order_ids()
        )
        plan_cancel_failures = max(
            plan_cancellation["failed"],
            len(remaining_plan_cancel_intents),
        )
        result["exit_groups_activated"] = (
            before_cancellation.groups_activated
            + after_cancellation.groups_activated
        )
        result["exit_rules_activated"] = (
            before_cancellation.rules_activated
            + after_cancellation.rules_activated
        )
        result["exit_rules_resized"] = (
            before_cancellation.rules_resized
            + after_cancellation.rules_resized
        )
        result["plan_rules_settled"] = (
            before_cancellation.rules_settled
            + after_cancellation.rules_settled
        )
        result["plans_completed"] = (
            before_cancellation.plans_completed
            + after_cancellation.plans_completed
        )
        result["plan_orders_canceled"] = plan_cancellation[
            "canceled"
        ]
        result["plan_order_cancel_failures"] = plan_cancel_failures
        result["remaining_plan_cancel_intents"] = (
            remaining_plan_cancel_intents
        )
        result["failed"] += plan_cancel_failures
        if plan_cancel_failures:
            self.breakers.trip(
                BreakerScope.broker_drift(),
                (
                    "plan cancellation remains unresolved for orders "
                    + ",".join(
                        str(order_id)
                        for order_id in remaining_plan_cancel_intents
                    )
                ),
                actor,
                request_id=request_id,
                audit_reason=reason,
            )
        with self.session_factory() as session:
            _persist_audit(
                    session,
                    actor=actor,
                    action="orders.sync",
                    target_type="broker_orders",
                    target_id="all",
                    request_id=request_id,
                    reason=reason,
                    result_code=(
                        "reconciled"
                        if result["failed"] == 0
                        else "reconciliation_required"
                    ),
                    detail_json=json.dumps(
                        {
                            "resolved_unknown": result["resolved_unknown"],
                            "unresolved_unknown": result[
                                "unresolved_unknown"
                            ],
                            "synced_orders": result["synced_orders"],
                            "inserted_fills": result["inserted_fills"],
                            "broker_drift": result["broker_drift"],
                        },
                        sort_keys=True,
                    ),
            )
            session.commit()
        return result

    def _validate_plan_exit_submission(
        self,
        order_id: int,
        request: OrderRequest,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> str | None:
        try:
            positions = {
                position.ticker.upper(): position
                for position in self.broker.get_positions()
            }
            position = positions.get(request.ticker.upper())
            broker_position_qty = (
                position.qty
                if position is not None
                else Decimal(0)
            )
        except Exception:
            error = "plan allocation broker truth is unavailable"
            self.breakers.trip(
                BreakerScope.broker_drift(),
                error,
                actor,
                request_id=request_id,
                audit_reason=reason,
            )
            return error
        error = self.rule_repository.validate_plan_exit_submission(
            order_id,
            request.qty,
            broker_position_qty,
        )
        if error in {
            "plan has negative trusted residual quantity",
            "plan allocation cannot be proven from reconciled fill truth",
            "plan allocation exceeds reconciled broker position",
        }:
            self.breakers.trip(
                BreakerScope.broker_drift(),
                error,
                actor,
                request_id=request_id,
                audit_reason=reason,
            )
        if error is not None:
            return error
        if self.rule_repository.is_plan_exit_order(order_id):
            return None
        allocated, allocation_exact = (
            self.rule_repository.plan_allocation_truth(
                request.ticker,
                request.side.value,
            )
        )
        if not allocation_exact:
            error = (
                "plan allocation cannot be proven from "
                "reconciled fill truth"
            )
            self.breakers.trip(
                BreakerScope.broker_drift(),
                error,
                actor,
                request_id=request_id,
                audit_reason=reason,
            )
            return error
        if allocated <= 0:
            return None
        if request.qty is not None:
            requested_qty = request.qty
        else:
            try:
                quote = self.broker.get_quote(request.ticker)
                if not quote.is_valid:
                    raise ValueError
                requested_qty = request.notional / quote.last
            except Exception:
                error = "plan allocation broker truth is unavailable"
                self.breakers.trip(
                    BreakerScope.broker_drift(),
                    error,
                    actor,
                    request_id=request_id,
                    audit_reason=reason,
                )
                return error
        reducible = (
            max(broker_position_qty, Decimal(0))
            if request.side is OrderSide.SELL
            else max(-broker_position_qty, Decimal(0))
        )
        if reducible > 0 and requested_qty > max(
            reducible - allocated,
            Decimal(0),
        ):
            return "order would consume plan-allocated position"
        return None

    def _reconcile_immediate_submission_fill(
        self,
        order_id: int,
        reported_filled_qty: Decimal,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> bool:
        """Require exact fill truth and activated plan exits before returning."""
        failure_reason = (
            f"immediate fill for order {order_id} was not exactly "
            "reconciled before submission returned"
        )
        try:
            report = self.sync_open_orders(
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
            with self.session_factory() as session:
                order = session.get(Order, order_id)
                exact_qty = session.scalar(
                    select(func.coalesce(func.sum(Fill.qty), 0)).where(
                        Fill.order_id == order_id,
                        Fill.reconciliation_state == "trusted",
                        Fill.broker_fill_id.is_not(None),
                        Fill.broker_fill_id != "",
                    )
                )
                proposal = session.scalar(
                    select(Proposal).where(
                        Proposal.order_id == order_id
                    )
                )
                source_rule = (
                    session.get(Rule, proposal.source_rule_id)
                    if proposal is not None
                    and proposal.source_rule_id is not None
                    else None
                )
                protection_ready = True
                if (
                    source_rule is not None
                    and source_rule.plan_id is not None
                    and source_rule.kind == "entry"
                ):
                    truth = (
                        self.rule_repository.plan_execution_truth(
                            source_rule.plan_id
                        )
                    )
                    downside_rules = list(
                        session.scalars(
                            select(Rule)
                            .join(
                                RuleGroup,
                                RuleGroup.id == Rule.group_id,
                            )
                            .where(
                                Rule.plan_id == source_rule.plan_id,
                                Rule.kind.in_(
                                    {"stop", "trailing"}
                                ),
                                Rule.state == "active",
                                RuleGroup.state == "active",
                                RuleGroup.reconciliation_required.is_(
                                    False
                                ),
                            )
                        ).all()
                    )
                    protected_qty = Decimal(0)
                    for downside_rule in downside_rules:
                        try:
                            action = json.loads(
                                downside_rule.action_json
                            )
                            qty = Decimal(str(action.get("qty")))
                        except (
                            json.JSONDecodeError,
                            InvalidOperation,
                            TypeError,
                            ValueError,
                        ):
                            qty = Decimal(0)
                        protected_qty = max(protected_qty, qty)
                    protection_ready = bool(
                        truth.residual_qty > 0
                        and not truth.reconciliation_required
                        and not truth.unresolved_order_ids
                        and protected_qty >= truth.residual_qty
                    )
                confirmed = bool(
                    order is not None
                    and order.acceptance_state
                    != FILL_RECONCILIATION_REQUIRED
                    and Decimal(str(exact_qty))
                    == reported_filled_qty
                    and report["failed"] == 0
                    and report["plan_order_cancel_failures"] == 0
                    and protection_ready
                )
        except Exception:
            confirmed = False
        if confirmed:
            return True
        self.breakers.trip(
            BreakerScope.broker_drift(),
            failure_reason,
            actor,
            request_id=request_id,
            audit_reason=reason,
        )
        self.breakers.trip(
            BreakerScope.operator_global(),
            failure_reason,
            actor,
            request_id=request_id,
            audit_reason=reason,
        )
        return False

    def cancel_live_order(
        self,
        order_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Cancel a live (SUBMITTED / PARTIALLY_FILLED) order at the broker + DB."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        with self.submission_barrier.hold_writer():
            result = self._cancel_live_order_under_writer(
                order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
            with self.session_factory() as session:
                _persist_audit(
                        session,
                        actor=actor,
                        action="order.cancel",
                        target_type="order",
                        target_id=str(order_id),
                        request_id=request_id,
                        reason=reason,
                        result_code=(
                            "canceled"
                            if "error" not in result
                            else "cancel_failed"
                        ),
                        detail_json=json.dumps(
                            {
                                "status": result.get("status"),
                                "has_error": "error" in result,
                            },
                            sort_keys=True,
                        ),
                )
                session.commit()
            return result

    @staticmethod
    def _record_plan_cancel_state(
        session: Session,
        order: Order,
        state: str,
        *,
        actor: str,
        reason: str,
        request_id: str,
        now,
    ) -> None:
        if state not in {
            PLAN_CANCEL_REQUESTED,
            PLAN_CANCEL_INDETERMINATE,
            PLAN_CANCEL_SETTLED,
        }:
            raise ValueError("invalid durable plan cancellation state")
        if order.plan_cancel_state == state:
            return
        order.plan_cancel_state = state
        order.updated_at = now
        order.version += 1
        _persist_audit(
                session,
                actor=actor,
                action="order.plan_cancel_intent",
                target_type="order",
                target_id=str(order.id),
                request_id=request_id,
                reason=reason,
                result_code=state,
                created_at=now,
        )

    @serialized_writer
    def quiesce_trade_plan_orders(
        self,
        plan_id: int,
        *,
        entry_only: bool = False,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, int]:
        """Make every plan-linked order terminal before rule cancellation."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        canceled = 0
        failed = 0
        order_ids = (
            self.rule_repository.plan_entry_nonterminal_order_ids(
                plan_id
            )
            if entry_only
            else self.rule_repository.plan_nonterminal_order_ids(
                plan_id
            )
        )
        for order_id in order_ids:
            with self.session_factory() as session:
                order = session.get(Order, order_id)
                if order is None:
                    failed += 1
                    continue
                current = OrderStatus(order.status)
                if current in {
                    OrderStatus.PROPOSED,
                    OrderStatus.APPROVAL_RECORDED,
                }:
                    target = (
                        OrderStatus.CANCELED
                        if current is OrderStatus.PROPOSED
                        else OrderStatus.REJECTED
                    )
                    OrderStateMachine.transition(order, target)
                    order.last_error_code = "plan_cancel"
                    self._record_plan_cancel_state(
                        session,
                        order,
                        PLAN_CANCEL_SETTLED,
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                        now=utcnow(),
                    )
                    _persist_audit(
                            session,
                            actor=actor,
                            action="order.plan_cancel",
                            target_type="order",
                            target_id=str(order_id),
                            request_id=request_id,
                            reason=reason,
                            result_code=target.value,
                    )
                    session.commit()
                    canceled += 1
                    continue
                if current not in {
                    OrderStatus.SUBMITTED,
                    OrderStatus.PARTIALLY_FILLED,
                }:
                    if order.plan_cancel_state not in {
                        PLAN_CANCEL_REQUESTED,
                        PLAN_CANCEL_INDETERMINATE,
                    }:
                        self._record_plan_cancel_state(
                            session,
                            order,
                            PLAN_CANCEL_REQUESTED,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            now=utcnow(),
                        )
                    trip_in_session(
                        session,
                        BreakerScope.broker_drift(),
                        (
                            f"plan {plan_id} cancellation found "
                            f"indeterminate order {order_id} in "
                            f"state {current.value}"
                        ),
                        actor,
                        request_id=request_id,
                        audit_reason=reason,
                    )
                    session.commit()
                    failed += 1
                    continue
                order.last_error_code = "plan_cancel"
                if order.plan_cancel_state not in {
                    PLAN_CANCEL_REQUESTED,
                    PLAN_CANCEL_INDETERMINATE,
                }:
                    self._record_plan_cancel_state(
                        session,
                        order,
                        PLAN_CANCEL_REQUESTED,
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                        now=utcnow(),
                    )
                _persist_audit(
                        session,
                        actor=actor,
                        action="order.plan_broker_cancel",
                        target_type="order",
                        target_id=str(order_id),
                        request_id=request_id,
                        reason=reason,
                        result_code="requested",
                )
                session.commit()
            outcome = self._cancel_live_order_under_writer(
                order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
                settle_plan_lifecycle=False,
            )
            if "error" in outcome:
                self.breakers.trip(
                    BreakerScope.broker_drift(),
                    (
                        f"plan {plan_id} cancellation remains "
                        f"unresolved for order {order_id}"
                    ),
                    actor,
                    request_id=request_id,
                    audit_reason=reason,
                )
                failed += 1
            else:
                canceled += 1
        remaining = len(
            (
                self.rule_repository.plan_entry_nonterminal_order_ids(
                    plan_id
                )
                if entry_only
                else self.rule_repository.plan_nonterminal_order_ids(
                    plan_id
                )
            )
        )
        return {
            "canceled": canceled,
            # Every still-live row has already contributed to ``failed`` when
            # its local or broker cancellation could not be confirmed. Keep
            # the count cardinal rather than double-counting those rows.
            "failed": max(failed, remaining),
            "remaining": remaining,
        }

    @serialized_writer
    def _cancel_plan_orders_after_exit_fill(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, int]:
        """Cancel broker-live entries/siblings before a filled exit closes a plan."""
        canceled = 0
        failed = 0
        seen: set[int] = set()
        while True:
            candidates = [
                order_id
                for order_id in sorted(
                    set(
                        self.rule_repository
                        .plan_order_ids_requiring_broker_cancel()
                    )
                    | set(
                        self.rule_repository
                        .plan_cancellation_intent_order_ids()
                    )
                )
                if order_id not in seen
            ]
            if not candidates:
                break
            for order_id in candidates:
                seen.add(order_id)
                with self.session_factory() as session:
                    order = session.get(Order, order_id)
                    if order is None:
                        failed += 1
                        continue
                    current = OrderStatus(order.status)
                    if (
                        current in TERMINAL_STATES
                        and order.acceptance_state
                        != FILL_RECONCILIATION_REQUIRED
                    ):
                        self._record_plan_cancel_state(
                            session,
                            order,
                            PLAN_CANCEL_SETTLED,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            now=utcnow(),
                        )
                        session.commit()
                        canceled += 1
                        continue
                    if current not in {
                        OrderStatus.SUBMITTED,
                        OrderStatus.PARTIALLY_FILLED,
                    }:
                        if order.plan_cancel_state not in {
                            PLAN_CANCEL_REQUESTED,
                            PLAN_CANCEL_INDETERMINATE,
                        }:
                            self._record_plan_cancel_state(
                                session,
                                order,
                                PLAN_CANCEL_REQUESTED,
                                actor=actor,
                                reason=reason,
                                request_id=request_id,
                                now=utcnow(),
                            )
                        trip_in_session(
                            session,
                            BreakerScope.broker_drift(),
                            (
                                "plan exit requires cancellation of "
                                f"indeterminate order {order_id} in "
                                f"state {current.value}"
                            ),
                            actor,
                            request_id=request_id,
                            audit_reason=reason,
                        )
                        session.commit()
                        failed += 1
                        continue
                    if order.last_error_code not in {
                        "plan_cancel",
                        "plan_exit_entry_cancel",
                    }:
                        order.last_error_code = (
                            "plan_exit_entry_cancel"
                        )
                    if order.plan_cancel_state not in {
                        PLAN_CANCEL_REQUESTED,
                        PLAN_CANCEL_INDETERMINATE,
                    }:
                        self._record_plan_cancel_state(
                            session,
                            order,
                            PLAN_CANCEL_REQUESTED,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            now=utcnow(),
                        )
                    _persist_audit(
                            session,
                            actor=actor,
                            action="order.plan_broker_cancel",
                            target_type="order",
                            target_id=str(order_id),
                            request_id=request_id,
                            reason=reason,
                            result_code="requested",
                    )
                    session.commit()
                outcome = self._cancel_live_order_under_writer(
                    order_id,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    settle_plan_lifecycle=False,
                )
                if "error" in outcome:
                    self.breakers.trip(
                        BreakerScope.broker_drift(),
                        (
                            "plan exit cancellation remains "
                            f"unresolved for order {order_id}"
                        ),
                        actor,
                        request_id=request_id,
                        audit_reason=reason,
                    )
                    failed += 1
                else:
                    canceled += 1
        return {"canceled": canceled, "failed": failed}

    def _cancel_live_order_under_writer(
        self,
        order_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
        settle_plan_lifecycle: bool = True,
    ) -> dict[str, Any]:
        with self.session_factory() as s:
            order = s.get(Order, order_id)
            if order is None:
                return {"error": "not found"}
            if OrderStatus(order.status) not in (
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIALLY_FILLED,
            ):
                return {"order_id": order_id, "status": order.status,
                        "error": "order not cancelable in this state"}
            broker_order_id = order.broker_order_id
            local_status = order.status
            if not broker_order_id:
                return {
                    "order_id": order_id,
                    "status": order.status,
                    "error": "live order has no broker order id",
                }

        # The process writer is already held, but this read session is closed
        # before broker I/O. Reconciliation later opens its own short write
        # transactions and commits exact fill/latch truth before writer release.
        try:
            broker_result = self.broker.cancel_order(broker_order_id)
        except Exception:
            try:
                broker_result = self.broker.get_order_status(broker_order_id)
            except Exception:
                fault_reason = (
                    "indeterminate broker cancellation for order "
                    f"{order_id}"
                )
                now = utcnow()
                # Fail closed first in an independent durable transaction. A
                # later latch/audit failure must not reopen submissions.
                self.breakers.trip(
                    BreakerScope.broker_drift(),
                    fault_reason,
                    actor,
                    now=now,
                    request_id=request_id,
                    audit_reason=reason,
                )

                # The latch and its exact provenance are one transaction. If
                # either write fails, neither may be visible.
                with self.session_factory() as session:
                    order = session.get(Order, order_id)
                    if order is None:
                        raise RuntimeError(
                            "order disappeared during cancellation latch"
                        )
                    order.acceptance_state = (
                        FILL_RECONCILIATION_REQUIRED
                    )
                    order.last_error_code = "indeterminate_cancel"
                    if order.plan_cancel_state in {
                        PLAN_CANCEL_REQUESTED,
                        PLAN_CANCEL_INDETERMINATE,
                    }:
                        self._record_plan_cancel_state(
                            session,
                            order,
                            PLAN_CANCEL_INDETERMINATE,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            now=now,
                        )
                    else:
                        order.updated_at = now
                        order.version += 1
                    _persist_audit(
                            session,
                            actor=actor,
                            action="order.cancel_latch",
                            target_type="order",
                            target_id=str(order_id),
                            request_id=request_id,
                            reason=reason,
                            result_code="indeterminate_cancel",
                            detail_json=json.dumps(
                                {
                                    "acceptance_state": (
                                        FILL_RECONCILIATION_REQUIRED
                                    ),
                                    "error_code": "indeterminate_cancel",
                                },
                                sort_keys=True,
                            ),
                            created_at=now,
                    )
                    session.commit()
                return {
                    "order_id": order_id,
                    "status": local_status,
                    "error": "broker cancellation could not be confirmed",
                }
        broker_status = broker_result.status
        if settle_plan_lifecycle:
            sync = self.sync_open_orders(
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
        else:
            sync = self.serialize_reconciliation_report(
                self.reconciliation.reconcile(
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
            )
        current = self.get_order_status(order_id)
        with self.session_factory() as session:
            current_row = session.get(Order, order_id)
            exact_fill_truth_confirmed = bool(
                current_row is not None
                and current_row.acceptance_state
                != FILL_RECONCILIATION_REQUIRED
            )
            terminal_exact = bool(
                current_row is not None
                and OrderStatus(current_row.status)
                in TERMINAL_STATES
                and exact_fill_truth_confirmed
            )
            plan_cancel_settled = bool(
                terminal_exact
                and current_row is not None
                and current_row.plan_cancel_state
                in {
                    PLAN_CANCEL_REQUESTED,
                    PLAN_CANCEL_INDETERMINATE,
                }
            )
            if plan_cancel_settled:
                self._record_plan_cancel_state(
                    session,
                    current_row,
                    PLAN_CANCEL_SETTLED,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    now=utcnow(),
                )
                session.commit()
        if (
            broker_status is OrderStatus.CANCELED
            and current is not None
            and current["status"] == OrderStatus.CANCELED.value
            and exact_fill_truth_confirmed
        ):
            return {"order_id": order_id, "status": current["status"]}
        if (
            plan_cancel_settled
            and not settle_plan_lifecycle
            and current is not None
        ):
            return {"order_id": order_id, "status": current["status"]}
        return {
            "order_id": order_id,
            "status": current["status"] if current else None,
            "error": (
                "broker cancellation lacks exact fill confirmation"
                if not exact_fill_truth_confirmed
                else (
                    "broker order was not canceled; reported "
                    f"{broker_status.value}"
                )
            ),
            "sync": sync,
        }

    def replace_order(
        self,
        order_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
        **new_order,
    ) -> dict[str, Any]:
        """Cancel/replace: cancel the live order, then propose a replacement."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        cancel = self.cancel_live_order(
            order_id,
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        if "error" in cancel:
            return {"canceled": cancel, "replacement": None}
        replacement = self.propose_order(
            **new_order,
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        return {"canceled": cancel, "replacement": replacement}

    def reconcile_positions(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Compare positions and durably trip drift in one writer interval."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        with self.submission_barrier.hold_writer():
            # Broker I/O is ordered by the process barrier but occurs before
            # any SQLite transaction is opened.
            try:
                positions = self.broker.get_positions()
            except Exception:
                self._audit_dependency_failure(
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    action="positions.reconcile",
                    target_type="portfolio",
                    target_id="all",
                    detail={"stage": "broker_positions"},
                )
                raise RequiredDependencyUnavailable from None
            broker_pos = {
                position.ticker.upper(): position.qty
                for position in positions
            }
            local: dict[str, Decimal] = {}
            with self.session_factory() as session:
                for fill in session.execute(select(Fill)).scalars().all():
                    if not fill_has_trusted_identity(fill):
                        continue
                    delta = fill.qty if fill.side == "buy" else -fill.qty
                    ticker = fill.ticker.upper()
                    local[ticker] = local.get(ticker, Decimal(0)) + delta
                drift = {}
                for ticker in set(broker_pos) | set(local):
                    broker_qty = Decimal(str(broker_pos.get(ticker, 0)))
                    local_qty = local.get(ticker, Decimal(0))
                    if broker_qty != local_qty:
                        drift[ticker] = {
                            "broker": str(broker_qty),
                            "local": str(local_qty),
                        }
                if drift:
                    drift_detail = json.dumps(drift, sort_keys=True)
                    _persist_risk(
                            session,
                            event_type="reconciliation",
                            reason=drift_detail,
                    )
                    trip_in_session(
                        session,
                        BreakerScope.broker_drift(),
                        f"position reconciliation drift: {drift_detail}",
                        actor,
                        request_id=request_id,
                        audit_reason=reason,
                    )
                _persist_audit(
                        session,
                        actor=actor,
                        action="positions.reconcile",
                        target_type="portfolio",
                        target_id="all",
                        request_id=request_id,
                        reason=reason,
                        result_code=(
                            "reconciled" if not drift else "drift_detected"
                        ),
                        detail_json=json.dumps(
                            {"drift": drift},
                            sort_keys=True,
                        ),
                )
                session.commit()
            return {"reconciled": not drift, "drift": drift}

    def enforce_daily_loss_limits(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, bool]:
        """Trip each asset class's kill switch if its realized daily loss breached."""
        actor, reason, request_id = _require_mutation_context(
            actor, reason, request_id
        )
        with self.submission_barrier.hold_writer():
            with self.session_factory() as session:
                realized = {
                    asset_class: self._realized_pnl_today(
                        session,
                        asset_class,
                    )
                    for asset_class in (
                        AssetClass.EQUITY,
                        AssetClass.CRYPTO,
                    )
                }
                result: dict[str, bool] = {}
                for asset_class, pnl in realized.items():
                    scope = BreakerScope.loss(asset_class)
                    limit = abs(self._loss_limit_for(asset_class))
                    if pnl <= -limit:
                        trip_in_session(
                            session,
                            scope,
                            (
                                f"daily realized loss {pnl} breached limit "
                                f"-{limit}"
                            ),
                            actor,
                            request_id=request_id,
                            audit_reason=reason,
                        )
                        result[asset_class.value] = True
                    else:
                        result[asset_class.value] = bool(
                            session.scalar(
                                select(CircuitBreakerState.tripped).where(
                                    CircuitBreakerState.scope_key == scope.key
                                )
                            )
                        )
                session.commit()
                return result

    def get_pending(self) -> list[dict[str, Any]]:
        with self.session_factory() as s:
            rows = (
                s.execute(
                    select(Order).where(Order.status == OrderStatus.PROPOSED.value)
                )
                .scalars()
                .all()
            )
            out = []
            for o in rows:
                d = self._order_dict(o)
                if o.proposal is not None:
                    d["expires_at"] = o.proposal.expires_at.isoformat()
                    d["expired"] = o.proposal.is_expired()
                out.append(d)
            return out

    def get_approval_confirmation(
        self,
        order_id: int,
    ) -> dict[str, Any]:
        """Return read-only server proof for a human approval decision."""
        with self.session_factory() as session:
            order = session.get(Order, order_id)
            if order is None:
                return {"error": "not_found"}
            if order.status != OrderStatus.PROPOSED.value:
                return {"error": "conflict"}
            proposal = order.proposal
            if proposal is not None and proposal.is_expired():
                return {"error": "conflict"}
            order_request = OrderRequest(
                ticker=order.ticker,
                side=OrderSide(order.side),
                order_type=OrderType(order.order_type),
                idempotency_key=order.idempotency_key,
                qty=order.qty,
                notional=order.notional,
                limit_price=order.limit_price,
            )
            order_payload = {
                "order_id": order.id,
                "symbol": order.ticker,
                "side": order.side,
                "order_type": order.order_type,
                "quantity": (
                    None if order.qty is None else str(order.qty)
                ),
                "notional": (
                    None
                    if order.notional is None
                    else str(order.notional)
                ),
                "limit_price": (
                    None
                    if order.limit_price is None
                    else str(order.limit_price)
                ),
            }
            expires_at = (
                proposal.expires_at.isoformat()
                if proposal is not None
                else None
            )

        missing_proof: list[str] = []
        if self.config.trading.broker is not BrokerKind.ALPACA:
            missing_proof.append("broker")
        if self.config.trading.mode is not TradingMode.PAPER:
            missing_proof.append("mode")
        if expires_at is None:
            missing_proof.append("expires_at")

        snapshot = self.snapshot_service.assemble_for_confirmation(
            order_request.ticker,
            exclude_order_id=order_id,
        )
        if snapshot.pending_exposure_complete is not True:
            missing_proof.append("pending_exposure_complete")
        if snapshot.broker_reconciled is not True:
            missing_proof.append("broker_reconciled")

        symbol = order_request.ticker.upper()
        quote = snapshot.quotes.get(symbol)
        current_position = snapshot.positions.get(symbol)
        current_quantity = (
            current_position.qty
            if current_position is not None
            else Decimal(0)
        )
        current_signed_notional: Decimal | None = None
        resulting_signed_notional: Decimal | None = None
        if (
            quote is None
            or not quote.is_valid
            or snapshot.quote_fresh is not True
        ):
            missing_proof.append("quote")
        else:
            current_signed_notional = current_quantity * quote.last
            risk_base = snapshot.effective_signed_value(symbol)
            if risk_base is not None:
                order_notional = order_request.risk_notional(quote)
                signed_order_notional = (
                    order_notional
                    if order_request.side is OrderSide.BUY
                    else -order_notional
                )
                resulting_signed_notional = (
                    risk_base + signed_order_notional
                )
        if (
            current_signed_notional is None
            or not current_signed_notional.is_finite()
            or resulting_signed_notional is None
            or not resulting_signed_notional.is_finite()
        ):
            missing_proof.append("exposure")

        return {
            "complete": not missing_proof,
            "missing_proof": sorted(set(missing_proof)),
            "broker": (
                "Alpaca"
                if self.config.trading.broker is BrokerKind.ALPACA
                else self.config.trading.broker.value.title()
            ),
            "mode": self.config.trading.mode.value,
            "order": order_payload,
            "expires_at": expires_at,
            "exposure": {
                "currency": "USD",
                "current_position_quantity": str(current_quantity),
                "current_signed_notional": (
                    None
                    if current_signed_notional is None
                    else str(current_signed_notional)
                ),
                "resulting_signed_notional": (
                    None
                    if resulting_signed_notional is None
                    else str(resulting_signed_notional)
                ),
                "as_of": snapshot.as_of.isoformat(),
            },
        }

    def get_positions(self) -> list[dict[str, Any]]:
        try:
            positions = self._read_valid_broker_positions()
        except Exception:
            raise RequiredDependencyUnavailable from None
        return [
            {
                "ticker": p.ticker,
                "qty": str(p.qty),
                "avg_entry_price": str(p.avg_entry_price),
                "current_price": str(p.current_price),
                "market_value": str(p.market_value),
            }
            for p in positions
        ]

    def available_reduce_qty(
        self,
        ticker: str,
        side: str,
        *,
        reference_price: Decimal | None = None,
    ) -> Decimal:
        """Quantity an exit can safely reserve without reversing the position.

        Existing same-side live orders reserve quantity first, so a stop and a
        still-open target cannot both fill past flat.
        """
        symbol = ticker.upper()
        positions = {p.ticker.upper(): p for p in self.broker.get_positions()}
        position = positions.get(symbol)
        held_qty = position.qty if position is not None else Decimal(0)
        if side == OrderSide.SELL.value:
            reducible = max(held_qty, Decimal(0))
        elif side == OrderSide.BUY.value:
            reducible = max(-held_qty, Decimal(0))
        else:
            return Decimal(0)
        if reducible == 0:
            return Decimal(0)

        with self.session_factory() as s:
            live = s.execute(
                select(Order).where(
                    Order.ticker == symbol,
                    Order.side == side,
                    Order.status.in_(_EXIT_RESERVATION_STATUSES),
                )
            ).scalars().all()
            reserved = Decimal(0)
            for order in live:
                filled_qty = sum((fill.qty for fill in order.fills), Decimal(0))
                if order.qty is not None:
                    reserved += max(order.qty - filled_qty, Decimal(0))
                elif order.notional is not None:
                    price = reference_price
                    if price is None:
                        price = self.broker.get_quote(symbol).last
                    spent = sum(
                        (fill.qty * fill.price for fill in order.fills), Decimal(0)
                    )
                    remaining = max(order.notional - spent, Decimal(0))
                    if price > 0:
                        reserved += remaining / price
        return max(reducible - reserved, Decimal(0))

    def get_log(self, limit: int = 100) -> dict[str, Any]:
        with self.session_factory() as s:
            store = sensitive_store(s, self.session_factory)
            risk_events = [
                {
                    "id": e.id,
                    "order_id": e.order_id,
                    "type": e.event_type,
                    "reason": store.read(e, "reason"),
                    "at": e.created_at.isoformat(),
                }
                for e in s.execute(
                    select(RiskEvent).order_by(RiskEvent.id.desc()).limit(limit)
                )
                .scalars()
                .all()
            ]
            decisions = [
                {
                    "id": d.id,
                    "prompt": store.read(d, "prompt"),
                    "reasoning_summary": store.read(
                        d,
                        "reasoning_summary",
                    ),
                    "model": d.model,
                    "at": d.created_at.isoformat(),
                }
                for d in s.execute(
                    select(LLMDecision).order_by(LLMDecision.id.desc()).limit(limit)
                )
                .scalars()
                .all()
            ]
            return {"risk_events": risk_events, "llm_decisions": decisions}

    # ── external (read-only) accounts ──────────────────────────
    def _external_available(self) -> bool:
        return self.external_source is not None

    def get_external_positions(self) -> dict[str, Any]:
        if not self._external_available():
            return {"available": False, "positions": []}
        positions = self._external_positions_map()
        return {
            "available": True,
            "stale": getattr(self.external_source, "stale", False),
            "positions": [
                {
                    "ticker": p.ticker,
                    "quantity": str(p.quantity),
                    "avg_cost": str(p.avg_cost),
                    "current_value": str(p.current_value),
                    "unrealized_pnl": str(p.unrealized_pnl),
                    "source": p.source,
                }
                for p in positions.values()
            ],
        }

    def get_external_account_summary(self) -> dict[str, Any]:
        if not self._external_available():
            return {"available": False}
        try:
            summary = self.external_source.get_account_summary()
        except Exception:
            return {"available": True, "stale": True}
        if summary is None:
            return {"available": True, "stale": True}
        return {
            "available": True,
            "total_equity": str(summary.total_equity),
            "cash": str(summary.cash),
            "buying_power": str(summary.buying_power),
            "source": summary.source,
            "stale": summary.stale,
        }

    def get_external_order_history(self, days: int = 30) -> dict[str, Any]:
        if not self._external_available():
            return {"available": False, "orders": []}
        try:
            return {"available": True, "orders": self.external_source.get_order_history(days)}
        except Exception:
            return {"available": True, "orders": [], "stale": True}

    def get_external_dividends(self, days: int = 90) -> dict[str, Any]:
        if not self._external_available():
            return {"available": False, "dividends": []}
        try:
            return {"available": True, "dividends": self.external_source.get_dividends(days)}
        except Exception:
            return {"available": True, "dividends": [], "stale": True}

    def get_combined_holdings(
        self,
        *,
        alpaca_positions: list[dict[str, Any]] | None = None,
        alpaca_observed_at: str | None = None,
    ) -> dict[str, Any]:
        """Alpaca + external positions in one view, labeled by source, with
        per-ticker combined totals. External rows are marked read-only."""
        if alpaca_positions is None:
            alpaca_positions = self.get_positions()
            alpaca_observed_at = utcnow().isoformat()
        alpaca = [
            {**position, "source": "alpaca", "read_only": False}
            for position in alpaca_positions
        ]
        ext = self.get_external_positions()
        external = [
            {
                "ticker": p["ticker"],
                "qty": p["quantity"],
                "current_value": p["current_value"],
                "source": p["source"],
                "read_only": True,
            }
            for p in ext.get("positions", [])
        ]
        combined: dict[str, float] = {}
        for row in alpaca:
            combined[row["ticker"]] = combined.get(row["ticker"], 0.0) + float(row["market_value"])
        for row in external:
            combined[row["ticker"]] = combined.get(row["ticker"], 0.0) + float(row["current_value"])
        return {
            "alpaca_observed_at": alpaca_observed_at,
            "alpaca": alpaca,
            "external": external,
            "combined_by_ticker": {k: round(v, 2) for k, v in combined.items()},
            "external_available": ext.get("available", False),
            "external_stale": ext.get("stale", False),
        }

    # ── conditional rules ──────────────────────────────────────
    @serialized_writer
    def create_conditional_rule(
        self,
        ticker: str,
        condition: dict[str, Any],
        action: dict[str, Any],
        *,
        actor: str,
        reason: str,
        request_id: str,
        kind: str = "price",
        group_key: str | None = None,
        plan_id: int | None = None,
        pre_approved: bool = False,
        fraction: str | Decimal | None = None,
        high_water_mark: str | Decimal | None = None,
        deadline=None,
    ) -> dict[str, Any]:
        from .rules.models import RuleCommand

        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        typed_condition, typed_kind = self._typed_rule_condition(
            condition, kind=kind, deadline=deadline
        )
        typed_action = dict(action)
        typed_action.setdefault(
            "order_type",
            "limit" if typed_action.get("limit_price") is not None else "market",
        )
        command = RuleCommand.model_validate(
            {
                "ticker": ticker,
                "kind": typed_kind,
                "condition": typed_condition,
                "action": typed_action,
                "group_key": group_key,
                "pre_approved": pre_approved,
                "fraction": fraction,
                "high_water_mark": high_water_mark,
            }
        )
        with self.session_factory() as s:
            rule = self.rule_application.persist_commands(
                s,
                [command],
                actor=actor,
                reason=reason,
                request_id=request_id,
                plan_id=plan_id,
            )[0]
            s.commit()
            return self._rule_dict(rule)

    @staticmethod
    def _typed_rule_condition(
        condition: dict[str, Any], *, kind: str, deadline=None
    ) -> tuple[dict[str, Any], str]:
        if "type" in condition:
            return dict(condition), kind
        if set(condition) == {"price_below"}:
            return {
                "type": "price",
                "direction": "below",
                "price": condition["price_below"],
            }, kind
        if set(condition) == {"price_above"}:
            return {
                "type": "price",
                "direction": "above",
                "price": condition["price_above"],
            }, kind
        if set(condition) == {"trailing_stop_pct"}:
            return {
                "type": "trailing",
                "percent": condition["trailing_stop_pct"],
            }, ("trailing" if kind == "price" else kind)
        if kind == "time" and condition == {} and deadline is not None:
            return {
                "type": "time",
                "deadline": deadline,
            }, kind
        # Preserve the raw value only long enough for the strict discriminated
        # RuleCommand parser to reject it. Nothing is persisted before validation.
        return dict(condition), kind

    def list_rules(self) -> list[dict[str, Any]]:
        with self.session_factory() as s:
            rows = s.execute(select(Rule)).scalars().all()
            return [self._rule_dict(r) for r in rows]

    @serialized_writer
    def cancel_rule(
        self,
        rule_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        if (
            not isinstance(idempotency_key, str)
            or idempotency_key != idempotency_key.strip()
            or len(idempotency_key) > 64
        ):
            raise ValueError("idempotency_key is invalid")
        with self.session_factory() as s:
            if idempotency_key:
                prior_success = s.scalar(
                    select(AuditEvent.id)
                    .where(
                        AuditEvent.actor == actor,
                        AuditEvent.action == "rule.cancel",
                        AuditEvent.target_type == "rule",
                        AuditEvent.target_id == str(rule_id),
                        AuditEvent.idempotency_key == idempotency_key,
                        AuditEvent.result_code == "canceled",
                    )
                    .limit(1)
                )
                if prior_success is not None:
                    return {"rule_id": rule_id, "canceled": True}
            rule = s.get(Rule, rule_id)
            if rule is None:
                return {"rule_id": rule_id, "canceled": False, "error": "not found"}
            if rule.plan_id is not None:
                return {
                    "rule_id": rule_id,
                    "canceled": False,
                    "error": "plan_rule_requires_plan_cancel",
                }
            if rule.state not in _RESUMABLE_RULE_STATES:
                return {
                    "rule_id": rule_id,
                    "canceled": False,
                    "error": f"rule is terminal: {rule.state}",
                }
            group = s.get(RuleGroup, rule.group_id)
            if group is None:
                return {
                    "rule_id": rule_id,
                    "canceled": False,
                    "error": "rule group not found",
                }
            if group.state not in _RESUMABLE_RULE_GROUP_STATES:
                return {
                    "rule_id": rule_id,
                    "canceled": False,
                    "error": f"rule group is terminal: {group.state}",
                }

            canceled = s.execute(
                update(Rule)
                .where(
                    Rule.id == rule_id,
                    Rule.state.in_(_RESUMABLE_RULE_STATES),
                    Rule.group_id.in_(
                        select(RuleGroup.id).where(
                            RuleGroup.state.in_(
                                _RESUMABLE_RULE_GROUP_STATES
                            )
                        )
                    ),
                )
                .values(state="canceled")
            )
            if canceled.rowcount != 1:
                s.rollback()
                return {
                    "rule_id": rule_id,
                    "canceled": False,
                    "error": "rule or group changed during cancellation",
                }

            resumable_sibling = s.scalar(
                select(Rule.id).where(
                    Rule.group_id == rule.group_id,
                    Rule.id != rule.id,
                    Rule.state.in_(_RESUMABLE_RULE_STATES),
                ).limit(1)
            )
            if resumable_sibling is None:
                group_canceled = s.execute(
                    update(RuleGroup)
                    .where(
                        RuleGroup.id == rule.group_id,
                        RuleGroup.state.in_(
                            _RESUMABLE_RULE_GROUP_STATES
                        ),
                    )
                    .values(
                        state="canceled",
                        terminal_rule_id=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        version=RuleGroup.version + 1,
                        updated_at=utcnow(),
                    )
                )
                if group_canceled.rowcount != 1:
                    s.rollback()
                    return {
                        "rule_id": rule_id,
                        "canceled": False,
                        "error": "rule group changed during cancellation",
                    }
                _persist_audit(
                        s,
                        actor=actor,
                        action="rule_group.cancel",
                        target_type="rule_group",
                        target_id=str(rule.group_id),
                        request_id=request_id,
                        reason=reason,
                        result_code="canceled",
                )
            _persist_audit(
                    s,
                    actor=actor,
                    action="rule.cancel",
                    target_type="rule",
                    target_id=str(rule_id),
                    request_id=request_id,
                    reason=reason,
                    result_code="canceled",
                    idempotency_key=idempotency_key,
            )
            s.commit()
            return {"rule_id": rule_id, "canceled": True}

    # ── serializers ────────────────────────────────────────────
    @staticmethod
    def _order_dict(o: Order) -> dict[str, Any]:
        return {
            "order_id": o.id,
            "ticker": o.ticker,
            "side": o.side,
            "order_type": o.order_type,
            "qty": None if o.qty is None else str(o.qty),
            "notional": None if o.notional is None else str(o.notional),
            "limit_price": None if o.limit_price is None else str(o.limit_price),
            "status": o.status,
            "created_at": o.created_at.isoformat(),
        }

    @staticmethod
    def _rule_dict(r: Rule) -> dict[str, Any]:
        return {
            "rule_id": r.id,
            "group_id": r.group_id,
            "payload_version": r.payload_version,
            "ticker": r.ticker,
            "condition": json.loads(r.condition_json),
            "action": json.loads(r.action_json),
            "state": r.state,
            "pre_approved": r.pre_approved,
            "activation": r.activation,
            "terminal_on_trigger": r.terminal_on_trigger,
        }
