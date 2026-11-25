"""Isolated structured research with the installed SDK and a synthetic HTTP transport."""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from trading_assistant.analyst.analyst import Analyst
from trading_assistant.db.models import AuditEvent, ProviderBudgetDay, ProviderReservation
from trading_assistant.dependencies import RequiredDependencyUnavailable
from trading_assistant.llm import gemini_backend
from trading_assistant.llm.base import BudgetedLLMBackend
from trading_assistant.llm.budget import BudgetLimits, ProviderBudgetExceeded, ProviderBudgetService
from trading_assistant.ops.paper_report import build_report
from trading_assistant.signals.models import Regime

from tests.test_analyst_v2 import _feat, _PLAN_INPUT

MARKER = "private-synthetic-provider-detail"
REQUEST = "research-request"
FEATURES = _feat(Regime.TRENDING_UP)


def rejection(code, status):
    return httpx.Response(code, json={"error": {"code": code, "status": status, "message": MARKER}})


def success(*, valid=True):
    plan = _PLAN_INPUT if valid else {"thesis": MARKER}
    return httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "submit_plan", "args": plan}}
        ]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4, "thoughtsTokenCount": 5},
    })


@pytest.fixture
def research(monkeypatch, session_factory):
    clients = []

    def build(script, *, calls=10, max_attempts=2):
        attempts = []
        def serve(request):
            attempts.append(request.url.host)
            item = script[len(attempts) - 1]
            if callable(item):
                return item(request)
            if isinstance(item, BaseException):
                raise item
            return item

        sync = httpx.Client(transport=httpx.MockTransport(serve))
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        clients.append((sync, async_client))
        monkeypatch.setattr(gemini_backend, "new_httpx_client", lambda *a, **kw: sync)
        monkeypatch.setattr(gemini_backend, "new_async_httpx_client", lambda *a, **kw: async_client)
        budget = ProviderBudgetService(session_factory, BudgetLimits(calls, 1_000_000, 100_000))
        backend = BudgetedLLMBackend(gemini_backend.GeminiBackend("fake", "test-model", 128),
                                     budget, provider="gemini", category="analysis", max_output_tokens=128)
        return Analyst(backend, max_attempts=max_attempts), attempts

    yield build
    for sync, async_client in clients:
        sync.close()
        asyncio.run(async_client.aclose())


def ledger(factory):
    with factory() as session:
        return session.scalars(select(ProviderReservation).order_by(ProviderReservation.created_at)).all()


def metrics(factory, **window):
    with factory() as session:
        return build_report(session, **window)["research"]


def test_sdk_rejected_then_valid_plan_meters_actual_transport_and_one_logical_task(research, session_factory):
    analyst, attempts = research([rejection(503, "UNAVAILABLE"), success()])
    plan = analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert plan.exit_plan.stop == 92
    assert len(attempts) == 2
    rows = ledger(session_factory)
    assert len({r.reservation_id for r in rows}) == 2
    assert [r.request_id for r in rows] == [REQUEST, REQUEST]
    assert [(r.category, r.input_actual, r.output_actual) for r in rows] == [
        ("analysis", 0, 0), ("analysis_retry", 3, 9)]
    summary = metrics(session_factory)
    assert summary["structured_plans"] == {"valid": 1, "started": 1, "unknown": 0, "rate": 1}
    assert summary["attempts_started"] == 2
    assert summary["task_states"] == {"completed": 1}
    assert summary["retry_attempts_started"] == 1
    assert summary["confirmed_rejections"] == {"unavailable": 1}


@pytest.mark.parametrize("reply,state,outcome", [
    (rejection(504, "DEADLINE_EXCEEDED"), "unknown", "ambiguous"),
    (httpx.Response(503, text=MARKER), "unknown", "ambiguous"),
    (httpx.ReadTimeout(MARKER), "unknown", "ambiguous"),
    (rejection(401, "UNAUTHENTICATED"), "settled", "authentication"),
    (rejection(400, "INVALID_ARGUMENT"), "settled", "invalid_request"),
    (rejection(429, "RESOURCE_EXHAUSTED"), "settled", "quota"),
    (success(valid=False), "settled", "invalid_output"),
])
def test_nonretryable_outcomes_are_sanitized(research, session_factory, reply, state, outcome):
    analyst, attempts = research([reply, success()])
    with pytest.raises((RequiredDependencyUnavailable, ValueError)) as raised:
        analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert MARKER not in str(raised.value)
    assert len(attempts) == 1
    assert [r.state for r in ledger(session_factory)] == [state]
    summary = metrics(session_factory)
    assert summary["task_outcomes"] == {outcome: 1}
    assert summary["structured_plans"]["rate"] == 0
    assert MARKER not in json.dumps(summary)
    assert REQUEST not in json.dumps(summary)
    with session_factory() as session:
        for event in session.scalars(select(AuditEvent)):
            assert MARKER not in repr(vars(event))


def test_only_one_retry_even_if_caller_allows_more(research, session_factory):
    analyst, attempts = research([rejection(503, "UNAVAILABLE")] * 3, max_attempts=99)
    with pytest.raises(RequiredDependencyUnavailable):
        analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert len(attempts) == 2
    assert metrics(session_factory)["task_outcomes"] == {"unavailable": 1}


@pytest.mark.parametrize("cap,attempt_count,reason", [(0, 0, "budget_exhausted"), (1, 1, "budget_exhausted")])
def test_global_ceiling_blocks_without_dispatch(research, session_factory, cap, attempt_count, reason):
    analyst, attempts = research([rejection(503, "UNAVAILABLE")], calls=cap)
    with pytest.raises(RequiredDependencyUnavailable):
        analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert len(attempts) == len(ledger(session_factory)) == attempt_count
    assert metrics(session_factory)["task_outcomes"] == {reason: 1}


def test_competing_retry_reservations_share_two_daily_slots(session_factory):
    budget = ProviderBudgetService(session_factory, BudgetLimits(10, 100, 100))
    barrier = threading.Barrier(6)
    def run(i):
        barrier.wait(5)
        try:
            return budget.reserve(provider="gemini", category="analysis_retry",
                                  request_id=f"concurrent-{i}", input_tokens=1, output_tokens=1)
        except ProviderBudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        allowed = [r for r in pool.map(run, range(6)) if r]
    assert len(allowed) == 2
    budget.reserve(provider="gemini", category="analysis", request_id="first-attempt", input_tokens=1, output_tokens=1)
    with session_factory() as s:
        assert s.scalar(select(ProviderBudgetDay)).calls_used == 3


def test_empty_window_is_unknown(session_factory):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=1)
    summary = metrics(session_factory, since=start, until=end)
    assert summary["structured_plans"] == {"valid": 0, "started": 0, "unknown": 0, "rate": None}
    assert summary["window"] == {"since": start.isoformat(), "until": end.isoformat()}


@pytest.mark.parametrize("when", ["before", "during_wait"])
def test_cancel_scope_stops_dispatch(research, session_factory, when):
    from trading_assistant.llm.cancellation import research_cancellation
    analyst, attempts = research([rejection(503, "UNAVAILABLE"), success()])
    with research_cancellation() as stop:
        if when == "before":
            stop.set()
        else:
            # Wait for the actual backoff entry, then cancel from its owner.
            entered = threading.Event()
            original_wait = stop.wait

            def wait(seconds):
                assert 0 < seconds <= 1
                entered.set()
                return original_wait(seconds)

            stop.wait = wait
            def cancel():
                if entered.wait(3):
                    stop.set()
            worker = threading.Thread(target=cancel)
            worker.start()
        try:
            with pytest.raises(RequiredDependencyUnavailable):
                analyst.analyze_plan(FEATURES, request_id=REQUEST)
        finally:
            if when == "during_wait":
                worker.join(4)
    assert len(attempts) == (when == "during_wait")
    assert len(ledger(session_factory)) == len(attempts)
    assert metrics(session_factory)["task_outcomes"] == {"cancelled": 1}


def test_inflight_sdk_cancellation_propagates_and_retains_reserved_usage(research, session_factory):
    analyst, attempts = research([asyncio.CancelledError(), success()])
    with pytest.raises(asyncio.CancelledError):
        analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert len(attempts) == 1
    assert [r.state for r in ledger(session_factory)] == ["unknown"]
    assert metrics(session_factory)["task_outcomes"] == {"cancelled": 1}


def test_report_uses_task_identity_and_keeps_interrupted_and_legacy_outcomes_unknown(research, session_factory):
    from trading_assistant.operations.audit import AuditRecorder, MutationContext
    analyst, _ = research([success(), success()])
    analyst.analyze_plan(FEATURES, request_id=REQUEST)
    analyst.analyze_plan(FEATURES, request_id=REQUEST)
    recorder = AuditRecorder(session_factory)
    recorder.record(MutationContext("analyst", REQUEST, "interrupted fixture"),
                    "research.task", "plan", "interrupted", "started")
    recorder.record(MutationContext("analyst", REQUEST, "unrecognized fixture"),
                    "research.task", "plan", "interrupted", MARKER)
    summary = metrics(session_factory)
    assert summary["structured_plans"] == {"valid": 2, "started": 3, "unknown": 1, "rate": None}
    assert summary["attempts_started"] == 2
    assert summary["accounted_tokens"] == {"settled_input": 6, "settled_output": 18,
                                           "unresolved_input_reserved": 0, "unresolved_output_reserved": 0}
    assert MARKER not in json.dumps(summary)
    with session_factory() as session:
        last = session.scalars(select(AuditEvent).where(AuditEvent.result_code == "completed")
                               .order_by(AuditEvent.id.desc())).first()
    # Explicit half-open end excludes the second terminal receipt.
    assert metrics(session_factory, until=last.created_at)["structured_plans"] == {
        "valid": 1, "started": 2, "unknown": 1, "rate": None}


def test_historical_usage_latch_blocks_first_research_attempt_without_refund(research, session_factory):
    earlier = datetime.now(timezone.utc) - timedelta(days=3)
    budget = ProviderBudgetService(session_factory, BudgetLimits(10, 1_000_000, 100_000))
    reservation = budget.reserve(provider="gemini", category="analysis", request_id="older",
                                 input_tokens=17, output_tokens=27, now=earlier)
    budget.mark_started(reservation.reservation_id, now=earlier)
    budget.mark_unknown(reservation.reservation_id, now=earlier)
    analyst, attempts = research([success()])
    with pytest.raises(RequiredDependencyUnavailable):
        analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert attempts == []
    assert [r.state for r in ledger(session_factory)] == ["unknown"]
    summary = metrics(session_factory)
    assert summary["task_outcomes"] == {"reconciliation_required": 1}
    assert summary["accounted_tokens"]["unresolved_input_reserved"] == 17
    assert summary["accounted_tokens"]["unresolved_output_reserved"] == 27


def test_exhausted_retry_allowance_blocks_research_retry_but_allows_first_attempt(research, session_factory):
    analyst, attempts = research([rejection(503, "UNAVAILABLE")] * 5 + [success()])
    for _ in range(3):
        with pytest.raises(RequiredDependencyUnavailable):
            analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert len(attempts) == 5
    analyst.analyze_plan(FEATURES, request_id=REQUEST)
    summary = metrics(session_factory)
    assert summary["task_outcomes"] == {"unavailable": 2, "retry_budget_exhausted": 1, "completed": 1}
    assert summary["task_states"] == {"failed": 2, "blocked": 1, "completed": 1}
    assert summary["structured_plans"] == {"valid": 1, "started": 4, "unknown": 0, "rate": 0.25}


def test_metered_report_validation_sanitizes_provider_output(research, session_factory):
    analyst, attempts = research([httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"functionCall": {"name": "submit_analysis",
                        "args": {"action": MARKER}}}]}}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
    })])
    with pytest.raises(ValueError) as raised:
        analyst.analyze(FEATURES, request_id=REQUEST)
    assert MARKER not in str(raised.value)
    assert len(attempts) == 1
    summary = metrics(session_factory)
    assert summary["structured_plans"]["started"] == 0
    assert summary["task_outcomes"] == {"invalid_output": 1}


@pytest.mark.parametrize("owner", ["http_cancel", "http_lease_loss", "daemon_timeout"])
def test_owner_cancels_research_even_when_sync_worker_outlives_wait(owner, research, session_factory, monkeypatch):
    from types import SimpleNamespace

    from trading_assistant.app import policy
    from trading_assistant.daemon.monitor import Monitor
    from trading_assistant.llm.cancellation import cancellation_event

    analyst, attempts = research([rejection(503, "UNAVAILABLE"), success()])
    entered = threading.Event()

    def worker():
        cancel = cancellation_event()
        original_wait = cancel.wait

        def wait(seconds):
            entered.set()
            return original_wait(seconds)

        cancel.wait = wait
        try:
            analyst.analyze_plan(FEATURES, request_id=REQUEST)
        except RequiredDependencyUnavailable:
            pass

    async def scenario():
        if owner == "daemon_timeout":
            monitor = SimpleNamespace(run_daily_tasks=worker, daily_task_timeout=0.4)
            await Monitor._bounded_daily_tasks(monitor)
        else:
            async def handler(_request):
                await asyncio.to_thread(worker)

            async def renewal(_app, _hold, stop, **kwargs):
                if owner == "http_lease_loss":
                    assert await asyncio.to_thread(entered.wait, 3)
                    return "lost"
                await stop.wait()

            monkeypatch.setattr(policy, "_maintain_lease", renewal)
            task = asyncio.create_task(policy._call_with_lease_renewal(
                None, None, handler, None, panic_request_id=None))
            assert await asyncio.to_thread(entered.wait, 3)
            if owner == "http_cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task

    asyncio.run(scenario())
    assert entered.is_set()
    assert len(attempts) == 1
    assert metrics(session_factory)["task_outcomes"] == {"cancelled": 1}


@pytest.mark.parametrize("reply", [rejection(404, "NOT_FOUND"), rejection(400, "FAILED_PRECONDITION")])
def test_confirmed_model_or_project_configuration_errors_do_not_retry(research, session_factory, reply):
    analyst, attempts = research([reply, success()])
    with pytest.raises(RequiredDependencyUnavailable):
        analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert len(attempts) == 1
    assert [r.state for r in ledger(session_factory)] == ["settled"]
    assert metrics(session_factory)["task_outcomes"] == {"configuration": 1}


def test_unclassified_legacy_rejection_is_sanitized_and_terminal():
    from trading_assistant.llm.base import ProviderRequestRejected
    failure = ProviderRequestRejected(MARKER)
    assert str(failure) == "provider_rejected"


def test_daemon_shutdown_stops_paid_work_before_retry(make_service, research, session_factory):
    from trading_assistant.daemon.monitor import Monitor
    from trading_assistant.llm.cancellation import cancellation_event

    analyst, attempts = research([rejection(503, "UNAVAILABLE"), success()])
    entered = threading.Event()
    finished = threading.Event()
    monitor = Monitor(make_service(), poll_interval_seconds=0.005)
    def daily():
        cancel = cancellation_event()
        wait = cancel.wait
        def waiting(seconds):
            entered.set()
            return wait(seconds)
        cancel.wait = waiting
        try:
            analyst.analyze_plan(FEATURES, request_id=REQUEST)
        except RequiredDependencyUnavailable:
            pass
        finally:
            finished.set()
    monitor.run_daily_tasks = daily

    async def scenario():
        stop = asyncio.Event()
        running = asyncio.create_task(monitor.run(stop))
        assert await asyncio.to_thread(entered.wait, 3)
        stop.set()
        await running
        assert await asyncio.to_thread(finished.wait, 3)

    asyncio.run(scenario())
    assert len(attempts) == 1
    assert metrics(session_factory)["task_outcomes"] == {"cancelled": 1}


def test_cancel_while_waiting_for_durable_start_does_not_dispatch(research, session_factory, monkeypatch):
    from trading_assistant.llm.cancellation import research_cancellation
    analyst, attempts = research([success()])
    budget = analyst.backend.budgets
    original_start = budget.mark_started
    with research_cancellation() as stop:
        def cancelled_start(*args, **kwargs):
            stop.set()
            return original_start(*args, **kwargs)
        monkeypatch.setattr(budget, "mark_started", cancelled_start)
        with pytest.raises(RequiredDependencyUnavailable):
            analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert attempts == []
    assert [r.state for r in ledger(session_factory)] == ["reserved"]
    budget.release_expired_unstarted(datetime.now(timezone.utc) + timedelta(days=1))
    assert [r.state for r in ledger(session_factory)] == ["released"]
    assert metrics(session_factory)["attempts_started"] == 0


def test_one_attempt_validation_setting_never_uses_retry_allowance(research, session_factory):
    analyst, attempts = research([rejection(503, "UNAVAILABLE"), success()], max_attempts=1)
    with pytest.raises(RequiredDependencyUnavailable):
        analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert len(attempts) == 1
    assert metrics(session_factory)["retry_attempts_started"] == 0


def test_cancellation_survives_terminal_receipt_write_failure(research, session_factory, monkeypatch):
    from trading_assistant.operations.audit import AuditRecorder
    analyst, attempts = research([asyncio.CancelledError()])
    original = AuditRecorder.record
    def write(self, context, action, kind, task_id, code):
        if code == "cancelled":
            raise RuntimeError(MARKER)
        return original(self, context, action, kind, task_id, code)
    monkeypatch.setattr(AuditRecorder, "record", write)
    with pytest.raises(asyncio.CancelledError):
        analyst.analyze_plan(FEATURES, request_id=REQUEST)
    assert len(attempts) == 1
    assert metrics(session_factory)["structured_plans"]["rate"] is None


def test_owner_cancellation_during_inflight_transport_error_keeps_attempt_unknown(
    research, session_factory,
):
    from trading_assistant.llm.cancellation import ResearchCancelled, research_cancellation

    with research_cancellation() as stop:
        def cancel_and_timeout(_request):
            stop.set()
            raise httpx.ReadTimeout(MARKER)

        analyst, attempts = research([cancel_and_timeout, success()])
        with pytest.raises(RequiredDependencyUnavailable) as error:
            analyst.analyze_plan(FEATURES, request_id=REQUEST)

    assert len(attempts) == 1
    row, = ledger(session_factory)
    assert row.state == "unknown"
    assert row.input_actual is None and row.output_actual is None
    assert row.input_reserved > 0 and row.output_reserved == 128
    assert metrics(session_factory)["task_outcomes"] == {"cancelled": 1}
    assert isinstance(error.value, ResearchCancelled)


def test_rejection_receipt_write_failure_stops_retry_and_reports_local_cause(
    research, session_factory, monkeypatch,
):
    from trading_assistant.operations.audit import AuditRecorder

    original = AuditRecorder.record
    def fail_only_rejection(self, context, action, kind, task_id, code):
        if action == "research.rejection":
            raise OSError(MARKER)
        return original(self, context, action, kind, task_id, code)

    monkeypatch.setattr(AuditRecorder, "record", fail_only_rejection)
    analyst, attempts = research([rejection(503, "UNAVAILABLE"), success()])
    with pytest.raises(RequiredDependencyUnavailable) as error:
        analyst.analyze_plan(FEATURES, request_id=REQUEST)

    assert len(attempts) == 1
    row, = ledger(session_factory)
    assert (row.state, row.input_actual, row.output_actual) == ("settled", 0, 0)
    summary = metrics(session_factory)
    assert summary["task_outcomes"] == {"receipt_unavailable": 1}
    assert summary["task_states"] == {"blocked": 1}
    assert summary["confirmed_rejections"] == {}
    assert MARKER not in json.dumps(summary) and MARKER not in str(error.value)


@pytest.mark.parametrize("tool_name", ["submit_plan", "submit_analysis"])
@pytest.mark.parametrize("summary_kind,returned_refs,valid", [
    ("none", [], True), ("facts", ["s2"], True), ("metadata", [], True),
    ("none", ["s2"], False), ("facts", ["s9"], False),
    ("facts", ["s1"], False),
])
def test_gemini_wire_citation_contract(research, session_factory, tool_name, summary_kind, returned_refs, valid):
    from trading_assistant.analyst.untrusted import UntrustedFact, UntrustedSummary
    from tests.test_untrusted_model_quarantine import ANALYSIS_INPUT, PLAN_INPUT

    summary = None
    if summary_kind != "none":
        facts = (UntrustedFact(text="Demand increased.", source_ref="s2"),) if summary_kind == "facts" else ()
        summary = UntrustedSummary(facts=facts, source_refs=("s1", "s2"), uncertainties=(), injection_flags=())
    received = []
    def respond(request):
        received.append(json.loads(request.content))
        args = dict(PLAN_INPUT if tool_name == "submit_plan" else ANALYSIS_INPUT, cited_source_refs=returned_refs)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"functionCall": {"name": tool_name, "args": args}}]}}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1},
        })

    analyst, attempts = research([respond])
    run = analyst.analyze_plan if tool_name == "submit_plan" else analyst.analyze
    if valid:
        assert run(FEATURES, request_id=REQUEST, untrusted_summary=summary).cited_source_refs == returned_refs
    else:
        with pytest.raises(ValueError):
            run(FEATURES, request_id=REQUEST, untrusted_summary=summary)
    assert len(attempts) == len(ledger(session_factory)) == 1
    body, = received
    declaration, = body["tools"][0]["functionDeclarations"]
    assert declaration["name"] == tool_name
    params = declaration["parameters"]
    assert "cited_source_refs" in params["required"]
    refs = params["properties"]["cited_source_refs"]
    prompt = body["contents"][0]["parts"][0]["text"]
    assert "cited_concepts" in prompt and "playbook" in prompt.lower()
    assert refs["description"] in prompt
    if summary is None:
        assert refs["max_items"] == 0
        assert "cited_source_refs" in prompt and "[]" in prompt
    else:
        assert refs["items"]["enum"] == ["s1", "s2"]
        assert refs["max_items"] == 2
        assert refs.get("min_items", 0) == (1 if summary.facts else 0)
        assert json.dumps(["s1", "s2"]) in refs["description"]
        if summary.facts:
            assert "at least one" in prompt and "fact" in prompt
