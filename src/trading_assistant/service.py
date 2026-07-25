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
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from .broker.base import BrokerClient
from .broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
)
from .assets import AssetClass
from .config import AppConfig
from .db.models import (
    AuditEvent,
    CircuitBreakerState,
    FILL_RECONCILIATION_REQUIRED,
    Fill,
    LLMDecision,
    Order,
    OrderStateMachine,
    Proposal,
    RiskEvent,
    Rule,
    RuleGroup,
    fill_has_trusted_identity,
    utcnow,
)
from .dependencies import RequiredDependencyUnavailable
from .orders.application import (
    ApprovalCommand,
    ApprovalConflict as ApprovalApplicationConflict,
    OrderApplicationService,
)
from .orders.snapshot import PortfolioSnapshotService
from .orders.reconciliation import ReconciliationService
from .orders.submission import OrderSubmissionService
from .risk.breakers import BreakerScope, BreakerService, trip_in_session
from .risk.clock import CryptoClock, MarketClock
from .risk.engine import RiskEngine
from .risk.pnl import FillLike, realized_pnl_today
from .risk.submission_barrier import SubmissionBarrier

# Statuses that count as "still live / open" for listing purposes.
_OPEN_STATUSES = (
    OrderStatus.PROPOSED.value,
    OrderStatus.APPROVED.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)

_EXIT_RESERVATION_STATUSES = (
    OrderStatus.APPROVED.value,
    OrderStatus.APPROVAL_RECORDED.value,
    OrderStatus.SUBMITTING.value,
    OrderStatus.ACCEPTANCE_UNKNOWN.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)

_RESUMABLE_RULE_STATES = ("active", "processing")

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


class TradingService:
    def __init__(
        self,
        broker: BrokerClient,
        session_factory: sessionmaker[Session],
        config: AppConfig,
        clock: MarketClock,
        crypto_clock: Optional[MarketClock] = None,
        external_source=None,
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
            session.add(
                AuditEvent(
                    actor=actor,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    request_id=request_id,
                    reason=reason,
                    result_code="dependency_unavailable",
                    detail_json=json.dumps(detail, sort_keys=True),
                )
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

    def get_account_summary(self) -> dict[str, Any]:
        acct = self.broker.get_account()
        positions = [
            {
                "ticker": p.ticker,
                "qty": str(p.qty),
                "avg_entry_price": str(p.avg_entry_price),
                "current_price": str(p.current_price),
                "market_value": str(p.market_value),
            }
            for p in self.broker.get_positions()
        ]
        return {
            "buying_power": str(acct.buying_power),
            "equity": str(acct.equity),
            "cash": str(acct.cash),
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
            )
            s.add(order)
            s.flush()
            risk_cfg = self.config.crypto_risk if ac is AssetClass.CRYPTO else self.config.risk
            ttl = (risk_cfg or self.config.risk).proposal_ttl_minutes
            s.add(
                Proposal(
                    order_id=order.id,
                    ttl_minutes=ttl,
                    expires_at=utcnow() + timedelta(minutes=ttl),
                )
            )

            if result.rejected:
                OrderStateMachine.transition(order, OrderStatus.REJECTED)
                s.add(
                    RiskEvent(
                        order_id=order.id,
                        event_type="rejection",
                        reason=result.reason_text(),
                    )
                )
            # Non-blocking warnings (e.g. cross-broker concentration) are logged
            # but never change the outcome.
            for warning in result.warnings:
                s.add(RiskEvent(order_id=order.id, event_type="warning", reason=warning))
            s.add(
                AuditEvent(
                    actor=actor,
                    action="order.propose",
                    target_type="order",
                    target_id=str(order.id),
                    request_id=request_id,
                    reason=reason,
                    result_code=order.status,
                )
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
        return OrderRequest(
            ticker=order.ticker,
            side=OrderSide(order.side),
            order_type=OrderType(order.order_type),
            idempotency_key=order.idempotency_key,
            qty=order.qty,
            notional=order.notional,
            limit_price=order.limit_price,
        )

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

    def submit_bracket_order(
        self,
        order_req: OrderRequest,
        take_profit,
        stop_loss,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Persist, approve, and submit a bracket through the same durable outbox."""
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
        with self.session_factory() as s:
            existing = s.execute(
                select(Order).where(
                    Order.idempotency_key == order_req.idempotency_key
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
                        {"take_profit": str(take_profit), "stop_loss": str(stop_loss)}
                    ),
                )
                s.add(order)
                risk_cfg = self.config.crypto_risk if self._asset_class(order_req.ticker) is AssetClass.CRYPTO else self.config.risk
                ttl = (risk_cfg or self.config.risk).proposal_ttl_minutes
                s.flush()
                s.add(
                    Proposal(
                        order_id=order.id,
                        ttl_minutes=ttl,
                        expires_at=utcnow() + timedelta(minutes=ttl),
                    )
                )
                s.add(
                    AuditEvent(
                        actor=actor,
                        action="order.propose",
                        target_type="order",
                        target_id=str(order.id),
                        request_id=request_id,
                        reason=reason,
                        result_code=OrderStatus.PROPOSED.value,
                        detail_json=json.dumps(
                            {"submission_kind": "bracket"},
                            sort_keys=True,
                        ),
                    )
                )
                s.commit()
                order_id = order.id

        with self.session_factory() as session:
            current = session.get(Order, order_id)
            assert current is not None
            current_status = OrderStatus(current.status)
        if current_status is OrderStatus.PROPOSED:
            approval = self.order_application.approve(
                ApprovalCommand(
                    order_id,
                    actor,
                    reason,
                    utcnow(),
                    request_id,
                )
            )
            current_status = approval.status
        result = self.order_submission.submit(
            order_id,
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        return {
            "executed": result.status
            in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED},
            "bracket": True,
            "order_id": order_id,
            "status": result.status.value,
            "broker_order_id": result.broker_order_id,
            "risk_reasons": list(result.risk_reasons),
        }

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
            s.add(
                RiskEvent(
                    order_id=order.id,
                    event_type="rejection",
                    reason=reason,
                )
            )
            s.add(
                AuditEvent(
                    actor=actor,
                    action="order.reject",
                    target_type="order",
                    target_id=str(order_id),
                    request_id=request_id,
                    reason=reason,
                    result_code=OrderStatus.REJECTED.value,
                )
            )
            s.commit()
            return {"order_id": order_id, "status": order.status}

    def write_heartbeat(self, source: str = "daemon") -> None:
        from .db.models import Heartbeat

        with self.session_factory() as s:
            s.add(Heartbeat(source=source))
            s.commit()

    def health(self) -> dict[str, Any]:
        """Liveness for GET /health (no auth): heartbeat age, DB ok, kill switches."""
        from sqlalchemy import select as _select

        from .db.models import Heartbeat

        try:
            with self.session_factory() as s:
                last = s.execute(
                    _select(Heartbeat).order_by(Heartbeat.id.desc()).limit(1)
                ).scalar_one_or_none()
                age = (utcnow() - last.at).total_seconds() if last else None
                eq_state = s.get(
                    CircuitBreakerState,
                    BreakerScope.loss(AssetClass.EQUITY).key,
                )
                cr_state = s.get(
                    CircuitBreakerState,
                    BreakerScope.loss(AssetClass.CRYPTO).key,
                )
                eq_tripped = bool(eq_state and eq_state.tripped)
                cr_tripped = bool(cr_state and cr_state.tripped)
            return {
                "db_ok": True,
                "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                "daemon_alive": (
                    age is not None
                    and age < self.config.daemon.heartbeat_stale_seconds
                ),
                "killswitch": {"equity": eq_tripped, "crypto": cr_tripped},
                "killswitch_generation": {
                    "equity": (
                        eq_state.generation if eq_state is not None else None
                    ),
                    "crypto": (
                        cr_state.generation if cr_state is not None else None
                    ),
                },
            }
        except Exception:
            return {"db_ok": False, "error": "database_unavailable"}

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

    @staticmethod
    def _reset_health_complete(snapshot: PortfolioSnapshot) -> bool:
        daily_total = (
            snapshot.realized_pnl_today
            + snapshot.unrealized_pnl_today
        )
        return (
            snapshot.daily_pnl_complete is True
            and snapshot.broker_reconciled is True
            and snapshot.account_complete is True
            and snapshot.pending_exposure_complete is True
            and snapshot.quote_fresh is True
            and daily_total.is_finite()
            and snapshot.account_equity.is_finite()
            and snapshot.account_equity > 0
        )

    def reset_killswitch(
        self,
        asset_class: AssetClass | str,
        *,
        actor: str,
        reason: str,
        expected_generation: int,
        request_id: str,
    ) -> dict[str, Any]:
        ac = (
            asset_class
            if isinstance(asset_class, AssetClass)
            else AssetClass(asset_class)
        )
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        risk_config = (
            self.config.crypto_risk or self.config.risk
            if ac is AssetClass.CRYPTO
            else self.config.risk
        )
        probe_symbol = next(
            (
                symbol
                for symbol in risk_config.ticker_allowlist
                if self._asset_class(symbol) is ac
            ),
            None,
        )
        if probe_symbol is None:
            raise RuntimeError(
                f"no configured {ac.value} symbol for fresh breaker health"
            )
        try:
            snapshot = self.snapshot_service.assemble_for_execution(
                probe_symbol
            )
        except RequiredDependencyUnavailable:
            self._audit_dependency_failure(
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="circuit_breaker.reset",
                target_type="circuit_breaker",
                target_id=BreakerScope.loss(ac).key,
                detail={
                    "asset_class": ac.value,
                    "expected_generation": expected_generation,
                    "stage": "health_collection",
                },
            )
            raise RequiredDependencyUnavailable from None
        if not self._reset_health_complete(snapshot):
            self._audit_dependency_failure(
                actor=actor,
                reason=reason,
                request_id=request_id,
                action="circuit_breaker.reset",
                target_type="circuit_breaker",
                target_id=BreakerScope.loss(ac).key,
                detail={
                    "asset_class": ac.value,
                    "expected_generation": expected_generation,
                    "stage": "health_validation",
                },
            )
            raise RequiredDependencyUnavailable
        prior_health = {
            "captured_at": snapshot.as_of.isoformat(),
            "daily_pnl_complete": snapshot.daily_pnl_complete,
            "daily_total_pnl": str(
                snapshot.realized_pnl_today
                + snapshot.unrealized_pnl_today
            ),
            "broker_reconciled": snapshot.broker_reconciled,
            "account_equity": str(snapshot.account_equity),
            "quote_fresh": snapshot.quote_fresh,
            "active_breakers": sorted(snapshot.active_breakers),
        }
        state = self.breakers.reset(
            BreakerScope.loss(ac),
            actor=actor,
            reason=reason,
            prior_health=prior_health,
            expected_generation=expected_generation,
            request_id=request_id,
        )
        return {
            "killswitch": "reset",
            "asset_class": ac.value,
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
        with self.session_factory() as session:
            session.add(
                AuditEvent(
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
            )
            session.commit()
        return result

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
                session.add(
                    AuditEvent(
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
                )
                session.commit()
            return result

    def _cancel_live_order_under_writer(
        self,
        order_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
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
                    order.updated_at = now
                    order.version += 1
                    session.add(
                        AuditEvent(
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
                    )
                    session.commit()
                return {
                    "order_id": order_id,
                    "status": local_status,
                    "error": "broker cancellation could not be confirmed",
                }
        broker_status = broker_result.status
        sync = self.sync_open_orders(
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        current = self.get_order_status(order_id)
        if (
            broker_status is OrderStatus.CANCELED
            and current is not None
            and current["status"] == OrderStatus.CANCELED.value
        ):
            return {"order_id": order_id, "status": current["status"]}
        return {
            "order_id": order_id,
            "status": current["status"] if current else None,
            "error": f"broker order was not canceled; reported {broker_status.value}",
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
                    session.add(
                        RiskEvent(
                            event_type="reconciliation",
                            reason=drift_detail,
                        )
                    )
                    trip_in_session(
                        session,
                        BreakerScope.broker_drift(),
                        f"position reconciliation drift: {drift_detail}",
                        actor,
                        request_id=request_id,
                        audit_reason=reason,
                    )
                session.add(
                    AuditEvent(
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

    def get_positions(self) -> list[dict[str, Any]]:
        try:
            positions = self.broker.get_positions()
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
            risk_events = [
                {
                    "id": e.id,
                    "order_id": e.order_id,
                    "type": e.event_type,
                    "reason": e.reason,
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
                    "prompt": d.prompt,
                    "reasoning_summary": d.reasoning_summary,
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

    def get_combined_holdings(self) -> dict[str, Any]:
        """Alpaca + external positions in one view, labeled by source, with
        per-ticker combined totals. External rows are marked read-only."""
        alpaca = [
            {**p, "source": "alpaca", "read_only": False} for p in self.get_positions()
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
            "alpaca": alpaca,
            "external": external,
            "combined_by_ticker": {k: round(v, 2) for k, v in combined.items()},
            "external_available": ext.get("available", False),
            "external_stale": ext.get("stale", False),
        }

    # ── conditional rules ──────────────────────────────────────
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

    def cancel_rule(
        self,
        rule_id: int,
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
            rule = s.get(Rule, rule_id)
            if rule is None:
                return {"rule_id": rule_id, "canceled": False, "error": "not found"}
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
            if group.state != "active":
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
                            RuleGroup.state == "active"
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
                        RuleGroup.state == "active",
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
                s.add(
                    AuditEvent(
                        actor=actor,
                        action="rule_group.cancel",
                        target_type="rule_group",
                        target_id=str(rule.group_id),
                        request_id=request_id,
                        reason=reason,
                        result_code="canceled",
                    )
                )
            s.add(
                AuditEvent(
                    actor=actor,
                    action="rule.cancel",
                    target_type="rule",
                    target_id=str(rule_id),
                    request_id=request_id,
                    reason=reason,
                    result_code="canceled",
                )
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
        }
