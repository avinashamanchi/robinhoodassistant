"""Aggregate fixed research receipt codes and durable provider attempt counts."""
from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select

from ..analyst.research import TaskOutcome
from ..db.models import AuditEvent, ProviderReservation, utcnow
from ..llm.base import RejectionCode
from ..llm.budget import RESEARCH_RETRY_CATEGORY


def research_metrics(session, *, since=None, until=None):
    end = until or utcnow()
    # The cohort is selected by task start. A missing terminal receipt (including
    # a task crossing the end boundary) is unknown. A batch request ID is not a task.
    receipts = select(
        AuditEvent.target_id, AuditEvent.target_type, AuditEvent.action, AuditEvent.result_code,
    ).where(
        AuditEvent.actor == "analyst",
        AuditEvent.action.in_(("research.task", "research.rejection")),
        AuditEvent.target_type.in_(("plan", "report")),
        AuditEvent.created_at < end,
    ).order_by(AuditEvent.id)
    if since is not None:
        receipts = receipts.where(AuditEvent.created_at >= since)
    outcomes = {}
    kinds = {}
    rejections = Counter()
    rejection_codes = set(RejectionCode)
    final_codes = set(TaskOutcome) | rejection_codes
    for task_id, kind, action, code in session.execute(receipts):
        if action == "research.rejection":
            if code in rejection_codes:
                rejections[code] += 1
        elif code == "started":
            kinds[task_id] = kind
            outcomes[task_id] = "unknown"
        elif task_id in kinds and code in final_codes:
            outcomes[task_id] = code
    task_states = Counter()
    for outcome in outcomes.values():
        if outcome in {"unknown", TaskOutcome.COMPLETED, TaskOutcome.CANCELLED}:
            state = outcome
        elif outcome in {TaskOutcome.BUDGET, TaskOutcome.RETRY_BUDGET,
                         TaskOutcome.METER, TaskOutcome.RECONCILIATION, TaskOutcome.RECEIPT}:
            state = "blocked"
        else:
            state = "failed"
        task_states[state] += 1
    total_tasks = len(kinds)
    rates_known = total_tasks > 0 and task_states["unknown"] == 0
    completed = task_states["completed"]
    failed = task_states["failed"]
    plans = Counter(outcomes[task_id] for task_id in kinds if kinds[task_id] == "plan")
    started, valid, unknown = plans.total(), plans["completed"], plans["unknown"]

    # Attempt accounting includes earlier releases' analysis reservations, even
    # when logical outcomes were not yet instrumented. Use durable start time.
    attempts = select(
        ProviderReservation.category, ProviderReservation.state, func.count(),
        func.sum(ProviderReservation.input_actual), func.sum(ProviderReservation.output_actual),
        func.sum(ProviderReservation.input_reserved), func.sum(ProviderReservation.output_reserved),
    ).where(
        ProviderReservation.category.in_(("analysis", RESEARCH_RETRY_CATEGORY)),
        ProviderReservation.started_at < end,
    )
    if since is not None:
        attempts = attempts.where(ProviderReservation.started_at >= since)
    states = Counter()
    retries = 0
    tokens = {"settled_input": 0, "settled_output": 0,
              "unresolved_input_reserved": 0, "unresolved_output_reserved": 0}
    for category, state, count, in_actual, out_actual, in_reserved, out_reserved in session.execute(
        attempts.group_by(ProviderReservation.category, ProviderReservation.state)
    ):
        states[state] += count
        if category == RESEARCH_RETRY_CATEGORY:
            retries += count
        if state == "settled":
            tokens["settled_input"] += in_actual
            tokens["settled_output"] += out_actual
        elif state in {"unknown", "started"}:
            tokens["unresolved_input_reserved"] += in_reserved
            tokens["unresolved_output_reserved"] += out_reserved
    return {
        "window": {"since": since.isoformat() if since else None, "until": end.isoformat()},
        "denominator": "All observed plan tasks started in [since, until), including blocked and cancelled tasks.",
        "coverage": "An open start includes all recorded history. Earlier uninstrumented task outcomes are unknown.",
        "attempt_basis": "Durable dispatch starts in the window, with current ledger states. "
                         "Settled token counts can include audited conservative charges.",
        "logical_tasks_started": total_tasks,
        "logical_task_rates": {
            "denominator": total_tasks,
            "denominator_basis": "All observed plan and report tasks started in [since, until), "
                                 "including blocked and cancelled tasks. Rates are unknown for "
                                 "an empty cohort or any missing terminal outcome.",
            "completed": completed,
            "failed": failed,
            "completion_rate": completed / total_tasks if rates_known else None,
            "failure_rate": failed / total_tasks if rates_known else None,
        },
        "task_states": dict(task_states),
        "task_outcomes": dict(Counter(outcomes.values())),
        "structured_plans": {"valid": valid, "started": started, "unknown": unknown,
                             "rate": valid / started if started and not unknown else None},
        "accounted_tokens": tokens,
        "attempts_started": states.total(),
        "retry_attempts_started": retries,
        "attempt_states": dict(states),
        "confirmed_rejections": dict(rejections),
    }
