"""Plan lifecycle: analyze → size → store → approve (decompose into rules) → cancel.

Approving a plan turns its SizedTradePlan into a group of PRE_APPROVED conditional
rules (entry tranches + targets + stop + trailing + time) tagged with the plan id.
With ``auto_execute_preapproved_rules`` on, the daemon runs the whole ladder and
exit sequence hands-free on Alpaca — every firing still passing the risk engine.

Promotion gate: while the analyst has <50 graded calls for an asset class, plans
for that class may be approved in PAPER mode only.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Optional

from sqlalchemy import select

from ..assets import AssetClass
from ..broker.models import OrderRequest, OrderSide, OrderType
from ..config import live_trading_enabled
from ..db.models import Rule, TradePlanRow, utcnow
from ..signals.models import MarketFeatures
from .models import PlanAction, TradePlan
from .promotion import can_promote
from .scorecard import build_scorecard
from .sizing import SizedTradePlan, size_trade
from .store import build_scorecard_from_db


def _floor(x: Decimal) -> Decimal:
    return x.to_integral_value(rounding=ROUND_DOWN)


class PlanningService:
    def __init__(
        self,
        service,
        analyst,
        feature_provider: Callable[[str], MarketFeatures],
        secrets=None,
    ) -> None:
        self.service = service
        self.analyst = analyst
        self.feature_provider = feature_provider
        self.secrets = secrets

    def _risk_cfg(self, symbol: str):
        ac = AssetClass.for_symbol(symbol)
        cfg = self.service.config.crypto_risk if ac is AssetClass.CRYPTO else self.service.config.risk
        return cfg or self.service.config.risk

    # ── analyze → size → store ─────────────────────────────────
    def analyze(self, symbol: str) -> dict[str, Any]:
        features = self.feature_provider(symbol)
        held = [p["ticker"] for p in self.service.get_external_positions().get("positions", [])]
        plan = self.analyst.analyze_plan(features, held_symbols=held)

        ac = AssetClass.for_symbol(symbol)
        with self.service.session_factory() as s:
            snapshot = self.service.assemble_snapshot(s, [symbol], ac)
        equity = self.service.broker.get_account().equity
        sized = size_trade(plan, snapshot, self._risk_cfg(symbol), equity)

        plan_id = self._store(plan, sized)
        return {"plan_id": plan_id, "plan": json.loads(plan.model_dump_json()),
                "sized": sized.to_dict()}

    def _store(self, plan: TradePlan, sized: SizedTradePlan) -> int:
        with self.service.session_factory() as s:
            row = TradePlanRow(
                symbol=plan.symbol,
                action=plan.action.value,
                status="proposed",
                plan_json=plan.model_dump_json(),
                sized_json=json.dumps(sized.to_dict()),
            )
            s.add(row)
            s.commit()
            return row.id

    # ── approve (gate + decompose into rules) ──────────────────
    def approve_plan(self, plan_id: int) -> dict[str, Any]:
        with self.service.session_factory() as s:
            row = s.get(TradePlanRow, plan_id)
            if row is None:
                return {"error": "not found"}
            if row.status != "proposed":
                return {"plan_id": plan_id, "status": row.status,
                        "error": "only proposed plans can be approved"}

            plan = TradePlan.model_validate_json(row.plan_json)
            sized = json.loads(row.sized_json)
            if plan.action not in (PlanAction.BUY, PlanAction.SELL) or Decimal(sized["total_shares"]) <= 0:
                return {"plan_id": plan_id, "error": "plan has no sized entry to approve"}

            # Promotion gate: <50 graded calls for this class -> paper mode only.
            promotable, _ = can_promote(build_scorecard_from_db(s))
            live = live_trading_enabled(self.service.config, self.secrets) if self.secrets else False
            if live and not promotable:
                return {
                    "plan_id": plan_id,
                    "error": "promotion gate: <50 graded calls — approvable in PAPER mode only",
                }

            # D4: single-tranche + single-target plans go out as a server-side
            # bracket (survives our downtime); the ladder/trailing/time cases stay
            # daemon-managed rules.
            bracket = None
            eligible = (
                self.service.config.execution.prefer_bracket_orders
                and len(plan.entry_plan.tranches) == 1
                and len(plan.exit_plan.targets) == 1
                and hasattr(self.service.broker, "submit_bracket")
                and sized["tranches"] and Decimal(sized["tranches"][0]["shares"]) > 0
            )
            if eligible:
                tr = sized["tranches"][0]
                is_long = plan.action is PlanAction.BUY
                order_req = OrderRequest(
                    ticker=plan.symbol,
                    side=OrderSide.BUY if is_long else OrderSide.SELL,
                    order_type=OrderType.LIMIT, idempotency_key=uuid.uuid4().hex,
                    qty=Decimal(tr["shares"]), limit_price=Decimal(str(tr["price_level"])),
                )
                bracket = self.service.submit_bracket_order(
                    order_req, plan.exit_plan.targets[0].price_level, plan.exit_plan.stop
                )
                rules = self._decompose(plan, sized, plan_id, exits_only=True)
            else:
                rules = self._decompose(plan, sized, plan_id)
            for r in rules:
                s.add(r)
            row.status = "approved"
            row.paper_only = not (live and promotable)
            s.commit()
            return {"plan_id": plan_id, "status": "approved",
                    "rules_created": len(rules), "paper_only": row.paper_only,
                    "bracket": bracket}

    def _decompose(
        self, plan: TradePlan, sized: dict, plan_id: int, exits_only: bool = False
    ) -> list[Rule]:
        symbol = plan.symbol
        is_long = plan.action is PlanAction.BUY
        entry_side = "buy" if is_long else "sell"
        exit_side = "sell" if is_long else "buy"
        total = Decimal(sized["total_shares"])
        rules: list[Rule] = []

        # When a bracket handles entry+target+stop, only trailing/time remain.
        for t in ([] if exits_only else sized["tranches"]):
            shares = Decimal(t["shares"])
            if shares <= 0:
                continue
            cond = ({"price_below": float(t["price_level"])} if is_long
                    else {"price_above": float(t["price_level"])})
            rules.append(Rule(
                ticker=symbol, plan_id=plan_id, kind="entry", pre_approved=True,
                fraction=Decimal(str(t["fraction"])),
                condition_json=json.dumps(cond),
                action_json=json.dumps({"side": entry_side, "qty": str(shares)}),
            ))

        if not exits_only:
            for tgt in plan.exit_plan.targets:
                qty = _floor(Decimal(str(tgt.fraction_to_sell)) * total)
                if qty <= 0:
                    continue
                cond = ({"price_above": float(tgt.price_level)} if is_long
                        else {"price_below": float(tgt.price_level)})
                rules.append(Rule(
                    ticker=symbol, plan_id=plan_id, kind="target", pre_approved=True,
                    condition_json=json.dumps(cond),
                    action_json=json.dumps({"side": exit_side, "qty": str(qty)}),
                ))

            stop_cond = ({"price_below": float(plan.exit_plan.stop)} if is_long
                         else {"price_above": float(plan.exit_plan.stop)})
            rules.append(Rule(
                ticker=symbol, plan_id=plan_id, kind="stop", pre_approved=True,
                condition_json=json.dumps(stop_cond),
                action_json=json.dumps({"side": exit_side, "qty": str(total)}),
            ))

        if plan.exit_plan.trailing_stop_pct:
            rules.append(Rule(
                ticker=symbol, plan_id=plan_id, kind="trailing", pre_approved=True,
                condition_json=json.dumps({"trailing_stop_pct": plan.exit_plan.trailing_stop_pct}),
                action_json=json.dumps({"side": exit_side, "qty": str(total)}),
            ))

        if plan.exit_plan.time_stop_days:
            rules.append(Rule(
                ticker=symbol, plan_id=plan_id, kind="time", pre_approved=True,
                deadline=utcnow() + timedelta(days=plan.exit_plan.time_stop_days),
                condition_json="{}",
                action_json=json.dumps({"side": exit_side, "qty": str(total)}),
            ))
        return rules

    # ── cancel + queries ───────────────────────────────────────
    def cancel_plan(self, plan_id: int) -> dict[str, Any]:
        with self.service.session_factory() as s:
            row = s.get(TradePlanRow, plan_id)
            if row is None:
                return {"error": "not found"}
            sibs = s.execute(
                select(Rule).where(Rule.plan_id == plan_id, Rule.state == "active")
            ).scalars().all()
            for r in sibs:
                r.state = "canceled"
            row.status = "canceled"
            s.commit()
            return {"plan_id": plan_id, "status": "canceled", "rules_canceled": len(sibs)}

    def get_plans(self) -> list[dict[str, Any]]:
        with self.service.session_factory() as s:
            rows = s.execute(select(TradePlanRow).order_by(TradePlanRow.id.desc())).scalars().all()
            return [{"plan_id": r.id, "symbol": r.symbol, "action": r.action,
                     "status": r.status, "paper_only": r.paper_only,
                     "created_at": r.created_at.isoformat()} for r in rows]

    def get_plan(self, plan_id: int) -> Optional[dict[str, Any]]:
        with self.service.session_factory() as s:
            row = s.get(TradePlanRow, plan_id)
            if row is None:
                return None
            return {
                "plan_id": row.id, "symbol": row.symbol, "status": row.status,
                "paper_only": row.paper_only,
                "plan": json.loads(row.plan_json), "sized": json.loads(row.sized_json),
            }
