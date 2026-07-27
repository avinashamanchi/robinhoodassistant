"""Strict value models for persisted conditional-rule commands."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN, localcontext
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
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
    PENDING = "pending"
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

PersistedHighWaterMark = Annotated[
    Decimal,
    Field(gt=0, max_digits=20, decimal_places=6),
]
PersistedOrderDecimal = Annotated[
    Decimal,
    Field(gt=0, max_digits=20, decimal_places=6),
]
_HIGH_WATER_MARK_ADAPTER = TypeAdapter(PersistedHighWaterMark)
_ORDER_DECIMAL_ADAPTER = TypeAdapter(PersistedOrderDecimal)
_ORDER_DECIMAL_QUANTUM = Decimal("0.000001")


def validate_persisted_high_water_mark(value: object) -> Decimal:
    """Validate a runtime HWM against the exact persisted Numeric shape."""

    return _HIGH_WATER_MARK_ADAPTER.validate_python(value)


def normalize_computed_order_decimal(value: Decimal) -> Decimal | None:
    """Round a positive computed order value down to the persisted scale."""

    if not value.is_finite() or value <= 0:
        raise ValueError("computed order value must be finite and positive")
    _, digits, exponent = value.normalize().as_tuple()
    whole_digits = (
        len(digits) + exponent
        if exponent >= 0
        else max(len(digits) + exponent, 0)
    )
    if whole_digits > 14:
        raise ValueError("computed order value exceeds persisted precision")
    with localcontext() as context:
        context.prec = max(20, len(digits) + max(exponent, 0) + 6)
        normalized = value.quantize(
            _ORDER_DECIMAL_QUANTUM,
            rounding=ROUND_DOWN,
        )
    if normalized == 0:
        return None
    return _ORDER_DECIMAL_ADAPTER.validate_python(normalized)


class RuleAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    qty: PersistedOrderDecimal | None = None
    notional: PersistedOrderDecimal | None = None
    limit_price: PersistedOrderDecimal | None = None

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
    fraction: Decimal | None = Field(
        default=None,
        gt=0,
        le=1,
        max_digits=8,
        decimal_places=6,
    )
    high_water_mark: PersistedHighWaterMark | None = None
    activation: Literal["immediate", "on_entry_fill"] = "immediate"
    terminal_on_trigger: bool = True

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

    @field_validator("pre_approved")
    @classmethod
    def reject_preapproval(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "pre_approved=true is disabled while global auto_execute=false"
            )
        return value

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
        if (
            self.activation == "on_entry_fill"
            and self.kind
            not in {
                RuleKind.TARGET,
                RuleKind.STOP,
                RuleKind.TRAILING,
                RuleKind.TIME,
            }
        ):
            raise ValueError(
                "only exit rules may activate from confirmed entry fills"
            )
        if not self.terminal_on_trigger and self.kind is not RuleKind.TARGET:
            raise ValueError(
                "only an intermediate target may be nonterminal"
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
