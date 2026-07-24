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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    ApprovalConflict,
    Fill,
    LLMDecision,
    Order,
    OrderStateMachine,
    Proposal,
    RiskEvent,
    Rule,
    approve_proposed,
    utcnow,
)
from .risk.clock import CryptoClock, MarketClock
from .risk.engine import RiskEngine
from .risk.killswitch import KillSwitch
from .risk.pnl import FillLike, realized_pnl_today

# Statuses that count as "still live / open" for listing purposes.
_OPEN_STATUSES = (
    OrderStatus.PROPOSED.value,
    OrderStatus.APPROVED.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)

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
    ) -> PortfolioSnapshot:
        positions = self.broker.get_positions()
        pos_map = {p.ticker.upper(): p for p in positions}
        pending_query = select(Order).where(
            Order.status.in_(
                (
                    OrderStatus.APPROVED.value,
                    OrderStatus.SUBMITTED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                )
            )
        )
        if exclude_order_id is not None:
            pending_query = pending_query.where(Order.id != exclude_order_id)
        pending_orders = session.execute(pending_query).scalars().all()
        want = (
            {t.upper() for t in tickers}
            | set(pos_map)
            | {order.ticker.upper() for order in pending_orders}
        )
        # A symbol the broker can't quote (typo / unknown ticker) must not crash
        # the snapshot — omit it. The risk engine then rejects cleanly ("not on
        # allowlist" and/or "no quote available"), which is fail-closed.
        quotes = {}
        for sym in want:
            try:
                quotes[sym] = self.broker.get_quote(sym)
            except Exception:  # noqa: BLE001 — unquotable symbol -> no price, safe rejection
                log.warning("no quote for %s; omitting from snapshot", sym)
        account = self.broker.get_account()
        pending_signed_notional: dict[str, Decimal] = {}
        pending_exposure_complete = True
        for pending_order in pending_orders:
            symbol = pending_order.ticker.upper()
            quote = quotes.get(symbol)
            if quote is None:
                pending_exposure_complete = False
                continue
            recorded_qty = sum(
                (fill.qty for fill in pending_order.fills), Decimal(0)
            )
            recorded_notional = sum(
                (fill.qty * fill.price for fill in pending_order.fills), Decimal(0)
            )
            if pending_order.qty is not None:
                remaining_qty = max(
                    pending_order.qty - recorded_qty, Decimal(0)
                )
                remaining_notional = remaining_qty * quote.last
            else:
                remaining_notional = max(
                    (pending_order.notional or Decimal(0)) - recorded_notional,
                    Decimal(0),
                )
            signed_notional = (
                remaining_notional
                if pending_order.side == OrderSide.BUY.value
                else -remaining_notional
            )
            pending_signed_notional[symbol] = (
                pending_signed_notional.get(symbol, Decimal(0)) + signed_notional
            )
        return PortfolioSnapshot(
            positions=pos_map,
            quotes=quotes,
            buying_power=account.buying_power,
            realized_pnl_today=self._realized_pnl_today(session, asset_class),
            external_positions=self._external_positions_map(),
            pending_signed_notional=pending_signed_notional,
            pending_exposure_complete=pending_exposure_complete,
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
        with self.session_factory() as s:
            snapshot = self.assemble_snapshot(s, [order_req.ticker], ac)
            result = self._risk_for(ac).check(
                order_req,
                snapshot,
                killswitch_tripped=KillSwitch.is_tripped(s, ac),
                market_open=self._clock_for(ac).is_open(),
            )

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

    def approve_order(self, order_id: int) -> dict[str, Any]:
        """Approve a PROPOSED order, re-run risk at execution moment, then submit.

        This is the ONLY path that trades. It: (1) refuses expired proposals (A6),
        (2) atomically compare-and-sets PROPOSED->APPROVED (A5) — a second approver
        conflicts, (3) re-runs the full risk engine against a FRESH snapshot
        (prices move between proposal and approval), rejecting if anything now
        fails, and only then (4) submits to the broker.
        """
        with self.session_factory() as s:
            order = s.get(Order, order_id)
            if order is None:
                return {"order_id": order_id, "error": "not found", "executed": False}

            # A6: expired proposals cannot be approved.
            if (
                order.status == OrderStatus.PROPOSED.value
                and order.proposal is not None
                and order.proposal.is_expired()
            ):
                OrderStateMachine.transition(order, OrderStatus.EXPIRED)
                s.commit()
                return {
                    "order_id": order_id,
                    "status": OrderStatus.EXPIRED.value,
                    "executed": False,
                    "error": "proposal expired",
                }

            # A5: atomic exactly-once approval.
            try:
                approve_proposed(s, order_id)
            except ApprovalConflict:
                s.rollback()
                current = s.get(Order, order_id)
                return {
                    "order_id": order_id,
                    "status": current.status if current else None,
                    "executed": False,
                    "error": "order not in PROPOSED state (already decided?)",
                }
            s.refresh(order)  # pick up status = APPROVED from the CAS UPDATE

            # Execution-time risk re-check against a fresh snapshot, routed by class.
            ac = self._asset_class(order.ticker)
            order_req = self._order_request_from(order)
            snapshot = self.assemble_snapshot(
                s, [order.ticker], ac, exclude_order_id=order.id
            )
            result = self._risk_for(ac).check(
                order_req,
                snapshot,
                killswitch_tripped=KillSwitch.is_tripped(s, ac),
                market_open=self._clock_for(ac).is_open(),
            )
            if result.rejected:
                OrderStateMachine.transition(order, OrderStatus.REJECTED)
                s.add(
                    RiskEvent(
                        order_id=order.id,
                        event_type="rejection",
                        reason="execution-time: " + result.reason_text(),
                    )
                )
                s.commit()
                return {
                    "order_id": order_id,
                    "status": OrderStatus.REJECTED.value,
                    "executed": False,
                    "risk_reasons": result.reasons,
                }

            # Passed final risk check -> submit to broker.
            broker_result = self.broker.submit_order(order_req)
            order.broker_order_id = broker_result.broker_order_id
            OrderStateMachine.transition(order, OrderStatus.SUBMITTED)
            s.commit()
            return {
                "order_id": order_id,
                "status": order.status,
                "executed": True,
                "broker_order_id": order.broker_order_id,
            }

    def submit_bracket_order(
        self, order_req: OrderRequest, take_profit, stop_loss
    ) -> dict[str, Any]:
        """Risk-check then submit a server-side bracket (D4). The entry still passes
        the risk engine; the broker holds the OCO exit so it survives our downtime."""
        if not hasattr(self.broker, "submit_bracket"):
            return {"error": "broker does not support bracket orders", "executed": False}
        ac = self._asset_class(order_req.ticker)
        with self.session_factory() as s:
            existing = s.execute(
                select(Order).where(
                    Order.idempotency_key == order_req.idempotency_key
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.broker_order_id:
                    return {
                        "executed": True,
                        "bracket": True,
                        "order_id": existing.id,
                        "broker_order_id": existing.broker_order_id,
                    }
                order_id = existing.id
            else:
                snapshot = self.assemble_snapshot(s, [order_req.ticker], ac)
                result = self._risk_for(ac).check(
                    order_req, snapshot,
                    killswitch_tripped=KillSwitch.is_tripped(s, ac),
                    market_open=self._clock_for(ac).is_open(),
                )
                if result.rejected:
                    return {
                        "executed": False,
                        "status": "rejected",
                        "risk_reasons": result.reasons,
                    }
                # Durable outbox: persist the stable idempotency key BEFORE broker
                # I/O. SUBMITTED with no broker id means acceptance is unknown and
                # retrying this same request is required.
                order = Order(
                    idempotency_key=order_req.idempotency_key,
                    ticker=order_req.ticker,
                    side=order_req.side.value,
                    order_type=order_req.order_type.value,
                    qty=order_req.qty,
                    notional=order_req.notional,
                    limit_price=order_req.limit_price,
                    status=OrderStatus.SUBMITTED.value,
                )
                s.add(order)
                try:
                    s.commit()
                    order_id = order.id
                except IntegrityError:
                    s.rollback()
                    concurrent = s.execute(
                        select(Order).where(
                            Order.idempotency_key == order_req.idempotency_key
                        )
                    ).scalar_one()
                    if concurrent.broker_order_id:
                        return {
                            "executed": True,
                            "bracket": True,
                            "order_id": concurrent.id,
                            "broker_order_id": concurrent.broker_order_id,
                        }
                    order_id = concurrent.id

        broker_result = self.broker.submit_bracket(
            order_req, take_profit, stop_loss
        )
        with self.session_factory() as s:
            order = s.get(Order, order_id)
            if order is None:
                raise RuntimeError("durable bracket outbox row disappeared")
            order.broker_order_id = broker_result.broker_order_id
            s.commit()
            return {
                "executed": True,
                "bracket": True,
                "order_id": order.id,
                "broker_order_id": order.broker_order_id,
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
                eq_tripped = KillSwitch.is_tripped(s, AssetClass.EQUITY)
                cr_tripped = KillSwitch.is_tripped(s, AssetClass.CRYPTO)
            return {
                "db_ok": True,
                "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                "daemon_alive": (
                    age is not None
                    and age < self.config.daemon.heartbeat_stale_seconds
                ),
                "killswitch": {"equity": eq_tripped, "crypto": cr_tripped},
            }
        except Exception as exc:
            return {"db_ok": False, "error": type(exc).__name__}

    def panic(self) -> dict[str, Any]:
        """PANIC: cancel open orders, disable all rules, trip all kill switches.

        Kill switches and rules are disabled before any broker I/O. An order is
        counted canceled only when the broker confirms it.
        """
        from .db.models import Rule

        with self.session_factory() as s:
            order_ids = [
                o.id
                for o in s.execute(
                    select(Order).where(
                        Order.status.in_(
                            (
                                OrderStatus.SUBMITTED.value,
                                OrderStatus.PARTIALLY_FILLED.value,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            ]

            rules = s.execute(select(Rule).where(Rule.state == "active")).scalars().all()
            for r in rules:
                r.state = "canceled"

            KillSwitch.trip(s, "panic button", AssetClass.EQUITY)
            KillSwitch.trip(s, "panic button", AssetClass.CRYPTO)
            s.add(RiskEvent(event_type="panic", reason="panic button engaged"))
            s.commit()

        canceled: list[int] = []
        unconfirmed: list[int] = []
        for order_id in order_ids:
            result = self.cancel_live_order(order_id)
            if result.get("status") == OrderStatus.CANCELED.value:
                canceled.append(order_id)
            else:
                unconfirmed.append(order_id)
        if unconfirmed:
            with self.session_factory() as s:
                s.add(
                    RiskEvent(
                        event_type="panic",
                        reason=f"broker cancellation unconfirmed for orders {unconfirmed}",
                    )
                )
                s.commit()
        return {
            "panic": True,
            "orders_canceled": len(canceled),
            "orders_unconfirmed": unconfirmed,
            "rules_disabled": len(rules),
            "killswitches_tripped": ["equity", "crypto"],
        }

    def trip_all_killswitches(self, reason: str) -> None:
        """Fail closed across asset classes for an operational safety fault."""
        with self.session_factory() as s:
            KillSwitch.trip(s, reason, AssetClass.EQUITY)
            KillSwitch.trip(s, reason, AssetClass.CRYPTO)
            s.commit()

    def reset_killswitch(
        self, asset_class: AssetClass | str = AssetClass.EQUITY
    ) -> dict[str, Any]:
        ac = asset_class if isinstance(asset_class, AssetClass) else AssetClass(asset_class)
        with self.session_factory() as s:
            KillSwitch.reset(s, asset_class=ac)
            s.commit()
            return {"killswitch": "reset", "asset_class": ac.value, "tripped": False}

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

    def sync_open_orders(self) -> dict[str, Any]:
        """Poll the broker for each live order and reconcile status + fills locally.

        Closes the gap between Alpaca truth and our DB: records new fills (idempotent
        on broker_order_id:cumulative_qty) and advances the lifecycle so realized
        P&L, the daily-loss kill switch, and /reconcile all reflect real fills.
        """
        from sqlalchemy import func as _func

        _STATUS_MAP = {
            OrderStatus.FILLED: OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCELED: OrderStatus.CANCELED,
            OrderStatus.REJECTED: OrderStatus.REJECTED,
            OrderStatus.EXPIRED: OrderStatus.EXPIRED,
        }
        synced = filled = failed = fills_repaired = 0
        with self.session_factory() as s:
            unknown_acceptance = s.execute(
                select(Order).where(
                    Order.status.in_(
                        (
                            OrderStatus.SUBMITTED.value,
                            OrderStatus.PARTIALLY_FILLED.value,
                        )
                    ),
                    Order.broker_order_id.is_(None),
                )
            ).scalars().all()
            if unknown_acceptance:
                failed += len(unknown_acceptance)
                log.error(
                    "submitted outbox rows lack broker ids: %s",
                    [order.id for order in unknown_acceptance],
                )
            open_orders = s.execute(
                select(Order).where(
                    Order.status.in_(
                        (OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value)
                    ),
                    Order.broker_order_id.isnot(None),
                )
            ).scalars().all()
            repair_orders: dict[int, Order] = {}
            for prior_fill in s.execute(
                select(Fill).where(Fill.broker_fill_id.isnot(None))
            ).scalars().all():
                prior_order = (
                    s.get(Order, prior_fill.order_id)
                    if prior_fill.order_id is not None
                    else None
                )
                if (
                    prior_order is not None
                    and prior_order.broker_order_id
                    and prior_fill.broker_fill_id.startswith(
                        f"{prior_order.broker_order_id}:"
                    )
                ):
                    repair_orders[prior_order.id] = prior_order

            exact_activities = None
            activity_reader = getattr(self.broker, "get_fill_activities", None)
            activity_targets = {
                order.id: order for order in [*open_orders, *repair_orders.values()]
            }
            if callable(activity_reader) and activity_targets:
                earliest = min(
                    order.created_at for order in activity_targets.values()
                ) - timedelta(days=1)
                try:
                    exact_activities = activity_reader(after=earliest)
                except Exception as exc:
                    failed += len(activity_targets)
                    log.warning(
                        "exact broker fill activity sync failed for %d order(s): %s",
                        len(activity_targets),
                        type(exc).__name__,
                    )
                    return {
                        "synced": 0,
                        "newly_filled": 0,
                        "failed": failed,
                        "fills_repaired": 0,
                    }

            def apply_exact_activities(
                order: Order, activities: list[Any]
            ) -> bool:
                matching = [
                    activity
                    for activity in activities
                    if activity.broker_order_id == order.broker_order_id
                ]
                if not matching:
                    return False
                changed = False
                for prior in s.execute(
                    select(Fill).where(Fill.order_id == order.id)
                ).scalars().all():
                    if (
                        prior.broker_fill_id
                        and prior.broker_fill_id.startswith(
                            f"{order.broker_order_id}:"
                        )
                    ):
                        s.delete(prior)
                        changed = True
                s.flush()
                existing_ids = {
                    fill_id
                    for fill_id in s.execute(
                        select(Fill.broker_fill_id).where(Fill.order_id == order.id)
                    ).scalars()
                    if fill_id is not None
                }
                for activity in matching:
                    if activity.broker_fill_id not in existing_ids:
                        s.add(
                            Fill(
                                order_id=order.id,
                                ticker=activity.ticker,
                                side=activity.side,
                                qty=activity.qty,
                                price=activity.price,
                                broker_fill_id=activity.broker_fill_id,
                                filled_at=activity.filled_at,
                            )
                        )
                        changed = True
                s.flush()
                return changed

            if exact_activities is not None:
                for repair_order in repair_orders.values():
                    if apply_exact_activities(repair_order, exact_activities):
                        fills_repaired += 1

            for o in open_orders:
                try:
                    res = self.broker.get_order_status(o.broker_order_id)
                except Exception as exc:
                    failed += 1
                    log.warning(
                        "broker status sync failed for local order %s: %s",
                        o.id,
                        type(exc).__name__,
                    )
                    continue
                synced += 1
                if exact_activities is not None:
                    apply_exact_activities(o, exact_activities)
                recorded = Decimal(str(
                    s.execute(
                        select(_func.coalesce(_func.sum(Fill.qty), 0)).where(Fill.order_id == o.id)
                    ).scalar_one()
                ))
                new_qty = res.filled_qty - recorded
                if (
                    exact_activities is not None
                    and new_qty > Decimal("0.000001")
                ):
                    failed += 1
                    log.warning(
                        "broker order %s reports %s filled but exact activities "
                        "contain only %s; deferring terminal transition",
                        o.id,
                        res.filled_qty,
                        recorded,
                    )
                    continue
                if (
                    exact_activities is None
                    and new_qty > 0
                    and res.avg_fill_price is not None
                ):
                    prior_fills = s.execute(
                        select(Fill).where(Fill.order_id == o.id)
                    ).scalars().all()
                    recorded_notional = sum(
                        (fill.qty * fill.price for fill in prior_fills), Decimal(0)
                    )
                    cumulative_notional = res.filled_qty * res.avg_fill_price
                    incremental_notional = cumulative_notional - recorded_notional
                    if incremental_notional <= 0:
                        log.error(
                            "broker cumulative fill moved behind local ledger for order %s: "
                            "broker_notional=%s local_notional=%s",
                            o.id,
                            cumulative_notional,
                            recorded_notional,
                        )
                        continue
                    incremental_price = incremental_notional / new_qty
                    s.add(Fill(
                        order_id=o.id, ticker=o.ticker, side=o.side,
                        qty=new_qty, price=incremental_price,
                        broker_fill_id=f"{o.broker_order_id}:{res.filled_qty}",
                    ))
                target = _STATUS_MAP.get(res.status)
                if target is not None and target.value != o.status:
                    if OrderStateMachine.can_transition(OrderStatus(o.status), target):
                        OrderStateMachine.transition(o, target)
                        if target is OrderStatus.FILLED:
                            filled += 1
            s.commit()
        return {
            "synced": synced,
            "newly_filled": filled,
            "failed": failed,
            "fills_repaired": fills_repaired,
        }

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
        result: dict[str, bool] = {}
        with self.session_factory() as s:
            for ac in (AssetClass.EQUITY, AssetClass.CRYPTO):
                pnl = self._realized_pnl_today(s, ac)
                tripped = KillSwitch.evaluate_daily_loss(
                    s, pnl, self._loss_limit_for(ac), ac
                )
                result[ac.value] = tripped
            s.commit()
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

    def available_reduce_qty(self, ticker: str, side: str) -> Decimal:
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
                    Order.status.in_(
                        (
                            OrderStatus.APPROVED.value,
                            OrderStatus.SUBMITTED.value,
                            OrderStatus.PARTIALLY_FILLED.value,
                        )
                    ),
                )
            ).scalars().all()
            quote = None
            reserved = Decimal(0)
            for order in live:
                filled_qty = sum((fill.qty for fill in order.fills), Decimal(0))
                if order.qty is not None:
                    reserved += max(order.qty - filled_qty, Decimal(0))
                elif order.notional is not None:
                    if quote is None:
                        quote = self.broker.get_quote(symbol)
                    spent = sum(
                        (fill.qty * fill.price for fill in order.fills), Decimal(0)
                    )
                    remaining = max(order.notional - spent, Decimal(0))
                    if quote.last > 0:
                        reserved += remaining / quote.last
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
        self, ticker: str, condition: dict[str, Any], action: dict[str, Any]
    ) -> dict[str, Any]:
        with self.session_factory() as s:
            rule = Rule(
                ticker=ticker.upper(),
                condition_json=json.dumps(condition),
                action_json=json.dumps(action),
                state="active",
            )
            s.add(rule)
            s.commit()
            return self._rule_dict(rule)

    def list_rules(self) -> list[dict[str, Any]]:
        with self.session_factory() as s:
            rows = s.execute(select(Rule)).scalars().all()
            return [self._rule_dict(r) for r in rows]

    def cancel_rule(self, rule_id: int) -> dict[str, Any]:
        with self.session_factory() as s:
            rule = s.get(Rule, rule_id)
            if rule is None:
                return {"rule_id": rule_id, "canceled": False, "error": "not found"}
            rule.state = "canceled"
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
            "ticker": r.ticker,
            "condition": json.loads(r.condition_json),
            "action": json.loads(r.action_json),
            "state": r.state,
        }
