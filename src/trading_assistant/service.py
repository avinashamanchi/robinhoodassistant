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
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
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
        return realized_pnl_today(fills, asset_class=asset_class)

    def assemble_snapshot(
        self,
        session: Session,
        tickers: list[str],
        asset_class: AssetClass = AssetClass.EQUITY,
    ) -> PortfolioSnapshot:
        positions = self.broker.get_positions()
        pos_map = {p.ticker.upper(): p for p in positions}
        want = {t.upper() for t in tickers} | set(pos_map)
        quotes = {sym: self.broker.get_quote(sym) for sym in want}
        account = self.broker.get_account()
        return PortfolioSnapshot(
            positions=pos_map,
            quotes=quotes,
            buying_power=account.buying_power,
            realized_pnl_today=self._realized_pnl_today(session, asset_class),
            external_positions=self._external_positions_map(),
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
    ) -> dict[str, Any]:
        """Create a PENDING proposal after a risk pre-check. Does NOT trade.

        A rejected order is still persisted (as REJECTED with a logged reason) so
        the UI can show why. An accepted order becomes PROPOSED, awaiting human
        approval — which will re-run the risk engine at execution time (A6/Phase 3).
        """
        order_req = OrderRequest(
            ticker=ticker.upper(),
            side=OrderSide(side.lower()),
            order_type=OrderType(order_type.lower()),
            idempotency_key=uuid.uuid4().hex,
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
            snapshot = self.assemble_snapshot(s, [order.ticker], ac)
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
            snapshot = self.assemble_snapshot(s, [order_req.ticker], ac)
            result = self._risk_for(ac).check(
                order_req, snapshot,
                killswitch_tripped=KillSwitch.is_tripped(s, ac),
                market_open=self._clock_for(ac).is_open(),
            )
            if result.rejected:
                return {"executed": False, "status": "rejected", "risk_reasons": result.reasons}
            broker_result = self.broker.submit_bracket(order_req, take_profit, stop_loss)
            order = Order(
                idempotency_key=order_req.idempotency_key,
                ticker=order_req.ticker, side=order_req.side.value,
                order_type=order_req.order_type.value, qty=order_req.qty,
                notional=order_req.notional, limit_price=order_req.limit_price,
                status=OrderStatus.SUBMITTED.value,
                broker_order_id=broker_result.broker_order_id,
            )
            s.add(order)
            s.commit()
            return {"executed": True, "bracket": True, "order_id": order.id,
                    "broker_order_id": order.broker_order_id}

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
                "daemon_alive": age is not None and age < 120,
                "killswitch": {"equity": eq_tripped, "crypto": cr_tripped},
            }
        except Exception as exc:
            return {"db_ok": False, "error": type(exc).__name__}

    def panic(self) -> dict[str, Any]:
        """PANIC: cancel open orders, disable all rules, trip all kill switches.

        Idempotent — a second call is a no-op on already-flat state.
        """
        from .db.models import Rule

        with self.session_factory() as s:
            open_orders = (
                s.execute(
                    select(Order).where(
                        Order.status.in_(
                            (OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value)
                        )
                    )
                ).scalars().all()
            )
            for o in open_orders:
                if o.broker_order_id:
                    try:
                        self.broker.cancel_order(o.broker_order_id)
                    except Exception:
                        pass
                OrderStateMachine.transition(o, OrderStatus.CANCELED)

            rules = s.execute(select(Rule).where(Rule.state == "active")).scalars().all()
            for r in rules:
                r.state = "canceled"

            KillSwitch.trip(s, "panic button", AssetClass.EQUITY)
            KillSwitch.trip(s, "panic button", AssetClass.CRYPTO)
            s.add(RiskEvent(event_type="panic", reason="panic button engaged"))
            s.commit()
            return {
                "panic": True,
                "orders_canceled": len(open_orders),
                "rules_disabled": len(rules),
                "killswitches_tripped": ["equity", "crypto"],
            }

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
        synced = filled = 0
        with self.session_factory() as s:
            open_orders = s.execute(
                select(Order).where(
                    Order.status.in_(
                        (OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value)
                    ),
                    Order.broker_order_id.isnot(None),
                )
            ).scalars().all()
            for o in open_orders:
                try:
                    res = self.broker.get_order_status(o.broker_order_id)
                except Exception:
                    continue
                synced += 1
                recorded = Decimal(str(
                    s.execute(
                        select(_func.coalesce(_func.sum(Fill.qty), 0)).where(Fill.order_id == o.id)
                    ).scalar_one()
                ))
                new_qty = res.filled_qty - recorded
                if new_qty > 0 and res.avg_fill_price is not None:
                    s.add(Fill(
                        order_id=o.id, ticker=o.ticker, side=o.side,
                        qty=new_qty, price=res.avg_fill_price,
                        broker_fill_id=f"{o.broker_order_id}:{res.filled_qty}",
                    ))
                target = _STATUS_MAP.get(res.status)
                if target is not None and target.value != o.status:
                    if OrderStateMachine.can_transition(OrderStatus(o.status), target):
                        OrderStateMachine.transition(o, target)
                        if target is OrderStatus.FILLED:
                            filled += 1
            s.commit()
        return {"synced": synced, "newly_filled": filled}

    def cancel_live_order(self, order_id: int) -> dict[str, Any]:
        """Cancel a live (SUBMITTED / PARTIALLY_FILLED) order at the broker + DB."""
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
                    self.broker.cancel_order(order.broker_order_id)
                except Exception:  # broker may have already terminated it
                    pass
            OrderStateMachine.transition(order, OrderStatus.CANCELED)
            s.commit()
            return {"order_id": order_id, "status": order.status}

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
