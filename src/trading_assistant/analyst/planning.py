"""Plan lifecycle: analyze → size → store → approve (decompose into rules) → cancel.

Approving a plan turns its SizedTradePlan into a human-gated group of typed
conditional rules (entry tranches + targets + stop + trailing + time) tagged
with the plan id. A firing creates a proposal and still requires a separate,
identified human approval.

Promotion gate: while the analyst has <50 graded calls for an asset class, plans
for that class may be approved in PAPER mode only.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Optional

from sqlalchemy import select, update

from ..assets import AssetClass
from ..config import live_trading_enabled
from ..db.models import AuditEvent, TradePlanRow, utcnow
from ..rules.models import RuleCommand
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
    def approve_plan(
        self,
        plan_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        if (
            not actor.strip()
            or not reason.strip()
            or not request_id.strip()
        ):
            raise ValueError(
                "approval actor, reason, and request_id must be non-empty"
            )
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

            claim = s.execute(
                update(TradePlanRow)
                .where(
                    TradePlanRow.id == plan_id,
                    TradePlanRow.status == "proposed",
                )
                .values(status="approving")
            )
            s.commit()
            if claim.rowcount != 1:
                current = s.get(TradePlanRow, plan_id)
                return {
                    "plan_id": plan_id,
                    "status": current.status if current else None,
                    "error": "plan approval is already in progress or complete",
                }
            s.refresh(row)

            try:
                # Automatic brackets remain disabled in this safety phase even
                # when an older config file explicitly prefers them.
                bracket = None
                rules = self._decompose(plan, sized, plan_id)
                self.service.rule_application.persist_commands(
                    s, rules, plan_id=plan_id
                )
                row.status = "approved"
                row.paper_only = not (live and promotable)
                s.add(
                    AuditEvent(
                        actor=actor,
                        action="plan.approve",
                        target_type="trade_plan",
                        target_id=str(plan_id),
                        request_id=request_id,
                        reason=reason,
                        result_code="approved",
                    )
                )
                s.commit()
                return {
                    "plan_id": plan_id,
                    "status": "approved",
                    "rules_created": len(rules),
                    "paper_only": row.paper_only,
                    "bracket": bracket,
                }
            except Exception:
                s.rollback()
                with self.service.session_factory() as recovery:
                    recovery.execute(
                        update(TradePlanRow)
                        .where(
                            TradePlanRow.id == plan_id,
                            TradePlanRow.status == "approving",
                        )
                        .values(status="proposed")
                    )
                    recovery.commit()
                raise

    def _decompose(
        self, plan: TradePlan, sized: dict, plan_id: int
    ) -> list[RuleCommand]:
        symbol = plan.symbol
        is_long = plan.action is PlanAction.BUY
        entry_side = "buy" if is_long else "sell"
        exit_side = "sell" if is_long else "buy"
        total = Decimal(sized["total_shares"])
        rules: list[RuleCommand] = []
        group_key = f"plan-{plan_id}"

        for t in sized["tranches"]:
            shares = Decimal(t["shares"])
            if shares <= 0:
                continue
            rules.append(
                RuleCommand.model_validate(
                    {
                        "ticker": symbol,
                        "kind": "entry",
                        "condition": {
                            "type": "price",
                            "direction": "below" if is_long else "above",
                            "price": t["price_level"],
                        },
                        "action": {
                            "side": entry_side,
                            "order_type": "market",
                            "qty": shares,
                        },
                        "group_key": group_key,
                        "fraction": t["fraction"],
                    }
                )
            )

        for tgt in plan.exit_plan.targets:
            qty = _floor(Decimal(str(tgt.fraction_to_sell)) * total)
            if qty <= 0:
                continue
            rules.append(
                RuleCommand.model_validate(
                    {
                        "ticker": symbol,
                        "kind": "target",
                        "condition": {
                            "type": "price",
                            "direction": "above" if is_long else "below",
                            "price": tgt.price_level,
                        },
                        "action": {
                            "side": exit_side,
                            "order_type": "market",
                            "qty": qty,
                        },
                        "group_key": group_key,
                    }
                )
            )

        rules.append(
            RuleCommand.model_validate(
                {
                    "ticker": symbol,
                    "kind": "stop",
                    "condition": {
                        "type": "price",
                        "direction": "below" if is_long else "above",
                        "price": plan.exit_plan.stop,
                    },
                    "action": {
                        "side": exit_side,
                        "order_type": "market",
                        "qty": total,
                    },
                    "group_key": group_key,
                }
            )
        )

        if plan.exit_plan.trailing_stop_pct:
            rules.append(
                RuleCommand.model_validate(
                    {
                        "ticker": symbol,
                        "kind": "trailing",
                        "condition": {
                            "type": "trailing",
                            "percent": plan.exit_plan.trailing_stop_pct,
                        },
                        "action": {
                            "side": exit_side,
                            "order_type": "market",
                            "qty": total,
                        },
                        "group_key": group_key,
                    }
                )
            )

        if plan.exit_plan.time_stop_days:
            rules.append(
                RuleCommand.model_validate(
                    {
                        "ticker": symbol,
                        "kind": "time",
                        "condition": {
                            "type": "time",
                            "deadline": utcnow()
                            + timedelta(days=plan.exit_plan.time_stop_days),
                        },
                        "action": {
                            "side": exit_side,
                            "order_type": "market",
                            "qty": total,
                        },
                        "group_key": group_key,
                    }
                )
            )
        return rules

    # ── cancel + queries ───────────────────────────────────────
    def cancel_plan(
        self,
        plan_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        actor = actor.strip()
        reason = reason.strip()
        request_id = request_id.strip()
        if not actor or not reason or not request_id:
            raise ValueError(
                "plan cancellation actor, reason, and request_id "
                "must be non-empty"
            )
        result = self.service.rule_repository.cancel_plan(
            plan_id,
            now=utcnow(),
        )
        if result.error == "not_found":
            return {"error": "not found"}
        response = {
            "plan_id": plan_id,
            "status": result.status,
            "rules_canceled": result.rules_canceled,
        }
        if result.error is not None:
            response["error"] = result.error
        with self.service.session_factory() as session:
            session.add(
                AuditEvent(
                    actor=actor,
                    action="plan.cancel",
                    target_type="trade_plan",
                    target_id=str(plan_id),
                    request_id=request_id,
                    reason=reason,
                    result_code=result.error or result.status,
                )
            )
            session.commit()
        return response

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
