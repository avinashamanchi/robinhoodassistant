"""Lease-based conditional-rule evaluator.

This worker creates proposals only. It never records approval and never calls a
broker submission method.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from trading_assistant.notifications.base import Notifier, NullNotifier
from trading_assistant.risk.staleness import is_stale

from .application import RuleApplicationService
from .models import (
    PriceCondition,
    RuleCommand,
    RuleKind,
    RuleOutcome,
    TimeCondition,
    TrailingCondition,
)
from .repository import RuleRepository, StoredRule

log = logging.getLogger(__name__)


class RuleWorker:
    def __init__(
        self,
        service,
        repository: RuleRepository,
        application: RuleApplicationService,
        notifier: Notifier | None = None,
        *,
        max_quote_age_seconds: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        quote_reader: Callable[[str], object] | None = None,
    ) -> None:
        self.service = service
        self.repository = repository
        self.application = application
        self.notifier = notifier or NullNotifier()
        self.max_quote_age_seconds = max_quote_age_seconds
        self.now = now
        self.quote_reader = quote_reader or service.broker.get_quote

    def tick(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> list[RuleOutcome]:
        actor = actor.strip()
        reason = reason.strip()
        request_id = request_id.strip()
        if not actor or not reason or not request_id:
            raise ValueError(
                "rule worker actor, reason, and request_id must be non-empty"
            )
        outcomes: list[RuleOutcome] = []
        quotes: dict[str, object] = {}
        for group_id in self.repository.active_group_ids():
            tick_now = self.now()
            lease = self.repository.lease_group(
                group_id,
                now=tick_now,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
            if lease is None:
                continue
            try:
                stored_rules = self.repository.load_rules(lease)
            except Exception:
                self.repository.release_group(
                    lease,
                    now=tick_now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                outcomes.append(
                    RuleOutcome(
                        group_id=group_id,
                        error="rule_load_failed",
                    )
                )
                continue
            if not stored_rules:
                self.repository.release_group(
                    lease,
                    now=tick_now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                continue

            high_water_marks: dict[int, Decimal] = {}
            fired: tuple[StoredRule, object, Decimal | None] | None = None
            try:
                for stored in stored_rules:
                    command = stored.command
                    if not self.service.market_is_open(command.ticker):
                        continue
                    quote = quotes.get(command.ticker)
                    if quote is None:
                        quote = self.quote_reader(command.ticker)
                        quotes[command.ticker] = quote
                    if is_stale(
                        quote.as_of,
                        now=tick_now,
                        max_age_seconds=self.max_quote_age_seconds,
                    ):
                        log.warning(
                            "rule %s skipped: quote stale (> %ss)",
                            stored.id,
                            self.max_quote_age_seconds,
                        )
                        continue
                    does_fire, new_hwm = self._fires(
                        command, quote.last, tick_now
                    )
                    if new_hwm is not None:
                        high_water_marks[stored.id] = new_hwm
                    if does_fire:
                        fired = (stored, quote, new_hwm)
                        break

                if fired is None:
                    self.repository.release_group(
                        lease,
                        now=tick_now,
                        high_water_marks=high_water_marks,
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                    )
                    continue

                stored, quote, new_hwm = fired
                outcome = self.application.propose_from_lease(
                    lease,
                    stored.id,
                    stored.command,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    now=tick_now,
                    reference_price=quote.last,
                    quote_cache=quotes,
                    high_water_mark=new_hwm,
                )
                outcomes.append(outcome)
                if outcome.proposal is not None:
                    try:
                        self.notifier.send(
                            f"Rule {stored.id} "
                            f"({stored.command.kind.value}) triggered "
                            f"on {stored.command.ticker}: proposal "
                            f"#{outcome.proposal['order_id']} "
                            f"[{outcome.proposal['status']}]"
                        )
                    except Exception:
                        log.error(
                            "rule notification failed "
                            "code=notification_failed proposal_id=%s",
                            outcome.proposal["order_id"],
                        )
            except ValueError:
                self.repository.release_group(
                    lease,
                    now=tick_now,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
                outcomes.append(
                    RuleOutcome(
                        group_id=group_id,
                        rule_id=fired[0].id if fired is not None else None,
                        error="rule_evaluation_invalid",
                    )
                )
            except Exception:
                # Leave the lease intact. If the crash was before the atomic
                # transaction, expiry permits recovery; if it was after commit,
                # the terminal group prevents a duplicate.
                log.error(
                    "rule evaluation failed "
                    "code=rule_evaluation_failed group_id=%s",
                    group_id,
                )
                outcomes.append(
                    RuleOutcome(
                        group_id=group_id,
                        rule_id=fired[0].id if fired is not None else None,
                        error="rule_evaluation_failed",
                    )
                )
        return outcomes

    @staticmethod
    def _fires(
        command: RuleCommand, last: Decimal, now: datetime
    ) -> tuple[bool, Decimal | None]:
        condition = command.condition
        if isinstance(condition, PriceCondition):
            if condition.direction == "below":
                return last < condition.price, None
            return last > condition.price, None
        if isinstance(condition, TrailingCondition):
            high_water_mark = max(
                command.high_water_mark or last,
                last,
            )
            threshold = high_water_mark * (
                Decimal(1) - condition.percent / Decimal(100)
            )
            return last <= threshold, high_water_mark
        if isinstance(condition, TimeCondition):
            deadline = condition.deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return now >= deadline, None
        raise ValueError(
            f"unsupported condition for rule kind {RuleKind(command.kind).value}"
        )
