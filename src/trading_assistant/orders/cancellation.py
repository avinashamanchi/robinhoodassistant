"""Live-order cancellation, replacement and plan-order quiescing.

Moved verbatim from ``TradingService`` (service.py) to keep that module
focused; ``TradingService`` inherits these methods unchanged, so callers and
the public interface are the same. The methods rely on the service's own
attributes (session factory, broker, breakers, submission barrier).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..broker.models import OrderStatus
from ..db.models import (
    FILL_RECONCILIATION_REQUIRED,
    Order,
    OrderStateMachine,
    PLAN_CANCEL_INDETERMINATE,
    PLAN_CANCEL_REQUESTED,
    PLAN_CANCEL_SETTLED,
    TERMINAL_STATES,
    utcnow,
)
from ..risk.breakers import BreakerScope, trip_in_session
from ..risk.submission_barrier import serialized_writer
from ..service_support import _persist_audit, _require_mutation_context


class OrderCancellationMixin:
    """Cancellation and replacement methods of ``TradingService``."""

    def cancel_live_order(
        self,
        order_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Cancel a live (SUBMITTED / PARTIALLY_FILLED) order at the broker + DB."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        with self.submission_barrier.hold_writer():
            result = self._cancel_live_order_under_writer(
                order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
            with self.session_factory() as session:
                _persist_audit(
                        session,
                        actor=actor,
                        action="order.cancel",
                        target_type="order",
                        target_id=str(order_id),
                        request_id=request_id,
                        reason=reason,
                        result_code=(
                            "canceled"
                            if "error" not in result
                            else "cancel_failed"
                        ),
                        detail_json=json.dumps(
                            {
                                "status": result.get("status"),
                                "has_error": "error" in result,
                            },
                            sort_keys=True,
                        ),
                )
                session.commit()
            return result

    @staticmethod
    def _record_plan_cancel_state(
        session: Session,
        order: Order,
        state: str,
        *,
        actor: str,
        reason: str,
        request_id: str,
        now,
    ) -> None:
        if state not in {
            PLAN_CANCEL_REQUESTED,
            PLAN_CANCEL_INDETERMINATE,
            PLAN_CANCEL_SETTLED,
        }:
            raise ValueError("invalid durable plan cancellation state")
        if order.plan_cancel_state == state:
            return
        order.plan_cancel_state = state
        order.updated_at = now
        order.version += 1
        _persist_audit(
                session,
                actor=actor,
                action="order.plan_cancel_intent",
                target_type="order",
                target_id=str(order.id),
                request_id=request_id,
                reason=reason,
                result_code=state,
                created_at=now,
        )

    @serialized_writer
    def quiesce_trade_plan_orders(
        self,
        plan_id: int,
        *,
        entry_only: bool = False,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, int]:
        """Make every plan-linked order terminal before rule cancellation."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        canceled = 0
        failed = 0
        order_ids = (
            self.rule_repository.plan_entry_nonterminal_order_ids(
                plan_id
            )
            if entry_only
            else self.rule_repository.plan_nonterminal_order_ids(
                plan_id
            )
        )
        for order_id in order_ids:
            with self.session_factory() as session:
                order = session.get(Order, order_id)
                if order is None:
                    failed += 1
                    continue
                current = OrderStatus(order.status)
                if current in {
                    OrderStatus.PROPOSED,
                    OrderStatus.APPROVAL_RECORDED,
                }:
                    target = (
                        OrderStatus.CANCELED
                        if current is OrderStatus.PROPOSED
                        else OrderStatus.REJECTED
                    )
                    OrderStateMachine.transition(order, target)
                    order.last_error_code = "plan_cancel"
                    self._record_plan_cancel_state(
                        session,
                        order,
                        PLAN_CANCEL_SETTLED,
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                        now=utcnow(),
                    )
                    _persist_audit(
                            session,
                            actor=actor,
                            action="order.plan_cancel",
                            target_type="order",
                            target_id=str(order_id),
                            request_id=request_id,
                            reason=reason,
                            result_code=target.value,
                    )
                    session.commit()
                    canceled += 1
                    continue
                if current not in {
                    OrderStatus.SUBMITTED,
                    OrderStatus.PARTIALLY_FILLED,
                }:
                    if order.plan_cancel_state not in {
                        PLAN_CANCEL_REQUESTED,
                        PLAN_CANCEL_INDETERMINATE,
                    }:
                        self._record_plan_cancel_state(
                            session,
                            order,
                            PLAN_CANCEL_REQUESTED,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            now=utcnow(),
                        )
                    trip_in_session(
                        session,
                        BreakerScope.broker_drift(),
                        (
                            f"plan {plan_id} cancellation found "
                            f"indeterminate order {order_id} in "
                            f"state {current.value}"
                        ),
                        actor,
                        request_id=request_id,
                        audit_reason=reason,
                    )
                    session.commit()
                    failed += 1
                    continue
                order.last_error_code = "plan_cancel"
                if order.plan_cancel_state not in {
                    PLAN_CANCEL_REQUESTED,
                    PLAN_CANCEL_INDETERMINATE,
                }:
                    self._record_plan_cancel_state(
                        session,
                        order,
                        PLAN_CANCEL_REQUESTED,
                        actor=actor,
                        reason=reason,
                        request_id=request_id,
                        now=utcnow(),
                    )
                _persist_audit(
                        session,
                        actor=actor,
                        action="order.plan_broker_cancel",
                        target_type="order",
                        target_id=str(order_id),
                        request_id=request_id,
                        reason=reason,
                        result_code="requested",
                )
                session.commit()
            outcome = self._cancel_live_order_under_writer(
                order_id,
                actor=actor,
                reason=reason,
                request_id=request_id,
                settle_plan_lifecycle=False,
            )
            if "error" in outcome:
                self.breakers.trip(
                    BreakerScope.broker_drift(),
                    (
                        f"plan {plan_id} cancellation remains "
                        f"unresolved for order {order_id}"
                    ),
                    actor,
                    request_id=request_id,
                    audit_reason=reason,
                )
                failed += 1
            else:
                canceled += 1
        remaining = len(
            (
                self.rule_repository.plan_entry_nonterminal_order_ids(
                    plan_id
                )
                if entry_only
                else self.rule_repository.plan_nonterminal_order_ids(
                    plan_id
                )
            )
        )
        return {
            "canceled": canceled,
            # Every still-live row has already contributed to ``failed`` when
            # its local or broker cancellation could not be confirmed. Keep
            # the count cardinal rather than double-counting those rows.
            "failed": max(failed, remaining),
            "remaining": remaining,
        }

    @serialized_writer
    def _cancel_plan_orders_after_exit_fill(
        self,
        *,
        actor: str,
        reason: str,
        request_id: str,
    ) -> dict[str, int]:
        """Cancel broker-live entries/siblings before a filled exit closes a plan."""
        canceled = 0
        failed = 0
        seen: set[int] = set()
        while True:
            candidates = [
                order_id
                for order_id in sorted(
                    set(
                        self.rule_repository
                        .plan_order_ids_requiring_broker_cancel()
                    )
                    | set(
                        self.rule_repository
                        .plan_cancellation_intent_order_ids()
                    )
                )
                if order_id not in seen
            ]
            if not candidates:
                break
            for order_id in candidates:
                seen.add(order_id)
                with self.session_factory() as session:
                    order = session.get(Order, order_id)
                    if order is None:
                        failed += 1
                        continue
                    current = OrderStatus(order.status)
                    if (
                        current in TERMINAL_STATES
                        and order.acceptance_state
                        != FILL_RECONCILIATION_REQUIRED
                    ):
                        self._record_plan_cancel_state(
                            session,
                            order,
                            PLAN_CANCEL_SETTLED,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            now=utcnow(),
                        )
                        session.commit()
                        canceled += 1
                        continue
                    if current not in {
                        OrderStatus.SUBMITTED,
                        OrderStatus.PARTIALLY_FILLED,
                    }:
                        if order.plan_cancel_state not in {
                            PLAN_CANCEL_REQUESTED,
                            PLAN_CANCEL_INDETERMINATE,
                        }:
                            self._record_plan_cancel_state(
                                session,
                                order,
                                PLAN_CANCEL_REQUESTED,
                                actor=actor,
                                reason=reason,
                                request_id=request_id,
                                now=utcnow(),
                            )
                        trip_in_session(
                            session,
                            BreakerScope.broker_drift(),
                            (
                                "plan exit requires cancellation of "
                                f"indeterminate order {order_id} in "
                                f"state {current.value}"
                            ),
                            actor,
                            request_id=request_id,
                            audit_reason=reason,
                        )
                        session.commit()
                        failed += 1
                        continue
                    if order.last_error_code not in {
                        "plan_cancel",
                        "plan_exit_entry_cancel",
                    }:
                        order.last_error_code = (
                            "plan_exit_entry_cancel"
                        )
                    if order.plan_cancel_state not in {
                        PLAN_CANCEL_REQUESTED,
                        PLAN_CANCEL_INDETERMINATE,
                    }:
                        self._record_plan_cancel_state(
                            session,
                            order,
                            PLAN_CANCEL_REQUESTED,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            now=utcnow(),
                        )
                    _persist_audit(
                            session,
                            actor=actor,
                            action="order.plan_broker_cancel",
                            target_type="order",
                            target_id=str(order_id),
                            request_id=request_id,
                            reason=reason,
                            result_code="requested",
                    )
                    session.commit()
                outcome = self._cancel_live_order_under_writer(
                    order_id,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    settle_plan_lifecycle=False,
                )
                if "error" in outcome:
                    self.breakers.trip(
                        BreakerScope.broker_drift(),
                        (
                            "plan exit cancellation remains "
                            f"unresolved for order {order_id}"
                        ),
                        actor,
                        request_id=request_id,
                        audit_reason=reason,
                    )
                    failed += 1
                else:
                    canceled += 1
        return {"canceled": canceled, "failed": failed}

    def _cancel_live_order_under_writer(
        self,
        order_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
        settle_plan_lifecycle: bool = True,
    ) -> dict[str, Any]:
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
            broker_order_id = order.broker_order_id
            local_status = order.status
            if not broker_order_id:
                return {
                    "order_id": order_id,
                    "status": order.status,
                    "error": "live order has no broker order id",
                }

        # The process writer is already held, but this read session is closed
        # before broker I/O. Reconciliation later opens its own short write
        # transactions and commits exact fill/latch truth before writer release.
        try:
            broker_result = self.broker.cancel_order(broker_order_id)
        except Exception:
            try:
                broker_result = self.broker.get_order_status(broker_order_id)
            except Exception:
                fault_reason = (
                    "indeterminate broker cancellation for order "
                    f"{order_id}"
                )
                now = utcnow()
                # Fail closed first in an independent durable transaction. A
                # later latch/audit failure must not reopen submissions.
                self.breakers.trip(
                    BreakerScope.broker_drift(),
                    fault_reason,
                    actor,
                    now=now,
                    request_id=request_id,
                    audit_reason=reason,
                )

                # The latch and its exact provenance are one transaction. If
                # either write fails, neither may be visible.
                with self.session_factory() as session:
                    order = session.get(Order, order_id)
                    if order is None:
                        raise RuntimeError(
                            "order disappeared during cancellation latch"
                        )
                    order.acceptance_state = (
                        FILL_RECONCILIATION_REQUIRED
                    )
                    order.last_error_code = "indeterminate_cancel"
                    if order.plan_cancel_state in {
                        PLAN_CANCEL_REQUESTED,
                        PLAN_CANCEL_INDETERMINATE,
                    }:
                        self._record_plan_cancel_state(
                            session,
                            order,
                            PLAN_CANCEL_INDETERMINATE,
                            actor=actor,
                            reason=reason,
                            request_id=request_id,
                            now=now,
                        )
                    else:
                        order.updated_at = now
                        order.version += 1
                    _persist_audit(
                            session,
                            actor=actor,
                            action="order.cancel_latch",
                            target_type="order",
                            target_id=str(order_id),
                            request_id=request_id,
                            reason=reason,
                            result_code="indeterminate_cancel",
                            detail_json=json.dumps(
                                {
                                    "acceptance_state": (
                                        FILL_RECONCILIATION_REQUIRED
                                    ),
                                    "error_code": "indeterminate_cancel",
                                },
                                sort_keys=True,
                            ),
                            created_at=now,
                    )
                    session.commit()
                return {
                    "order_id": order_id,
                    "status": local_status,
                    "error": "broker cancellation could not be confirmed",
                }
        broker_status = broker_result.status
        if settle_plan_lifecycle:
            sync = self.sync_open_orders(
                actor=actor,
                reason=reason,
                request_id=request_id,
            )
        else:
            sync = self.serialize_reconciliation_report(
                self.reconciliation.reconcile(
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                )
            )
        current = self.get_order_status(order_id)
        with self.session_factory() as session:
            current_row = session.get(Order, order_id)
            exact_fill_truth_confirmed = bool(
                current_row is not None
                and current_row.acceptance_state
                != FILL_RECONCILIATION_REQUIRED
            )
            terminal_exact = bool(
                current_row is not None
                and OrderStatus(current_row.status)
                in TERMINAL_STATES
                and exact_fill_truth_confirmed
            )
            plan_cancel_settled = bool(
                terminal_exact
                and current_row is not None
                and current_row.plan_cancel_state
                in {
                    PLAN_CANCEL_REQUESTED,
                    PLAN_CANCEL_INDETERMINATE,
                }
            )
            if plan_cancel_settled:
                self._record_plan_cancel_state(
                    session,
                    current_row,
                    PLAN_CANCEL_SETTLED,
                    actor=actor,
                    reason=reason,
                    request_id=request_id,
                    now=utcnow(),
                )
                session.commit()
        if (
            broker_status is OrderStatus.CANCELED
            and current is not None
            and current["status"] == OrderStatus.CANCELED.value
            and exact_fill_truth_confirmed
        ):
            return {"order_id": order_id, "status": current["status"]}
        if (
            plan_cancel_settled
            and not settle_plan_lifecycle
            and current is not None
        ):
            return {"order_id": order_id, "status": current["status"]}
        return {
            "order_id": order_id,
            "status": current["status"] if current else None,
            "error": (
                "broker cancellation lacks exact fill confirmation"
                if not exact_fill_truth_confirmed
                else (
                    "broker order was not canceled; reported "
                    f"{broker_status.value}"
                )
            ),
            "sync": sync,
        }

    def replace_order(
        self,
        order_id: int,
        *,
        actor: str,
        reason: str,
        request_id: str,
        **new_order,
    ) -> dict[str, Any]:
        """Cancel/replace: cancel the live order, then propose a replacement."""
        actor, reason, request_id = _require_mutation_context(
            actor,
            reason,
            request_id,
        )
        cancel = self.cancel_live_order(
            order_id,
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        if "error" in cancel:
            return {"canceled": cancel, "replacement": None}
        replacement = self.propose_order(
            **new_order,
            actor=actor,
            reason=reason,
            request_id=request_id,
        )
        return {"canceled": cancel, "replacement": replacement}
