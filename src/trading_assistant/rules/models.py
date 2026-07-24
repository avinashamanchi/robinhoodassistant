"""Strict value models for persisted conditional-rule commands."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class RuleKind(str, Enum):
    PRICE = "price"
    ENTRY = "entry"
    TARGET = "target"
    STOP = "stop"
    TRAILING = "trailing"
    TIME = "time"


class RuleState(str, Enum):
    ACTIVE = "active"
    PROCESSING = "processing"
    TRIGGERED = "triggered"
    CANCELED = "canceled"
    FAILED = "failed"


class PriceCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["price"]
    direction: Literal["below", "above"]
    price: Decimal = Field(gt=0)


class TrailingCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["trailing"]
    percent: Decimal = Field(gt=0, le=100)


class TimeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["time"]
    deadline: datetime


RuleCondition = Annotated[
    PriceCondition | TrailingCondition | TimeCondition,
    Field(discriminator="type"),
]


class RuleAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    qty: Decimal | None = Field(default=None, gt=0)
    notional: Decimal | None = Field(default=None, gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_shape(self) -> "RuleAction":
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional is required")
        if (self.order_type == "limit") != (self.limit_price is not None):
            raise ValueError("limit_price must be present only for limit orders")
        return self


class RuleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1, max_length=16)
    kind: RuleKind
    condition: RuleCondition
    action: RuleAction
    group_key: str | None = Field(default=None, min_length=1, max_length=128)
    pre_approved: bool = False
    fraction: Decimal | None = Field(default=None, gt=0, le=1)
    high_water_mark: Decimal | None = Field(default=None, gt=0)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("ticker must be non-empty")
        return normalized

    @field_validator("group_key")
    @classmethod
    def normalize_group_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("group_key must be non-empty")
        return normalized

    @model_validator(mode="after")
    def validate_condition_matches_kind(self) -> "RuleCommand":
        expected = {
            RuleKind.TRAILING: "trailing",
            RuleKind.TIME: "time",
        }.get(self.kind, "price")
        if self.condition.type != expected:
            raise ValueError(
                f"condition type {self.condition.type!r} is invalid for "
                f"rule kind {self.kind.value!r}"
            )
        return self


class RuleOutcome(BaseModel):
    """One worker decision. ``executed`` is always ``None`` in this phase."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    group_id: int
    rule_id: int | None = None
    proposal: dict[str, Any] | None = None
    executed: None = None
    oco_canceled: int = 0
    error: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)
