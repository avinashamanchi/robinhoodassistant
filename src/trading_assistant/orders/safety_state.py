"""Truthful, category-aware enumeration of durable local safety state."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..broker.models import OrderStatus
from ..db.models import (
    FILL_RECONCILIATION_QUARANTINED,
    FILL_RECONCILIATION_REQUIRED,
    FILL_RECONCILIATION_SUPERSEDED,
    Fill,
    Order,
    Rule,
    RuleGroup,
)

_LIVE_OR_UNKNOWN_ORDER_STATUSES = (
    OrderStatus.APPROVED.value,
    OrderStatus.APPROVAL_RECORDED.value,
    OrderStatus.SUBMITTING.value,
    OrderStatus.ACCEPTANCE_UNKNOWN.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)

_SAFETY_LATCH_ERROR_CODES = (
    "broker_submission_unknown",
    "cumulative_fill_contradiction",
    "fill_quantity_exceeds_order",
    "indeterminate_cancel",
    "invalid_broker_data",
    "invalid_broker_identity",
    "invalid_cumulative_fill",
    "legacy_unidentified_fill",
    "legacy_unverified_fill",
    "remote_fill_ahead",
    "waiting_for_exact_fill",
)


@dataclass(frozen=True)
class UnsafeLocalState:
    live_or_unknown_order_ids: tuple[int, ...] = ()
    latched_order_ids: tuple[int, ...] = ()
    unsafe_fill_ids: tuple[int, ...] = ()
    active_rule_ids: tuple[int, ...] = ()
    unsafe_rule_group_ids: tuple[int, ...] = ()
    unknown_categories: tuple[str, ...] = ()

    @property
    def enumeration(self) -> str:
        return "unknown" if self.unknown_categories else "confirmed"

    @property
    def unsafe_order_ids(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                set(self.live_or_unknown_order_ids)
                | set(self.latched_order_ids)
            )
        )

    @property
    def has_unsafe_state(self) -> bool:
        return bool(
            self.unknown_categories
            or self.live_or_unknown_order_ids
            or self.latched_order_ids
            or self.unsafe_fill_ids
            or self.active_rule_ids
            or self.unsafe_rule_group_ids
        )

    def as_dict(self) -> dict[str, list[int] | list[str]]:
        return {
            "live_or_unknown_order_ids": list(
                self.live_or_unknown_order_ids
            ),
            "latched_order_ids": list(self.latched_order_ids),
            "unsafe_fill_ids": list(self.unsafe_fill_ids),
            "active_rule_ids": list(self.active_rule_ids),
            "unsafe_rule_group_ids": list(
                self.unsafe_rule_group_ids
            ),
            "unknown_categories": list(self.unknown_categories),
        }


def enumerate_unsafe_local_state(
    session_factory: sessionmaker[Session],
) -> UnsafeLocalState:
    """Enumerate each required category independently.

    A failed query never turns into an empty, confirmed category. Other
    successful categories remain available so a panic receipt preserves the
    maximum confirmed local truth without claiming completeness.
    """

    categories = (
        "live_or_unknown_orders",
        "latched_orders",
        "unsafe_fills",
        "active_rules",
        "unsafe_rule_groups",
    )
    unknown: list[str] = []
    results: dict[str, tuple[int, ...]] = {
        category: () for category in categories
    }

    try:
        with session_factory() as session:
            def query_ids(category: str, statement) -> None:
                try:
                    results[category] = tuple(
                        session.scalars(statement).all()
                    )
                except Exception:
                    unknown.append(category)

            # One read transaction gives normal panic one coherent local
            # snapshot. If a database error invalidates the transaction, each
            # affected remaining category is marked unknown rather than empty.
            query_ids(
                "live_or_unknown_orders",
                select(Order.id)
                .where(
                    Order.status.in_(
                        _LIVE_OR_UNKNOWN_ORDER_STATUSES
                    )
                )
                .order_by(Order.id),
            )
            query_ids(
                "latched_orders",
                select(Order.id)
                .where(
                    or_(
                        Order.acceptance_state
                        == FILL_RECONCILIATION_REQUIRED,
                        Order.last_error_code.in_(
                            _SAFETY_LATCH_ERROR_CODES
                        ),
                    )
                )
                .order_by(Order.id),
            )
            query_ids(
                "unsafe_fills",
                select(Fill.id)
                .where(
                    or_(
                        Fill.order_id.is_(None),
                        Fill.reconciliation_state
                        == FILL_RECONCILIATION_QUARANTINED,
                        (
                            Fill.reconciliation_state
                            != FILL_RECONCILIATION_SUPERSEDED
                        )
                        & or_(
                            Fill.broker_fill_id.is_(None),
                            func.trim(Fill.broker_fill_id) == "",
                        ),
                    )
                )
                .order_by(Fill.id),
            )
            query_ids(
                "active_rules",
                select(Rule.id)
                .where(
                    Rule.state.in_(("active", "processing"))
                )
                .order_by(Rule.id),
            )
            query_ids(
                "unsafe_rule_groups",
                select(RuleGroup.id)
                .where(
                    or_(
                        RuleGroup.state == "active",
                        RuleGroup.reconciliation_required.is_(True),
                    )
                )
                .order_by(RuleGroup.id),
            )
    except Exception:
        unknown = list(categories)

    return UnsafeLocalState(
        live_or_unknown_order_ids=results[
            "live_or_unknown_orders"
        ],
        latched_order_ids=results["latched_orders"],
        unsafe_fill_ids=results["unsafe_fills"],
        active_rule_ids=results["active_rules"],
        unsafe_rule_group_ids=results["unsafe_rule_groups"],
        unknown_categories=tuple(unknown),
    )
