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

from ..signals.models import MarketFeatures
from .models import AnalysisReport

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
            "regime_note": {"type": "string"},
            "earnings_note": {"type": ["string", "null"]},
            "correlation_note": {"type": ["string", "null"]},
        },
        "required": ["action", "confidence", "thesis", "cited_concepts", "regime_note"],
    },
}


class LLMBackend(Protocol):
    def create(self, *, system: str, messages: list[dict], tools: list[dict]) -> Any: ...


class Analyst:
    def __init__(self, backend: LLMBackend, model: str = "", max_tokens: int = 1024) -> None:
        self.backend = backend
        self.model = model
        self.max_tokens = max_tokens

    def _prompt(self, features: MarketFeatures, held_symbols: list[str]) -> str:
        # Exclude the raw bar list to keep the prompt small; the indicators are what
        # the analyst reasons over.
        payload = features.model_dump(mode="json", exclude={"recent_bars"})
        return (
            "Analyze these features and submit_analysis.\n"
            f"Currently held (for correlation): {held_symbols or 'none'}\n"
            f"FEATURES:\n{json.dumps(payload, indent=2, default=str)}"
        )

    def analyze(
        self, features: MarketFeatures, held_symbols: Optional[list[str]] = None
    ) -> AnalysisReport:
        resp = self.backend.create(
            system=SYSTEM_PREAMBLE,
            messages=[{"role": "user", "content": self._prompt(features, held_symbols or [])}],
            tools=[SUBMIT_TOOL],
        )
        report = self._parse(resp, features)
        self._enforce_quality(report, features)
        return report

    @staticmethod
    def _parse(resp: Any, features: MarketFeatures) -> AnalysisReport:
        for block in getattr(resp, "content", []):
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_analysis":
                data = dict(block.input)
                data["symbol"] = features.symbol
                data["as_of"] = features.as_of
                return AnalysisReport(**data)
        raise ValueError("analyst did not submit an analysis")

    @staticmethod
    def _enforce_quality(report: AnalysisReport, features: MarketFeatures) -> None:
        # Earnings inside the horizon must be addressed — silence is not allowed.
        dte = features.days_to_next_earnings
        if dte is not None and 0 <= dte <= EARNINGS_HORIZON_DAYS and not report.earnings_note:
            raise ValueError(
                f"earnings in {dte}d but the analysis did not address earnings risk"
            )
