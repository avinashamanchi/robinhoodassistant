"""Application boundary for validated rule persistence and proposal creation."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable, Mapping, MutableMapping, TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_assistant.broker.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trading_assistant.db.models import (
    Order,
    Proposal,
    RiskEvent,
    Rule,
    RuleGroup,
)

from .models import (
    normalize_computed_order_decimal,
    RuleAction,
    RuleCommand,
    RuleKind,
    RuleOutcome,
    RuleState,
)
from .repository import RuleGroupLease, RuleRepository

if TYPE_CHECKING:
    from trading_assistant.service import TradingService


class RuleApplicationService:
    """The only runtime boundary allowed to persist or fire rule commands."""

    def __init__(
        self,
        service: "TradingService",
        repository: RuleRepository,
        *,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.service = service
        self.repository = repository
        self.crash_hook = crash_hook

    @staticmethod
    def _validated(command: RuleCommand | dict) -> RuleCommand:
        validated = RuleCommand.model_validate(
            command.model_dump(mode="python")
            if isinstance(command, RuleCommand)
            else command
        )
        if validated.pre_approved:
            raise ValueError(
                "pre_approved=true is disabled while global auto_execute=false"
            )
        return validated

    def create_rule(
        self, command: RuleCommand | dict, *, plan_id: int | None = None
    ) -> int:
        with self.service.session_factory() as session:
            rows = self.persist_commands(session, [command], plan_id=plan_id)
            session.commit()
            return rows[0].id

    def persist_commands(
        self,
        session: Session,
        commands: Iterable[RuleCommand | dict],
        *,
        plan_id: int | None = None,
    ) -> list[Rule]:
        validated = [self._validated(command) for command in commands]
        if not validated:
            return []

        generated_group_key = f"rule-{uuid.uuid4().hex}"
        groups: dict[str, RuleGroup] = {}
        rows: list[Rule] = []
        for command in validated:
            group_key = command.group_key or generated_group_key
            group = groups.get(group_key)
            if group is None:
                group = session.scalar(
                    select(RuleGroup).where(RuleGroup.group_key == group_key)
                )
                if group is None:
                    group = RuleGroup(group_key=group_key)
                    session.add(group)
                    session.flush()
                elif group.state != RuleState.ACTIVE.value:
                    raise ValueError(
                        f"rule group {group_key!r} is not active"
                    )
                groups[group_key] = group

            payload = command.model_dump(mode="json")
            row = Rule(
                group_id=group.id,
                payload_version=1,
                ticker=command.ticker,
                condition_json=json.dumps(
                    payload["condition"], separators=(",", ":"), sort_keys=True
                ),
                action_json=json.dumps(
                    payload["action"], separators=(",", ":"), sort_keys=True
                ),
                state=RuleState.ACTIVE.value,
                plan_id=plan_id,
                kind=command.kind.value,
                fraction=command.fraction,
                hwm=command.high_water_mark,
                deadline=(
                    command.condition.deadline
                    if command.kind is RuleKind.TIME
                    else None
                ),
                pre_approved=command.pre_approved,
            )
            session.add(row)
            session.flush()
            rows.append(row)
        return rows

    def propose_from_lease(
        self,
        lease: RuleGroupLease,
        rule_id: int,
        command: RuleCommand | dict,
        *,
        now: datetime | None = None,
        reference_price: Decimal | None = None,
        reference_quote=None,
        quote_overrides: Mapping[str, object] | None = None,
        quote_cache: MutableMapping[str, object] | None = None,
        high_water_mark: Decimal | None = None,
    ) -> RuleOutcome:
        now = now or datetime.now(timezone.utc)
        validated = self._validated(command)
        stored = self.repository.load_rule(lease, rule_id)
        if stored is None:
            return RuleOutcome(
                group_id=lease.group_id,
                rule_id=rule_id,
                error="lease_conflict",
            )
        if stored.command != validated:
            raise ValueError("rule command does not match validated persisted payload")

        action = self._bounded_action(validated, reference_price)
        if action is None:
            self.repository.release_group(lease, now=now)
            return RuleOutcome(
                group_id=lease.group_id,
                rule_id=rule_id,
                error="no unreserved position to exit",
            )

        request = OrderRequest(
            ticker=validated.ticker,
            side=OrderSide(action.side),
            order_type=OrderType(action.order_type),
            idempotency_key=f"rule-group-{lease.group_id}",
            qty=action.qty,
            notional=action.notional,
            limit_price=action.limit_price,
        )
        asset_class = self.service._asset_class(request.ticker)
        snapshot_quote_overrides = dict(quote_overrides or {})
        if quote_cache is not None:
            for symbol, quote in quote_cache.items():
                snapshot_quote_overrides.setdefault(symbol, quote)
        if reference_quote is not None:
            snapshot_quote_overrides.setdefault(request.ticker, reference_quote)
        with self.service.session_factory() as read_session:
            snapshot = self.service.assemble_snapshot(
                read_session,
                [request.ticker],
                asset_class,
                quote_overrides=snapshot_quote_overrides or None,
            )
            if quote_cache is not None:
                for symbol, quote in snapshot.quotes.items():
                    quote_cache.setdefault(symbol.upper(), quote)
            risk = self.service._risk_for(asset_class).check(
                request,
                snapshot,
                killswitch_tripped=self.service._risk_is_blocked(
                    read_session, asset_class
                ),
                market_open=self.service._clock_for(asset_class).is_open(),
            )

        if self.crash_hook is not None:
            self.crash_hook("before_transaction")

        with self.service.session_factory() as session:
            sibling_count = session.scalar(
                select(func.count())
                .select_from(Rule)
                .where(
                    Rule.group_id == lease.group_id,
                    Rule.id != rule_id,
                    Rule.state.in_(
                        (RuleState.ACTIVE.value, RuleState.PROCESSING.value)
                    ),
                )
            )
            terminal_state = (
                RuleState.FAILED if risk.rejected else RuleState.TRIGGERED
            )
            if not self.repository.claim_terminal(
                lease,
                rule_id,
                now=now,
                terminal_state=terminal_state,
                high_water_mark=high_water_mark,
                session=session,
            ):
                return RuleOutcome(
                    group_id=lease.group_id,
                    rule_id=rule_id,
                    error="lease_conflict",
                )

            order = Order(
                idempotency_key=request.idempotency_key,
                ticker=request.ticker,
                side=request.side.value,
                order_type=request.order_type.value,
                qty=request.qty,
                notional=request.notional,
                limit_price=request.limit_price,
                status=(
                    OrderStatus.REJECTED.value
                    if risk.rejected
                    else OrderStatus.PROPOSED.value
                ),
            )
            session.add(order)
            session.flush()
            risk_config = (
                self.service.config.crypto_risk
                if asset_class.value == "crypto"
                else self.service.config.risk
            )
            ttl = (risk_config or self.service.config.risk).proposal_ttl_minutes
            session.add(
                Proposal(
                    order_id=order.id,
                    source_rule_group_id=lease.group_id,
                    ttl_minutes=ttl,
                    created_at=now,
                    expires_at=now + timedelta(minutes=ttl),
                )
            )
            for reason in risk.reasons:
                session.add(
                    RiskEvent(
                        order_id=order.id,
                        event_type="rejection",
                        reason=reason,
                    )
                )
            for warning in risk.warnings:
                session.add(
                    RiskEvent(
                        order_id=order.id,
                        event_type="warning",
                        reason=warning,
                    )
                )
            session.commit()
            proposal = {
                "order_id": order.id,
                "status": order.status,
                "approved_by_risk": risk.approved,
                "risk_reasons": list(risk.reasons),
                "risk_warnings": list(risk.warnings),
                "executed": False,
            }

        if self.crash_hook is not None:
            self.crash_hook("after_transaction")
        return RuleOutcome(
            group_id=lease.group_id,
            rule_id=rule_id,
            proposal=proposal,
            oco_canceled=int(sibling_count or 0),
        )

    def _bounded_action(
        self, command: RuleCommand, reference_price: Decimal | None
    ) -> RuleAction | None:
        action = command.action
        if command.kind not in {
            RuleKind.TARGET,
            RuleKind.STOP,
            RuleKind.TRAILING,
            RuleKind.TIME,
        }:
            return action

        available = self.service.available_reduce_qty(
            command.ticker,
            action.side,
            reference_price=reference_price,
        )
        if available <= 0:
            return None
        if action.qty is not None:
            qty = normalize_computed_order_decimal(min(action.qty, available))
            if qty is None:
                return None
            return RuleAction.model_validate(
                {**action.model_dump(mode="python"), "qty": qty}
            )

        price = reference_price
        if price is None:
            price = self.service.broker.get_quote(command.ticker).last
        if price <= 0:
            raise ValueError("reference price must be positive")
        assert action.notional is not None
        qty = normalize_computed_order_decimal(
            min(action.notional / price, available)
        )
        if qty is None:
            return None
        return RuleAction.model_validate(
            {
                **action.model_dump(mode="python"),
                "qty": qty,
                "notional": None,
            }
        )
