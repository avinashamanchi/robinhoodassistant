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
    CircuitBreakerState,
    Fill,
    LLMDecision,
    Order,
    OrderStateMachine,
    Proposal,
    RiskEvent,
    Rule,
    RuleGroup,
    utcnow,
)
from .orders.application import (
    ApprovalCommand,
    ApprovalConflict as ApprovalApplicationConflict,
    OrderApplicationService,
)
from .orders.snapshot import PortfolioSnapshotService
from .orders.reconciliation import ReconciliationService
from .orders.submission import OrderSubmissionService
from .risk.breakers import BreakerScope, BreakerService
from .risk.clock import CryptoClock, MarketClock
from .risk.engine import RiskEngine
from .risk.pnl import FillLike, realized_pnl_today

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
            if AssetClass.for_symbol(r.ticker) is asset_class
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
    ) -> PortfolioSnapshot:
        return self.snapshot_service.assemble(
            session,
            tickers,
            asset_class,
            exclude_order_id=exclude_order_id,
            quote_overrides=quote_overrides,
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
    ) -> dict[str, Any]:
        """Create a PENDING proposal after a risk pre-check. Does NOT trade.

        A rejected order is still persisted (as REJECTED with a logged reason) so
        the UI can show why. An accepted order becomes PROPOSED, awaiting human
        approval — which will re-run the risk engine at execution time (A6/Phase 3).
        """
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
        request_id: str = "",
    ) -> dict[str, Any]:
        """Record an identified approval, then send through the durable outbox."""
        if not actor.strip() or not reason.strip():
            raise ValueError("approval actor and reason must be non-empty")
        try:
            approval = self.order_application.approve(
                ApprovalCommand(
                    order_id,
                    actor,
                    reason,
                    utcnow(),
                    request_id or uuid.uuid4().hex,
                )
            )
        except KeyError:
            return {"order_id": order_id, "error": "not found", "executed": False}
        except ApprovalApplicationConflict:
            with self.session_factory() as session:
                current = session.get(Order, order_id)
                return {
                    "order_id": order_id,
                    "status": current.status if current else None,
                    "executed": False,
                    "error": "order not in PROPOSED state (already decided?)",
                }
        if approval.status is OrderStatus.EXPIRED:
            return {
                "order_id": order_id,
                "status": OrderStatus.EXPIRED.value,
                "executed": False,
                "error": "proposal expired",
            }

        result = self.order_submission.submit(order_id)
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
        request_id: str = "",
    ) -> dict[str, Any]:
        """Persist, approve, and submit a bracket through the same durable outbox."""
        if not actor.strip() or not reason.strip():
            raise ValueError("approval actor and reason must be non-empty")
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
                    request_id or uuid.uuid4().hex,
                )
            )
            current_status = approval.status
        result = self.order_submission.submit(order_id)
        return {
            "executed": result.status
            in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED},
            "bracket": True,
            "order_id": order_id,
            "status": result.status.value,
            "broker_order_id": result.broker_order_id,
            "risk_reasons": list(result.risk_reasons),
        }

    def reject_order(self, order_id: int) -> dict[str, Any]:
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
                    order_id=order.id, event_type="rejection", reason="rejected by human"
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
        except Exception as exc:
            return {"db_ok": False, "error": type(exc).__name__}

    def panic(self, actor: str, reason: str) -> dict[str, Any]:
        """Latch and execute panic, returning only confirmed broker/local truth."""
        report = self.reconciliation.panic(actor, reason)
        return {
            "safe": report.safe,
            "confirmed_canceled": list(report.confirmed_canceled),
            "unconfirmed_order_ids": list(report.unconfirmed_order_ids),
            "remote_open_order_ids": list(report.remote_open_order_ids),
            "message": report.message,
        }

    def trip_all_killswitches(self, reason: str) -> None:
        """Fail closed across asset classes for an operational safety fault."""
        self.breakers.trip(
            BreakerScope.operator_global(),
            reason,
            "daemon:operations",
        )

    def reset_killswitch(
        self,
        asset_class: AssetClass | str,
        *,
        actor: str,
        reason: str,
        expected_generation: int,
    ) -> dict[str, Any]:
        ac = (
            asset_class
            if isinstance(asset_class, AssetClass)
            else AssetClass(asset_class)
        )
        actor = actor.strip()
        reason = reason.strip()
        if not actor or not reason:
            raise ValueError(
                "breaker reset actor and reason must be non-empty"
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
        snapshot = self.snapshot_service.assemble_for_execution(probe_symbol)
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
        )
        return {
            "killswitch": "reset",
            "asset_class": ac.value,
            "tripped": state.tripped,
            "generation": state.generation,
        }

    # ── hardening: fills, cancel/replace, reconcile, drills (P5) ─
    def record_fill(
        self,
        order_id: int,
        qty: str,
        price: str,
        broker_fill_id: Optional[str] = None,
        ts=None,
    ) -> dict[str, Any]:
        """Ingest a (possibly partial) fill and advance the order lifecycle.

        Idempotent on ``broker_fill_id`` — a duplicated fill event is ignored,
        so a phantom position can't be created (Phase 7 stress scenario #7).
        """
        from sqlalchemy import func

        with self.session_factory() as s:
            order = s.get(Order, order_id)
            if order is None:
                return {"error": "not found"}
            if broker_fill_id is not None:
                from sqlalchemy import func as _func

                dup = s.execute(
                    select(Fill).where(Fill.broker_fill_id == broker_fill_id)
                ).scalar_one_or_none()
                if dup is not None:
                    filled = s.execute(
                        select(_func.coalesce(_func.sum(Fill.qty), 0)).where(
                            Fill.order_id == order.id
                        )
                    ).scalar_one()
                    return {
                        "order_id": order_id,
                        "status": order.status,
                        "filled_qty": str(Decimal(str(filled))),
                        "duplicate": True,
                    }
            if OrderStatus(order.status) not in (
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIALLY_FILLED,
            ):
                return {"order_id": order_id, "status": order.status,
                        "error": "order not open for fills"}

            s.add(
                Fill(
                    order_id=order.id,
                    ticker=order.ticker,
                    side=order.side,
                    qty=Decimal(qty),
                    price=Decimal(price),
                    broker_fill_id=broker_fill_id,
                    filled_at=ts or utcnow(),
                )
            )
            s.flush()

            filled = s.execute(
                select(func.coalesce(func.sum(Fill.qty), 0)).where(
                    Fill.order_id == order.id
                )
            ).scalar_one()
            filled = Decimal(str(filled))
            target = order.qty
            if target is None and order.notional is not None:
                target = order.notional / Decimal(price)

            if target is not None and filled >= target - Decimal("0.000001"):
                OrderStateMachine.transition(order, OrderStatus.FILLED)
            elif order.status == OrderStatus.SUBMITTED.value:
                OrderStateMachine.transition(order, OrderStatus.PARTIALLY_FILLED)
            s.commit()
            return {
                "order_id": order_id,
                "status": order.status,
                "filled_qty": str(filled),
                "duplicate": False,
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

    def sync_open_orders(self) -> dict[str, Any]:
        """Compatibility facade for callers that still consume dictionary reports."""
        return self.serialize_reconciliation_report(
            self.reconciliation.reconcile()
        )

    def cancel_live_order(self, order_id: int) -> dict[str, Any]:
        """Cancel a live (SUBMITTED / PARTIALLY_FILLED) order at the broker + DB."""
        broker_status: OrderStatus | None = None
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
            if order.broker_order_id:
                try:
                    broker_result = self.broker.cancel_order(order.broker_order_id)
                except Exception as cancel_error:
                    # A cancel can race a fill. Query authoritative broker state;
                    # never claim CANCELED merely because the DELETE failed.
                    try:
                        broker_result = self.broker.get_order_status(
                            order.broker_order_id
                        )
                    except Exception:
                        return {
                            "order_id": order_id,
                            "status": order.status,
                            "error": (
                                "broker cancellation could not be confirmed: "
                                f"{type(cancel_error).__name__}"
                            ),
                        }
                broker_status = broker_result.status
                if broker_status is OrderStatus.CANCELED:
                    OrderStateMachine.transition(order, OrderStatus.CANCELED)
                    s.commit()
                    return {"order_id": order_id, "status": order.status}
            else:
                return {
                    "order_id": order_id,
                    "status": order.status,
                    "error": "live order has no broker order id",
                }

        # The broker reported a fill or another non-canceled state. Leave the
        # local row open until the normal reconciliation path ingests broker truth.
        sync = self.sync_open_orders()
        current = self.get_order_status(order_id)
        return {
            "order_id": order_id,
            "status": current["status"] if current else None,
            "error": f"broker order was not canceled; reported {broker_status.value}",
            "sync": sync,
        }

    def replace_order(self, order_id: int, **new_order) -> dict[str, Any]:
        """Cancel/replace: cancel the live order, then propose a replacement."""
        cancel = self.cancel_live_order(order_id)
        if "error" in cancel:
            return {"canceled": cancel, "replacement": None}
        replacement = self.propose_order(**new_order)
        return {"canceled": cancel, "replacement": replacement}

    def reconcile_positions(self) -> dict[str, Any]:
        """Compare broker truth to locally-derived positions; log any drift (§6)."""
        broker_pos = {p.ticker.upper(): p.qty for p in self.broker.get_positions()}
        local: dict[str, Decimal] = {}
        with self.session_factory() as s:
            for f in s.execute(select(Fill)).scalars().all():
                delta = f.qty if f.side == "buy" else -f.qty
                local[f.ticker.upper()] = local.get(f.ticker.upper(), Decimal(0)) + delta
            drift = {}
            for ticker in set(broker_pos) | set(local):
                b = Decimal(str(broker_pos.get(ticker, 0)))
                l = local.get(ticker, Decimal(0))
                if b != l:
                    drift[ticker] = {"broker": str(b), "local": str(l)}
            if drift:
                s.add(
                    RiskEvent(
                        event_type="reconciliation",
                        reason=json.dumps(drift),
                    )
                )
                s.commit()
        return {"reconciled": not drift, "drift": drift}

    def enforce_daily_loss_limits(self) -> dict[str, bool]:
        """Trip each asset class's kill switch if its realized daily loss breached."""
        realized: dict[AssetClass, Decimal] = {}
        with self.session_factory() as s:
            for ac in (AssetClass.EQUITY, AssetClass.CRYPTO):
                realized[ac] = self._realized_pnl_today(s, ac)
        result: dict[str, bool] = {}
        for ac, pnl in realized.items():
            limit = abs(self._loss_limit_for(ac))
            if pnl <= -limit:
                self.breakers.trip(
                    BreakerScope.loss(ac),
                    (
                        f"daily realized loss {pnl} breached limit "
                        f"-{limit}"
                    ),
                    "daemon:daily-loss",
                )
            result[ac.value] = self.breakers.is_tripped(
                BreakerScope.loss(ac)
            )
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
        return [
            {
                "ticker": p.ticker,
                "qty": str(p.qty),
                "avg_entry_price": str(p.avg_entry_price),
                "current_price": str(p.current_price),
                "market_value": str(p.market_value),
            }
            for p in self.broker.get_positions()
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
        kind: str = "price",
        group_key: str | None = None,
        plan_id: int | None = None,
        pre_approved: bool = False,
        fraction: str | Decimal | None = None,
        high_water_mark: str | Decimal | None = None,
        deadline=None,
    ) -> dict[str, Any]:
        from .rules.models import RuleCommand

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
        rule_id = self.rule_application.create_rule(command, plan_id=plan_id)
        with self.session_factory() as s:
            rule = s.get(Rule, rule_id)
            assert rule is not None
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

    def cancel_rule(self, rule_id: int) -> dict[str, Any]:
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
