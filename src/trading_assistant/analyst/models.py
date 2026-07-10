"""Typed analyst output + grade."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AnalystAction(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class AnalysisReport(BaseModel):
    """The analyst's structured interpretation of a MarketFeatures bundle.

    ``cited_concepts`` and ``regime_note`` are required — a thesis that doesn't say
    what drove it or how the regime shaped it is rejected upstream.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    as_of: datetime
    action: AnalystAction
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    cited_concepts: list[str] = Field(min_length=1)
    regime_note: str = Field(min_length=1)
    earnings_note: Optional[str] = None       # required when earnings are in-horizon
    correlation_note: Optional[str] = None
    size_hint: Optional[float] = None


class Grade(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correct: bool
    forward_return_pct: float
    rationale: str
