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
from .config import AppConfig
from .db.models import (
    Fill,
    Order,
    OrderStateMachine,
    Proposal,
    RiskEvent,
    Rule,
    utcnow,
)
from .risk.clock import MarketClock
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
    ) -> None:
        self.broker = broker
        self.session_factory = session_factory
        self.config = config
        self.clock = clock
        self.risk = RiskEngine(config.risk)

    # ── snapshot assembly (A1) ─────────────────────────────────
    def _realized_pnl_today(self, session: Session) -> Decimal:
        rows = session.execute(select(Fill)).scalars().all()
        fills = [
            FillLike(r.ticker, r.side, r.qty, r.price, r.filled_at) for r in rows
        ]
        return realized_pnl_today(fills)

    def assemble_snapshot(self, session: Session, tickers: list[str]) -> PortfolioSnapshot:
        positions = self.broker.get_positions()
        pos_map = {p.ticker.upper(): p for p in positions}
        want = {t.upper() for t in tickers} | set(pos_map)
        quotes = {sym: self.broker.get_quote(sym) for sym in want}
        account = self.broker.get_account()
        return PortfolioSnapshot(
            positions=pos_map,
            quotes=quotes,
            buying_power=account.buying_power,
            realized_pnl_today=self._realized_pnl_today(session),
        )

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

        with self.session_factory() as s:
            snapshot = self.assemble_snapshot(s, [order_req.ticker])
            tripped = KillSwitch.is_tripped(s)
            market_open = self.clock.is_open()
            result = self.risk.check(
                order_req,
                snapshot,
                killswitch_tripped=tripped,
                market_open=market_open,
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
            ttl = self.config.risk.proposal_ttl_minutes
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

            s.commit()
            return {
                "order_id": order.id,
                "status": order.status,
                "approved_by_risk": result.approved,
                "risk_reasons": result.reasons,
                "executed": False,  # invariant: proposing never executes
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
