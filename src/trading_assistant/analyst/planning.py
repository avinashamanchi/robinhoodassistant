"""Plan lifecycle: analyze → size → store → approve (decompose into rules) → cancel.

Approving a plan turns its SizedTradePlan into a human-gated group of typed
conditional rules (entry tranches + targets + stop + trailing + time) tagged
with the plan id. A firing creates a proposal and still requires a separate,
identified human approval.

Promotion gate: while the analyst has <50 graded calls for an asset class, plans
for that class may be approved in PAPER mode only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Optional

from sqlalchemy import select, update

from ..assets import AssetClass
from ..config import live_trading_enabled
from ..db.models import AuditEvent, TradePlanRow, utcnow
from ..dependencies import RequiredDependencyUnavailable
from ..identity import canonical_request_id
from ..security.sensitive_fields import sensitive_store
from ..rules.models import RuleCommand
from ..signals.models import MarketFeatures
from .models import PlanAction, TradePlan
from .promotion import can_promote
from .scorecard import build_scorecard
from .sizing import SizedTradePlan, size_trade
from .store import build_scorecard_from_db
from .untrusted import UntrustedSummary


def _floor(x: Decimal) -> Decimal:
    return x.to_integral_value(rounding=ROUND_DOWN)


_AUTHORITY_VERSION = 1


def _canonical_number(value: object) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("plan authority number invalid")
    normalized = format(number.normalize(), "f")
    return "0" if Decimal(normalized) == 0 else normalized


def _authority_payload(plan: TradePlan, sized: dict[str, Any]) -> dict:
    return {
        "action": plan.action.value,
        "entry_type": plan.entry_plan.type,
        "exit": {
            "stop": _canonical_number(plan.exit_plan.stop),
            "targets": [
                {
                    "fraction": _canonical_number(
                        target.fraction_to_sell
                    ),
                    "price": _canonical_number(target.price_level),
                }
                for target in plan.exit_plan.targets
            ],
            "time_stop_days": plan.exit_plan.time_stop_days,
            "trailing_stop_pct": (
                _canonical_number(plan.exit_plan.trailing_stop_pct)
                if plan.exit_plan.trailing_stop_pct is not None
                else None
            ),
        },
        "sized": {
            "direction": str(sized.get("direction", "")),
            "total_shares": _canonical_number(sized["total_shares"]),
            "tranches": [
                {
                    "fraction": _canonical_number(tranche["fraction"]),
                    "price": _canonical_number(
                        tranche["price_level"]
                    ),
                    "shares": _canonical_number(tranche["shares"]),
                }
                for tranche in sized["tranches"]
            ],
        },
        "symbol": plan.symbol,
        "version": _AUTHORITY_VERSION,
    }


def _authority_digest(plan: TradePlan, sized: dict[str, Any]) -> str:
    encoded = json.dumps(
        _authority_payload(plan, sized),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_token(plan_id: int, version: int, digest: str) -> str:
    return f"plan:{plan_id}:authority:v{version}:{digest}"


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
    def analyze(
        self,
        symbol: str,
        *,
        actor: str,
        reason: str,
        request_id: str,
        untrusted_summary: UntrustedSummary | None = None,
    ) -> dict[str, Any]:
        actor = actor.strip()
        reason = reason.strip()
        request_id = canonical_request_id(request_id)
        if not actor or not reason:
            raise ValueError(
                "plan analysis actor, reason, and request_id must be non-empty"
            )
        features = self.feature_provider(symbol)
        held = [p["ticker"] for p in self.service.get_external_positions().get("positions", [])]
        plan = self.analyst.analyze_plan(
            features,
            held_symbols=held,
            untrusted_summary=untrusted_summary,
            request_id=request_id,
        )

        ac = AssetClass.for_symbol(symbol)
        with self.service.session_factory() as s:
            snapshot = self.service.assemble_snapshot(
                s,
                [symbol],
                ac,
                required_dependencies=True,
            )
        equity = snapshot.account_equity
        sized = size_trade(plan, snapshot, self._risk_cfg(symbol), equity)

        plan_id, authority_digest = self._store(
            plan,
            sized,
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        return {
            "plan_id": plan_id,
            "plan": json.loads(plan.model_dump_json()),
            "sized": sized.to_dict(),
            "authority_version": _AUTHORITY_VERSION,
            "authority_digest": authority_digest,
            "review_token": _review_token(
                plan_id,
                _AUTHORITY_VERSION,
                authority_digest,
            ),
        }

    def _store(
        self,
        plan: TradePlan,
        sized: SizedTradePlan,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> tuple[int, str]:
        sized_payload = sized.to_dict()
        authority_digest = _authority_digest(plan, sized_payload)
        with self.service.session_factory() as s:
            row = TradePlanRow(
                symbol=plan.symbol,
                action=plan.action.value,
                status="proposed",
                authority_version=_AUTHORITY_VERSION,
                authority_digest=authority_digest,
            )
            store = sensitive_store(s, self.service.session_factory)
            store.write_many(
                row,
                {
                    "plan_json": plan.model_dump_json(),
                    "sized_json": json.dumps(sized_payload),
                },
            )
            audit = AuditEvent(
                actor=actor,
                action="plan.create",
                target_type="trade_plan",
                target_id=str(row.id),
                request_id=request_id,
                result_code="proposed",
            )
            store.write_many(
                audit,
                {
                    "reason": reason,
                    "detail_json": json.dumps(
                        {"symbol": plan.symbol},
                        sort_keys=True,
                    ),
                },
            )
            s.commit()
            return row.id, authority_digest

    # ── approve (gate + decompose into rules) ──────────────────
    def approve_plan(
        self,
        plan_id: int,
        *,
        review_token: str,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        actor = actor.strip()
        reason = reason.strip()
        request_id = canonical_request_id(request_id)
        if not actor or not reason:
            raise ValueError(
                "approval actor, reason, and request_id must be non-empty"
            )
        if not isinstance(review_token, str) or not review_token:
            return {"plan_id": plan_id, "error": "plan_review_stale"}
        with self.service.session_factory() as s:
            row = s.get(TradePlanRow, plan_id)
            if row is None:
                return {"error": "not found"}
            if row.status != "proposed":
                return {"plan_id": plan_id, "status": row.status,
                        "error": "only proposed plans can be approved"}
            if (
                row.authority_version != _AUTHORITY_VERSION
                or not isinstance(row.authority_digest, str)
                or not hmac.compare_digest(
                    review_token,
                    _review_token(
                        row.id,
                        row.authority_version,
                        row.authority_digest,
                    ),
                )
            ):
                return {
                    "plan_id": plan_id,
                    "error": "plan_review_stale",
                }

            store = sensitive_store(s, self.service.session_factory)
            try:
                plan = TradePlan.model_validate_json(
                    store.read(row, "plan_json")
                )
                sized = json.loads(store.read(row, "sized_json"))
                observed_digest = _authority_digest(plan, sized)
            except (ArithmeticError, KeyError, TypeError, ValueError):
                return {
                    "plan_id": plan_id,
                    "error": "plan_review_stale",
                }
            if not hmac.compare_digest(
                observed_digest,
                row.authority_digest,
            ):
                return {
                    "plan_id": plan_id,
                    "error": "plan_review_stale",
                }
            reviewed_digest = row.authority_digest
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

        # Decomposition is pure application work. It happens before the write
        # transaction so a process stop cannot leave a durable claim behind.
        rules = self._decompose(plan, sized, plan_id)
        paper_only = not (live and promotable)
        bracket = None
        with self.service.submission_barrier.hold_writer():
            with self.service.session_factory() as s:
                connection = s.connection()
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                current = s.get(TradePlanRow, plan_id)
                if (
                    current is None
                    or current.status != "proposed"
                    or current.authority_version != _AUTHORITY_VERSION
                    or current.authority_digest != reviewed_digest
                ):
                    s.rollback()
                    return {
                        "plan_id": plan_id,
                        "status": (
                            current.status if current else None
                        ),
                        "error": "plan_review_stale",
                    }
                current_store = sensitive_store(
                    s,
                    self.service.session_factory,
                )
                try:
                    current_plan = TradePlan.model_validate_json(
                        current_store.read(current, "plan_json")
                    )
                    current_sized = json.loads(
                        current_store.read(current, "sized_json")
                    )
                    current_digest = _authority_digest(
                        current_plan,
                        current_sized,
                    )
                except (ArithmeticError, KeyError, TypeError, ValueError):
                    s.rollback()
                    return {
                        "plan_id": plan_id,
                        "error": "plan_review_stale",
                    }
                if not hmac.compare_digest(
                    current_digest,
                    reviewed_digest,
                ):
                    s.rollback()
                    return {
                        "plan_id": plan_id,
                        "error": "plan_review_stale",
                    }
                claim = s.execute(
                    update(TradePlanRow)
                    .where(
                        TradePlanRow.id == plan_id,
                        TradePlanRow.status == "proposed",
                        TradePlanRow.authority_version
                        == _AUTHORITY_VERSION,
                        TradePlanRow.authority_digest
                        == reviewed_digest,
                    )
                    .values(
                        status="approved",
                        paper_only=paper_only,
                    )
                )
                if claim.rowcount != 1:
                    s.rollback()
                    current = s.get(TradePlanRow, plan_id)
                    return {
                        "plan_id": plan_id,
                        "status": (
                            current.status if current else None
                        ),
                        "error": (
                            "plan approval is already complete "
                            "or unavailable"
                        ),
                    }
                self.service.rule_application.persist_commands(
                    s,
                    rules,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    plan_id=plan_id,
                )
                audit = AuditEvent(
                    actor=actor,
                    action="plan.approve",
                    target_type="trade_plan",
                    target_id=str(plan_id),
                    request_id=request_id,
                    result_code="approved",
                )
                sensitive_store(
                    s,
                    self.service.session_factory,
                ).write_many(
                    audit,
                    {"reason": reason, "detail_json": "{}"},
                )
                s.commit()
                return {
                    "plan_id": plan_id,
                    "status": "approved",
                    "rules_created": len(rules),
                    "paper_only": paper_only,
                    "bracket": bracket,
                }

    def _decompose(
        self, plan: TradePlan, sized: dict, plan_id: int
    ) -> list[RuleCommand]:
        symbol = plan.symbol
        is_long = plan.action is PlanAction.BUY
        entry_side = "buy" if is_long else "sell"
        exit_side = "sell" if is_long else "buy"
        total = Decimal(sized["total_shares"])
        rules: list[RuleCommand] = []
        exit_group_key = f"plan-{plan_id}-exits"

        for index, t in enumerate(sized["tranches"], start=1):
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
                        "group_key": f"plan-{plan_id}-entry-{index}",
                        "fraction": t["fraction"],
                    }
                )
            )

        cumulative_fraction = Decimal(0)
        allocated_target_qty = Decimal(0)
        for tgt in plan.exit_plan.targets:
            fraction = Decimal(str(tgt.fraction_to_sell))
            cumulative_fraction += fraction
            terminal_on_trigger = cumulative_fraction >= Decimal(1)
            qty = (
                total - allocated_target_qty
                if terminal_on_trigger
                else _floor(fraction * total)
            )
            if qty <= 0:
                continue
            allocated_target_qty += qty
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
                        "group_key": exit_group_key,
                        "fraction": fraction,
                        "activation": "on_entry_fill",
                        "terminal_on_trigger": terminal_on_trigger,
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
                    "group_key": exit_group_key,
                    "fraction": Decimal(1),
                    "activation": "on_entry_fill",
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
                        "group_key": exit_group_key,
                        "fraction": Decimal(1),
                        "activation": "on_entry_fill",
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
                        "group_key": exit_group_key,
                        "fraction": Decimal(1),
                        "activation": "on_entry_fill",
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
        request_id = canonical_request_id(request_id)
        if not actor or not reason:
            raise ValueError(
                "plan cancellation actor, reason, and request_id "
                "must be non-empty"
            )
        with self.service.submission_barrier.hold_writer():
            blocker = (
                self.service.rule_repository.plan_cancellation_blocker(
                    plan_id,
                    now=utcnow(),
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
            )
            if (
                blocker is not None
                and blocker.error == "reconciliation_required"
            ):
                try:
                    self.service.sync_open_orders(
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                    )
                except RequiredDependencyUnavailable:
                    return {
                        "plan_id": plan_id,
                        "status": blocker.status,
                        "rules_canceled": 0,
                        "error": "order_cancel_unconfirmed",
                    }
                blocker = (
                    self.service.rule_repository.plan_cancellation_blocker(
                        plan_id,
                        now=utcnow(),
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                    )
                )
            if blocker is not None:
                if blocker.error == "not_found":
                    return {"error": "not found"}
                return {
                    "plan_id": plan_id,
                    "status": blocker.status,
                    "rules_canceled": 0,
                    "error": blocker.error,
                }
            quiesced = self.service.quiesce_trade_plan_orders(
                plan_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
            if quiesced["failed"]:
                with self.service.session_factory() as session:
                    plan = session.get(TradePlanRow, plan_id)
                return {
                    "plan_id": plan_id,
                    "status": (
                        plan.status if plan is not None else "unknown"
                    ),
                    "rules_canceled": 0,
                    "error": "order_cancel_unconfirmed",
                }
            result = self.service.rule_repository.cancel_plan(
                plan_id,
                now=utcnow(),
                actor=actor,
                reason=reason,
                request_id=request_id,
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
        return response

    def get_plans(self) -> list[dict[str, Any]]:
        with self.service.session_factory() as s:
            rows = s.execute(select(TradePlanRow).order_by(TradePlanRow.id.desc())).scalars().all()
            return [{"plan_id": r.id, "symbol": r.symbol, "action": r.action,
                     "status": r.status, "paper_only": r.paper_only,
                     "authority_version": r.authority_version,
                     "authority_digest": r.authority_digest,
                     "review_token": (
                         _review_token(
                             r.id,
                             r.authority_version,
                             r.authority_digest,
                         )
                         if r.authority_version == _AUTHORITY_VERSION
                         and isinstance(r.authority_digest, str)
                         else None
                     ),
                     "created_at": r.created_at.isoformat()} for r in rows]

    def get_plan(self, plan_id: int) -> Optional[dict[str, Any]]:
        with self.service.session_factory() as s:
            row = s.get(TradePlanRow, plan_id)
            if row is None:
                return None
            return {
                "plan_id": row.id, "symbol": row.symbol, "status": row.status,
                "paper_only": row.paper_only,
                "authority_version": row.authority_version,
                "authority_digest": row.authority_digest,
                "review_token": (
                    _review_token(
                        row.id,
                        row.authority_version,
                        row.authority_digest,
                    )
                    if row.authority_version == _AUTHORITY_VERSION
                    and isinstance(row.authority_digest, str)
                    else None
                ),
                "plan": json.loads(
                    sensitive_store(s).read(row, "plan_json")
                ),
                "sized": json.loads(
                    sensitive_store(s).read(row, "sized_json")
                ),
            }
