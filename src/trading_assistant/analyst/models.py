"""Typed analyst output: AnalysisReport, the richer TradePlan, and Grade."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

_EPS = 1e-6


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalystAction(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class PlanAction(str, enum.Enum):
    BUY = "buy"          # enter long
    SELL = "sell"        # enter short
    HOLD = "hold"        # keep an existing position, no new entry
    NO_TRADE = "no_trade"  # do nothing — a modeled, valid recommendation


class AnalysisReport(_Model):
    """The analyst's structured interpretation of a MarketFeatures bundle.

    ``cited_concepts`` and ``regime_note`` are required — a thesis that doesn't say
    what drove it or how the regime shaped it is rejected upstream. Note: there is
    no ``size_hint`` — position sizing is deterministic code (analyst/sizing.py),
    never model output.
    """

    symbol: str
    as_of: datetime
    action: AnalystAction
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    cited_concepts: list[str] = Field(min_length=1)
    regime_note: str = Field(min_length=1)
    earnings_note: Optional[str] = None       # required when earnings are in-horizon
    correlation_note: Optional[str] = None


# ── TradePlan components ─────────────────────────────────────────
class Scenario(_Model):
    name: Literal["bear", "base", "bull"]
    price_target: Decimal
    horizon_days: int = Field(gt=0)
    probability: float = Field(ge=0.0, le=1.0)


class Tranche(_Model):
    price_level: Decimal
    fraction: float = Field(gt=0.0, le=1.0)


class Invalidation(_Model):
    price_level: Decimal
    rationale: str = Field(min_length=1)


class EntryPlan(_Model):
    type: Literal["single", "ladder"]
    tranches: list[Tranche] = Field(min_length=1, max_length=4)


class ExitTarget(_Model):
    price_level: Decimal
    fraction_to_sell: float = Field(gt=0.0, le=1.0)


class ExitPlan(_Model):
    targets: list[ExitTarget] = Field(min_length=1)
    stop: Decimal
    trailing_stop_pct: Optional[float] = Field(default=None, gt=0.0, le=100.0)
    time_stop_days: Optional[int] = Field(default=None, gt=0)


class TradePlan(AnalysisReport):
    """Full, actionable plan. Deterministic sizing is applied separately."""

    action: PlanAction  # widened to include NO_TRADE
    # reference_price is injected by the analyze flow (from the snapshot), NOT the
    # LLM — it anchors the "no chasing" tranche checks.
    reference_price: Decimal
    scenarios: list[Scenario] = Field(min_length=3, max_length=3)
    invalidation: Invalidation
    entry_plan: EntryPlan
    exit_plan: ExitPlan

    @model_validator(mode="after")
    def _check(self) -> "TradePlan":
        # Scenarios: exactly the three names, probabilities sum to 1.
        names = {s.name for s in self.scenarios}
        if names != {"bear", "base", "bull"}:
            raise ValueError("scenarios must be exactly bear, base, bull")
        if abs(sum(s.probability for s in self.scenarios) - 1.0) > 1e-3:
            raise ValueError("scenario probabilities must sum to 1.0")

        entry = self.entry_plan
        if entry.type == "ladder" and not (2 <= len(entry.tranches) <= 4):
            raise ValueError("ladder entry requires 2-4 tranches")
        if entry.type == "single" and len(entry.tranches) != 1:
            raise ValueError("single entry requires exactly 1 tranche")
        if abs(sum(t.fraction for t in entry.tranches) - 1.0) > _EPS:
            raise ValueError("entry tranche fractions must sum to 1.0")

        # No chasing + ordering, direction-aware.
        levels = [t.price_level for t in entry.tranches]
        if self.action is PlanAction.BUY:
            if any(l > self.reference_price for l in levels):
                raise ValueError("BUY tranches must be at or below current price")
            if levels != sorted(levels, reverse=True):
                raise ValueError("BUY tranches must be ordered descending")
        elif self.action is PlanAction.SELL:
            if any(l < self.reference_price for l in levels):
                raise ValueError("short SELL tranches must be at or above current price")
            if levels != sorted(levels):
                raise ValueError("short SELL tranches must be ordered ascending")

        # Exit targets total <= 1.0.
        if sum(t.fraction_to_sell for t in self.exit_plan.targets) > 1.0 + _EPS:
            raise ValueError("exit fraction_to_sell must total <= 1.0")

        # Stop at least as tight as invalidation (direction-aware).
        stop, inval = self.exit_plan.stop, self.invalidation.price_level
        if self.action is PlanAction.BUY and stop < inval:
            raise ValueError("BUY stop must be >= invalidation (equal or tighter)")
        if self.action is PlanAction.SELL and stop > inval:
            raise ValueError("short SELL stop must be <= invalidation (equal or tighter)")
        return self


class Grade(_Model):
    correct: bool
    forward_return_pct: float
    rationale: str
