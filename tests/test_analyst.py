"""LLM analyst: structured output, citation/regime requirements, earnings guard."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trading_assistant.analyst.analyst import Analyst
from trading_assistant.analyst.models import AnalystAction
from trading_assistant.assets import AssetClass
from trading_assistant.dependencies import RequiredDependencyUnavailable
from trading_assistant.signals.models import MarketFeatures, Regime

TS = datetime(2022, 6, 1, tzinfo=timezone.utc)


def _feat(**kw) -> MarketFeatures:
    base = dict(symbol="AAPL", asset_class=AssetClass.EQUITY, as_of=TS, regime=Regime.RANGING)
    base.update(kw)
    return MarketFeatures(**base)


def _backend(inp):
    block = SimpleNamespace(type="tool_use", name="submit_analysis", id="t1", input=inp)

    class B:
        def create(
            self,
            *,
            system,
            messages,
            tools,
            tool_choice=None,
            request_id,
        ):
            return SimpleNamespace(content=[block])

    return B()


_VALID = {
    "action": "buy",
    "confidence": 0.7,
    "thesis": "Oversold bounce setup in a range.",
    "cited_concepts": ["Momentum (RSI)", "Regime conditioning"],
    "regime_note": "RANGING favors mean-reversion, so oversold is actionable.",
}


def test_analyst_returns_structured_report():
    analyst = Analyst(_backend(_VALID), max_attempts=2)
    report = analyst.analyze(
        _feat(rsi_14=28),
        request_id="analyst-structured-report",
    )
    assert report.action is AnalystAction.BUY
    assert report.symbol == "AAPL"
    assert report.cited_concepts and report.regime_note


def test_analyst_passes_explicit_request_id_to_analysis_attempt():
    class CaptureBackend:
        def __init__(self):
            self.request_ids: list[str] = []

        def create(self, **kwargs):
            self.request_ids.append(kwargs["request_id"])
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="submit_analysis",
                        id="t1",
                        input=_VALID,
                    )
                ]
            )

    backend = CaptureBackend()

    Analyst(backend, max_attempts=2).analyze(
        _feat(),
        request_id="  analyst-analysis-parent  ",
    )

    assert backend.request_ids == ["analyst-analysis-parent"]


def test_missing_cited_concepts_rejected():
    bad = dict(_VALID, cited_concepts=[])
    with pytest.raises(ValidationError):
        Analyst(_backend(bad), max_attempts=2).analyze(
            _feat(),
            request_id="analyst-missing-citations",
        )


def test_earnings_in_horizon_must_be_addressed():
    analyst = Analyst(
        _backend(_VALID),
        max_attempts=2,
    )  # _VALID has no earnings_note
    with pytest.raises(ValueError):
        analyst.analyze(
            _feat(days_to_next_earnings=5),
            request_id="analyst-earnings-guard",
        )


def test_earnings_addressed_passes():
    inp = dict(_VALID, earnings_note="Earnings in 5d — reducing size to accept gap risk.")
    report = Analyst(
        _backend(inp),
        max_attempts=2,
    ).analyze(
        _feat(days_to_next_earnings=5),
        request_id="analyst-earnings-addressed",
    )
    assert report.earnings_note is not None


def test_no_tool_call_raises():
    class B:
        def create(
            self,
            *,
            system,
            messages,
            tools,
            tool_choice=None,
            request_id,
        ):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="no")])

    with pytest.raises(ValueError):
        Analyst(B(), max_attempts=2).analyze(
            _feat(),
            request_id="analyst-no-tool-call",
        )


@pytest.mark.parametrize("method", ["analyze", "analyze_plan"])
def test_analyst_types_backend_outage_without_exposing_provider_text(method):
    marker = "provider-analyst-secret"

    class B:
        def create(
            self,
            *,
            system,
            messages,
            tools,
            tool_choice=None,
            request_id,
        ):
            raise RuntimeError(marker)

    with pytest.raises(RequiredDependencyUnavailable) as failure:
        getattr(Analyst(B(), max_attempts=2), method)(
            _feat(),
            request_id=f"analyst-outage-{method}",
        )

    assert marker not in str(failure.value)


def test_analyst_uses_one_configured_structured_attempt_and_request_id():
    class InvalidPlanBackend:
        def __init__(self):
            self.request_ids: list[str] = []

        def create(
            self,
            *,
            system,
            messages,
            tools,
            tool_choice=None,
            request_id,
        ):
            self.request_ids.append(request_id)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="invalid")]
            )

    backend = InvalidPlanBackend()
    analyst = Analyst(backend, max_attempts=1)

    with pytest.raises(ValueError, match="remained invalid"):
        analyst.analyze_plan(
            _feat(),
            request_id="configured-structured-attempt",
        )

    assert backend.request_ids == ["configured-structured-attempt"]


@pytest.mark.parametrize("method", ["analyze", "analyze_plan"])
def test_analyst_requires_explicit_request_id_keyword(method):
    backend = _backend(_VALID)
    analyst = Analyst(backend, max_attempts=2)

    with pytest.raises(TypeError, match="request_id"):
        getattr(analyst, method)(_feat())


@pytest.mark.parametrize("method", ["analyze", "analyze_plan"])
@pytest.mark.parametrize(
    "request_id",
    [None, "", " ", "\t"],
    ids=["none", "empty", "space", "tab"],
)
def test_analyst_rejects_missing_request_id_before_backend(
    method,
    request_id,
):
    class CountingBackend:
        def __init__(self):
            self.calls = 0

        def create(
            self,
            *,
            system,
            messages,
            tools,
            tool_choice=None,
            request_id,
        ):
            self.calls += 1
            return SimpleNamespace(content=[])

    backend = CountingBackend()
    analyst = Analyst(backend, max_attempts=2)

    with pytest.raises(ValueError, match="request_id"):
        getattr(analyst, method)(_feat(), request_id=request_id)

    assert backend.calls == 0
