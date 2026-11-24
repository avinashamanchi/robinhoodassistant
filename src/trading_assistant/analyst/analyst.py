"""The LLM analyst.

Interprets a MarketFeatures bundle (never computes indicators) into a structured,
cited AnalysisReport. The playbook is injected into the system prompt; a single
``submit_analysis`` tool forces structured output. The LLM backend is the same
swappable Protocol the agent uses, so tests run a scripted backend with no key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Protocol

from ..identity import canonical_request_id
from ..signals.models import MarketFeatures
from .citations import validate_source_citations
from .models import AnalysisReport
from .research import ResearchTask
from .untrusted import (
    UntrustedSummary,
    validate_summary_for_privileged_use,
)

_PLAYBOOK = (Path(__file__).resolve().parent.parent / "signals" / "playbook.md").read_text(
    encoding="utf-8"
)

EARNINGS_HORIZON_DAYS = 21
SYSTEM_PREAMBLE = (
    "You are a disciplined trading analyst. You are given deterministic, "
    "pre-computed market features — you INTERPRET them, you never recompute or "
    "estimate an indicator. Follow the playbook below. You MUST cite which playbook "
    "concepts drove your thesis, state the current regime and how it conditioned "
    "your read, address earnings if a date is within your horizon, and flag "
    "correlation with existing holdings. Submit exactly one analysis via the "
    "submit_analysis tool. HOLD is a valid, often correct answer.\n\n"
    "=== PLAYBOOK ===\n" + _PLAYBOOK
)

SUBMIT_TOOL: dict[str, Any] = {
    "name": "submit_analysis",
    "description": "Submit your structured analysis of the provided features.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "thesis": {"type": "string"},
            "cited_concepts": {"type": "array", "items": {"type": "string"}},
            "cited_source_refs": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
            "regime_note": {"type": "string"},
            "earnings_note": {"type": ["string", "null"]},
            "correlation_note": {"type": ["string", "null"]},
        },
        "required": [
            "action",
            "confidence",
            "thesis",
            "cited_concepts",
            "cited_source_refs",
            "regime_note",
        ],
    },
}


PLAN_PREAMBLE = SYSTEM_PREAMBLE + (
    "\n\n=== TRADE PLAN RULES ===\n"
    "Produce a full trade plan via submit_plan. Build the BEAR case with equal "
    "effort to the bull case. State the invalidation level (where the thesis is "
    "WRONG) BEFORE the entry logic. Entry tranches must not chase: for a long, "
    "every tranche price is at or below the current price and ordered high→low; for "
    "a short, at or above and low→high. The stop must be at least as tight as the "
    "invalidation. Recommending NO_TRADE (do nothing) is a valid, often correct "
    "output — e.g. conflicting signals in an unclear regime: return action "
    "'no_trade' with a single nominal tranche and a thesis explaining why you are "
    "standing aside. Never output position sizes — sizing is computed downstream."
)

_NUM = {"type": "number"}
SUBMIT_PLAN_TOOL: dict[str, Any] = {
    "name": "submit_plan",
    "description": "Submit your full trade plan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["buy", "sell", "hold", "no_trade"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "thesis": {"type": "string"},
            "cited_concepts": {"type": "array", "items": {"type": "string"}},
            "cited_source_refs": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
            "regime_note": {"type": "string"},
            "earnings_note": {"type": ["string", "null"]},
            "correlation_note": {"type": ["string", "null"]},
            "scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": ["bear", "base", "bull"]},
                        "price_target": _NUM, "horizon_days": {"type": "integer"},
                        "probability": _NUM,
                    },
                    "required": ["name", "price_target", "horizon_days", "probability"],
                },
            },
            "invalidation": {
                "type": "object",
                "properties": {"price_level": _NUM, "rationale": {"type": "string"}},
                "required": ["price_level", "rationale"],
            },
            "entry_plan": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["single", "ladder"]},
                    "tranches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"price_level": _NUM, "fraction": _NUM},
                            "required": ["price_level", "fraction"],
                        },
                    },
                },
                "required": ["type", "tranches"],
            },
            "exit_plan": {
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"price_level": _NUM, "fraction_to_sell": _NUM},
                            "required": ["price_level", "fraction_to_sell"],
                        },
                    },
                    "stop": _NUM,
                    "trailing_stop_pct": {"type": ["number", "null"]},
                    "time_stop_days": {"type": ["integer", "null"]},
                },
                "required": ["targets", "stop"],
            },
        },
        "required": [
            "action",
            "confidence",
            "thesis",
            "cited_concepts",
            "cited_source_refs",
            "regime_note",
            "scenarios",
            "invalidation",
            "entry_plan",
            "exit_plan",
        ],
    },
}


class LLMBackend(Protocol):
    def create(
        self, *, system: str, messages: list[dict], tools: list[dict],
        tool_choice: Optional[str] = None,
        request_id: str,
    ) -> Any: ...


class Analyst:
    def __init__(
        self, backend: LLMBackend, model: str = "", max_tokens: int = 1024,
        suppress_ranging: bool = False,
        *,
        max_attempts: int,
    ) -> None:
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts <= 0
        ):
            raise ValueError(
                "max_attempts must be a positive integer"
            )
        self.backend = backend
        self.model = model
        self.max_tokens = max_tokens
        # v2: the accuracy report showed 16.7% hits in RANGING — don't take
        # directional trades in directionless markets. Deterministic, not a number
        # we curve-fit: HOLD/NO_TRADE when the regime is RANGING.
        self.suppress_ranging = suppress_ranging
        self.max_attempts = max_attempts

    def _prompt(
        self,
        features: MarketFeatures,
        held_symbols: list[str],
        untrusted_summary: UntrustedSummary | None,
    ) -> str:
        # Exclude the raw bar list to keep the prompt small; the indicators are what
        # the analyst reasons over.
        payload = features.model_dump(mode="json", exclude={"recent_bars"})
        prompt = (
            "Analyze these features and submit_analysis.\n"
            f"Currently held (for correlation): {held_symbols or 'none'}\n"
            f"FEATURES:\n{json.dumps(payload, indent=2, default=str)}"
        )
        if untrusted_summary is not None:
            prompt += (
                "\nUNTRUSTED_SUMMARY:\n"
                + json.dumps(
                    untrusted_summary.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        return prompt

    def analyze(
        self,
        features: MarketFeatures,
        held_symbols: Optional[list[str]] = None,
        untrusted_summary: UntrustedSummary | None = None,
        *,
        request_id: str,
    ) -> AnalysisReport:
        return self._analyze(features, held_symbols, untrusted_summary, request_id, plan=False)

    def _analyze(self, features, held_symbols, untrusted_summary, request_id, *, plan):
        request_id = canonical_request_id(request_id)
        task = ResearchTask(
            self.backend, kind="plan" if plan else "report",
            request_id=request_id, max_attempts=self.max_attempts,
        )
        with task.observe():
            if untrusted_summary is not None:
                validate_summary_for_privileged_use(untrusted_summary)
            prompt = self._prompt(features, held_symbols or [], untrusted_summary)
            response = task.create(
                system=PLAN_PREAMBLE if plan else SYSTEM_PREAMBLE,
                messages=[{"role": "user", "content": prompt}],
                tools=[SUBMIT_PLAN_TOOL if plan else SUBMIT_TOOL],
                tool_choice="any",
                request_id=request_id,
            )
            task.check_cancelled()
            try:
                parse = self._parse_plan if plan else self._parse
                report = parse(response, features)
                validate_source_citations(report, untrusted_summary)
                self._enforce_quality(report, features)
            except (TypeError, ValueError):
                if plan:
                    raise ValueError("analyst plan remained invalid") from None
                raise
            return self._apply_regime_filter(report, features)

    @staticmethod
    def _parse(resp: Any, features: MarketFeatures) -> AnalysisReport:
        for block in getattr(resp, "content", []):
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_analysis":
                try:
                    data = dict(block.input)
                    data["symbol"] = features.symbol
                    data["as_of"] = features.as_of
                    return AnalysisReport(**data)
                except (TypeError, ValueError):
                    raise ValueError("analyst report remained invalid") from None
        raise ValueError("analyst did not submit an analysis")

    def analyze_plan(
        self,
        features: MarketFeatures,
        held_symbols: Optional[list[str]] = None,
        untrusted_summary: UntrustedSummary | None = None,
        *,
        request_id: str,
    ):
        """Produce a full TradePlan (scenarios, invalidation, entry ladder, exits)."""
        return self._analyze(features, held_symbols, untrusted_summary, request_id, plan=True)

    @staticmethod
    def _parse_plan(resp, features):
        from decimal import Decimal

        from .models import TradePlan

        blocks = [
            block for block in getattr(resp, "content", [])
            if getattr(block, "type", None) == "tool_use"
        ]
        if len(blocks) != 1 or blocks[0].name != "submit_plan":
            raise ValueError("analyst must submit exactly one plan")
        data = dict(blocks[0].input)
        data.update(
            symbol=features.symbol, as_of=features.as_of,
            reference_price=Decimal(str(features.last_close or 0)),
        )
        return TradePlan(**data)

    def _apply_regime_filter(self, report, features: MarketFeatures):
        """v2: force HOLD/NO_TRADE in RANGING regimes (deterministic post-filter)."""
        from ..signals.models import Regime
        from .models import AnalystAction, PlanAction, TradePlan

        if self.suppress_ranging and features.regime is Regime.RANGING:
            report.action = (
                PlanAction.NO_TRADE if isinstance(report, TradePlan) else AnalystAction.HOLD
            )
        return report

    @staticmethod
    def _enforce_quality(report: AnalysisReport, features: MarketFeatures) -> None:
        # Earnings inside the horizon must be addressed — silence is not allowed.
        dte = features.days_to_next_earnings
        if dte is not None and 0 <= dte <= EARNINGS_HORIZON_DAYS and not report.earnings_note:
            raise ValueError(
                f"earnings in {dte}d but the analysis did not address earnings risk"
            )
