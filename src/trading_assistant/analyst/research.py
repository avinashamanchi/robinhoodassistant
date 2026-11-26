"""One logical structured analysis, including its bounded, separately metered retry."""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError
from contextlib import contextmanager, suppress
from enum import StrEnum
from uuid import uuid4

from ..dependencies import RequiredDependencyUnavailable
from ..llm.base import BudgetedLLMBackend, ProviderRequestRejected, RejectionCode
from ..llm.budget import (
    ProviderBudgetExceeded,
    ProviderBudgetUnavailable,
    ProviderReconciliationRequired,
    ResearchRetryBudgetExceeded,
)
from ..llm.cancellation import ResearchCancelled, cancellation_event
from ..operations.audit import AuditRecorder, MutationContext


class TaskOutcome(StrEnum):
    COMPLETED = "completed"
    INVALID = "invalid_output"
    CANCELLED = "cancelled"
    RECONCILIATION = "reconciliation_required"
    BUDGET = "budget_exhausted"
    RETRY_BUDGET = "retry_budget_exhausted"
    METER = "meter_unavailable"
    RECEIPT = "receipt_unavailable"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class _ReceiptUnavailable(RequiredDependencyUnavailable):
    """Local research instrumentation could not persist a receipt."""


class ResearchTask:
    def __init__(self, backend, *, kind, request_id, max_attempts):
        self.backend = backend
        self.kind = kind
        self.cancel = cancellation_event()
        metered = isinstance(backend, BudgetedLLMBackend) and backend.category == "analysis"
        self.max_attempts = min(max_attempts, 2) if metered else 1
        self.recorder = AuditRecorder(backend.budgets.session_factory) if metered else None
        self.context = MutationContext("analyst", request_id, "structured research")
        # A batched HTTP request can run several independent analyses under one parent ID.
        self.task_id = uuid4().hex

    def record(self, action, code):
        if self.recorder is not None:
            try:
                self.recorder.record(self.context, action, self.kind, self.task_id, code)
            except Exception:
                raise _ReceiptUnavailable() from None

    def check_cancelled(self):
        if self.cancel.is_set():
            raise ResearchCancelled() from None

    @contextmanager
    def observe(self):
        self.record("research.task", "started")
        try:
            self.check_cancelled()
            yield
            self.check_cancelled()
        except (asyncio.CancelledError, CancelledError, ResearchCancelled, KeyboardInterrupt):
            # A missing terminal receipt remains unknown. Preserve owner cancellation.
            with suppress(RequiredDependencyUnavailable):
                self.record("research.task", TaskOutcome.CANCELLED)
            raise
        except ProviderRequestRejected as exc:
            self.record("research.task", exc.code)
            raise RequiredDependencyUnavailable() from None
        except ProviderBudgetUnavailable as exc:
            if isinstance(exc, ResearchRetryBudgetExceeded):
                outcome = TaskOutcome.RETRY_BUDGET
            elif isinstance(exc, ProviderReconciliationRequired):
                outcome = TaskOutcome.RECONCILIATION
            elif isinstance(exc, ProviderBudgetExceeded):
                outcome = TaskOutcome.BUDGET
            else:
                outcome = TaskOutcome.METER
            self.record("research.task", outcome)
            raise RequiredDependencyUnavailable() from None
        except (ValueError, TypeError):
            self.record("research.task", TaskOutcome.INVALID)
            raise
        except _ReceiptUnavailable:
            self.record("research.task", TaskOutcome.RECEIPT)
            raise
        except RequiredDependencyUnavailable:
            self.record("research.task", TaskOutcome.AMBIGUOUS)
            raise
        except Exception:
            self.record("research.task", TaskOutcome.FAILED)
            raise RequiredDependencyUnavailable() from None
        else:
            self.record("research.task", TaskOutcome.COMPLETED)

    def create(self, **payload):
        for attempt in range(self.max_attempts):
            self.check_cancelled()
            try:
                if self.recorder is None:
                    return self.backend.create(**payload)
                return self.backend.create(
                    **payload, research_retry=attempt > 0, cancel_event=self.cancel,
                )
            except ProviderRequestRejected as exc:
                self.record("research.rejection", exc.code)
                self.check_cancelled()
                if exc.code != RejectionCode.UNAVAILABLE or attempt + 1 == self.max_attempts:
                    raise
                # At most one wait. An owner cancellation wakes the worker immediately.
                if self.cancel.wait(1.0):
                    raise ResearchCancelled() from None
            except (ProviderBudgetUnavailable, ResearchCancelled, CancelledError):
                raise
            except Exception:
                # The backend has already reconciled this attempt. Its unknown
                # usage survives even when the owner cancelled the logical task.
                self.check_cancelled()
                raise RequiredDependencyUnavailable() from None
        raise AssertionError("unreachable")
