"""Durable, fail-closed daily budgets for LLM provider attempts."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.db.models import (
    ProviderBudgetDay,
    ProviderReservation,
    utcnow,
)
from .payloads import (
    build_anthropic_payload,
    build_gemini_payload,
    build_groq_payload,
)


UTC = timezone.utc
_MILLION = Decimal(1_000_000)
_RESERVATION_STATES = frozenset(
    {"reserved", "started", "settled", "unknown", "released"}
)


class ProviderBudgetUnavailable(RuntimeError):
    """Provider metering cannot safely authorize another provider attempt."""


class ProviderBudgetExceeded(ProviderBudgetUnavailable):
    """The requested reservation would cross a hard daily ceiling."""


def _require_nonnegative_int(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_positive_int(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value


def _as_utc(value: datetime | None) -> datetime:
    current = value or utcnow()
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _rollback_quietly(session: Session) -> None:
    try:
        session.rollback()
    except Exception:
        pass


@contextmanager
def _budget_store_session(session_factory: sessionmaker[Session]):
    try:
        with session_factory() as session:
            try:
                yield session
            except (SQLAlchemyError, OSError):
                _rollback_quietly(session)
                raise
    except (SQLAlchemyError, OSError) as exc:
        raise ProviderBudgetUnavailable(
            "durable provider budget store unavailable"
        ) from exc


@dataclass(frozen=True)
class BudgetLimits:
    calls: int
    input_tokens: int
    output_tokens: int
    reservation_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        _require_nonnegative_int("calls", self.calls)
        _require_nonnegative_int("input_tokens", self.input_tokens)
        _require_nonnegative_int("output_tokens", self.output_tokens)
        _require_positive_int(
            "reservation_ttl_seconds",
            self.reservation_ttl_seconds,
        )


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    provider: str
    category: str
    request_id: str
    input_reserved: int
    output_reserved: int


@dataclass(frozen=True)
class BudgetStatus:
    provider: str
    budget_day: date
    calls_used: int
    input_tokens_used: int
    output_tokens_used: int
    calls_limit: int
    input_tokens_limit: int
    output_tokens_limit: int
    calls_remaining: int
    input_tokens_remaining: int
    output_tokens_remaining: int
    reconciliation_required: bool
    reconciliation_code: str
    estimated_usd: Decimal | None
    price_model: str = ""
    price_effective_date: date | None = None


class ProviderInputEstimator(Protocol):
    def estimate_upper_bound(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> int: ...


class _ProviderPayloadEstimator:
    def __init__(self, builder: Callable[..., dict]) -> None:
        self._builder = builder

    def estimate_upper_bound(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> int:
        payload = self._builder(
            system=system,
            messages=messages,
            tools=tools,
            conservative_tool_choice=True,
        )
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return len(serialized.encode("utf-8"))


class AnthropicInputEstimator(_ProviderPayloadEstimator):
    def __init__(self) -> None:
        super().__init__(build_anthropic_payload)


class GeminiInputEstimator(_ProviderPayloadEstimator):
    def __init__(self) -> None:
        super().__init__(build_gemini_payload)


class GroqInputEstimator(_ProviderPayloadEstimator):
    def __init__(self) -> None:
        super().__init__(build_groq_payload)


class Utf8ByteUpperBoundEstimator(AnthropicInputEstimator):
    """Compatibility estimator using the conservative Anthropic envelope."""


@dataclass(frozen=True)
class _PriceRecord:
    provider: str
    model: str
    effective_date: date
    input_rate: Decimal
    output_rate: Decimal


class ProviderBudgetService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        limits: BudgetLimits,
        prices: Mapping[str, Any] | None = None,
        *,
        clock=utcnow,
    ) -> None:
        self._session_factory = session_factory
        self.limits = limits
        self._prices = self._validate_prices(
            {} if prices is None else prices
        )
        self._clock = clock

    def reserve(
        self,
        *,
        provider: str,
        category: str,
        request_id: str,
        input_tokens: int,
        output_tokens: int,
        now: datetime | None = None,
    ) -> BudgetReservation:
        _require_text("provider", provider)
        _require_text("category", category)
        _require_text("request_id", request_id)
        _require_nonnegative_int("input_tokens", input_tokens)
        _require_nonnegative_int("output_tokens", output_tokens)
        current = _as_utc(now or self._clock())
        budget_day = current.date()
        reservation_id = uuid.uuid4().hex

        with _budget_store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self._assert_no_invalid_reservation_states(session)
            self._release_expired_unstarted(session, current)

            unresolved = session.scalar(
                select(ProviderBudgetDay)
                .where(
                    ProviderBudgetDay.provider == provider,
                    ProviderBudgetDay.reconciliation_required.is_(True),
                )
                .limit(1)
            )
            if unresolved is not None:
                self._validate_day(unresolved)
                session.commit()
                raise ProviderBudgetExceeded(
                    "provider budget reconciliation required"
                )

            day = session.get(ProviderBudgetDay, (provider, budget_day))
            if day is not None:
                self._validate_day(day)
            calls_used = day.calls_used if day is not None else 0
            input_used = day.input_tokens_used if day is not None else 0
            output_used = day.output_tokens_used if day is not None else 0
            proposed_calls = calls_used + 1
            proposed_input = input_used + input_tokens
            proposed_output = output_used + output_tokens
            if (
                proposed_calls > self.limits.calls
                or proposed_input > self.limits.input_tokens
                or proposed_output > self.limits.output_tokens
            ):
                session.commit()
                raise ProviderBudgetExceeded(
                    "provider daily budget exceeded"
                )

            if day is None:
                day = ProviderBudgetDay(
                    provider=provider,
                    budget_day=budget_day,
                    calls_used=0,
                    input_tokens_used=0,
                    output_tokens_used=0,
                    reconciliation_required=False,
                    reconciliation_code="",
                    updated_at=current,
                )
                session.add(day)
            day.calls_used = proposed_calls
            day.input_tokens_used = proposed_input
            day.output_tokens_used = proposed_output
            day.updated_at = current
            session.add(
                ProviderReservation(
                    reservation_id=reservation_id,
                    provider=provider,
                    category=category,
                    request_id=request_id,
                    budget_day=budget_day,
                    state="reserved",
                    input_reserved=input_tokens,
                    output_reserved=output_tokens,
                    created_at=current,
                    expires_at=current
                    + timedelta(
                        seconds=self.limits.reservation_ttl_seconds
                    ),
                )
            )
            session.commit()

        return BudgetReservation(
            reservation_id=reservation_id,
            provider=provider,
            category=category,
            request_id=request_id,
            input_reserved=input_tokens,
            output_reserved=output_tokens,
        )

    def mark_started(
        self,
        reservation_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        _require_text("reservation_id", reservation_id)
        current = _as_utc(now or self._clock())
        statement = (
            update(ProviderReservation)
            .where(
                ProviderReservation.reservation_id == reservation_id,
                ProviderReservation.state == "reserved",
            )
            .values(state="started", started_at=current)
        )
        with _budget_store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            result = session.execute(statement)
            if result.rowcount != 1:
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "provider reservation cannot be started"
                )
            session.commit()

    def settle(
        self,
        reservation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        now: datetime | None = None,
    ) -> None:
        _require_text("reservation_id", reservation_id)
        _require_nonnegative_int("input_tokens", input_tokens)
        _require_nonnegative_int("output_tokens", output_tokens)
        current = _as_utc(now or self._clock())

        with _budget_store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            reservation = session.get(
                ProviderReservation,
                reservation_id,
            )
            if reservation is None:
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "provider reservation cannot be settled"
                )
            self._validate_reservation(reservation)
            if reservation.state != "started":
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "provider reservation cannot be settled"
                )
            day = session.get(
                ProviderBudgetDay,
                (reservation.provider, reservation.budget_day),
            )
            if day is None:
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "provider budget day is unavailable"
                )
            self._validate_day(day)

            day.input_tokens_used = (
                day.input_tokens_used
                - reservation.input_reserved
                + input_tokens
            )
            day.output_tokens_used = (
                day.output_tokens_used
                - reservation.output_reserved
                + output_tokens
            )
            if (
                day.input_tokens_used < 0
                or day.output_tokens_used < 0
            ):
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "corrupt provider budget state"
                )
            if (
                input_tokens > reservation.input_reserved
                or output_tokens > reservation.output_reserved
            ):
                day.reconciliation_required = True
                day.reconciliation_code = (
                    "provider_usage_over_reservation"
                )
            day.updated_at = current
            reservation.input_actual = input_tokens
            reservation.output_actual = output_tokens
            reservation.state = "settled"
            reservation.settled_at = current
            session.commit()

    def mark_unknown(
        self,
        reservation_id: str,
    ) -> None:
        _require_text("reservation_id", reservation_id)
        statement = (
            update(ProviderReservation)
            .where(
                ProviderReservation.reservation_id == reservation_id,
                ProviderReservation.state == "started",
            )
            .values(state="unknown")
        )
        with _budget_store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            result = session.execute(statement)
            if result.rowcount != 1:
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "provider reservation cannot be marked unknown"
                )
            session.commit()

    def release_expired_unstarted(self, now: datetime) -> int:
        current = _as_utc(now)
        with _budget_store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self._assert_no_invalid_reservation_states(session)
            released = self._release_expired_unstarted(
                session,
                current,
            )
            session.commit()
            return released

    @staticmethod
    def _release_expired_unstarted(
        session: Session,
        current: datetime,
    ) -> int:
        reservations = session.scalars(
            select(ProviderReservation).where(
                ProviderReservation.state == "reserved",
                ProviderReservation.expires_at <= current,
            )
        ).all()
        for reservation in reservations:
            ProviderBudgetService._validate_reservation(reservation)
            day = session.get(
                ProviderBudgetDay,
                (reservation.provider, reservation.budget_day),
            )
            if day is None:
                raise ProviderBudgetUnavailable(
                    "provider budget day is unavailable"
                )
            ProviderBudgetService._validate_day(day)
            if (
                day.calls_used < 1
                or day.input_tokens_used < reservation.input_reserved
                or day.output_tokens_used < reservation.output_reserved
            ):
                raise ProviderBudgetUnavailable(
                    "corrupt provider budget state"
                )
            day.calls_used -= 1
            day.input_tokens_used -= reservation.input_reserved
            day.output_tokens_used -= reservation.output_reserved
            day.updated_at = current
            reservation.state = "released"
        return len(reservations)

    def status(
        self,
        provider: str,
        *,
        model: str | None = None,
        now: datetime | None = None,
    ) -> BudgetStatus:
        _require_text("provider", provider)
        if model is not None:
            _require_text("model", model)
        budget_day = _as_utc(now or self._clock()).date()
        with _budget_store_session(self._session_factory) as session:
            row = session.get(ProviderBudgetDay, (provider, budget_day))
            reconciliation = session.scalar(
                select(ProviderBudgetDay)
                .where(
                    ProviderBudgetDay.provider == provider,
                    ProviderBudgetDay.reconciliation_required.is_(True),
                )
                .order_by(ProviderBudgetDay.budget_day)
                .limit(1)
            )
            if row is not None:
                self._validate_day(row)
            if reconciliation is not None:
                self._validate_day(reconciliation)

        calls_used = row.calls_used if row is not None else 0
        input_used = row.input_tokens_used if row is not None else 0
        output_used = row.output_tokens_used if row is not None else 0
        price = self._effective_price(
            provider,
            model=model,
            budget_day=budget_day,
        )
        estimated_usd = None
        price_model = ""
        price_effective_date = None
        if price is not None:
            price_model = price.model
            price_effective_date = price.effective_date
            estimated_usd = (
                Decimal(input_used) * price.input_rate
                + Decimal(output_used) * price.output_rate
            ) / _MILLION

        reconciled_row = reconciliation or (
            row
            if row is not None and row.reconciliation_required
            else None
        )
        return BudgetStatus(
            provider=provider,
            budget_day=budget_day,
            calls_used=calls_used,
            input_tokens_used=input_used,
            output_tokens_used=output_used,
            calls_limit=self.limits.calls,
            input_tokens_limit=self.limits.input_tokens,
            output_tokens_limit=self.limits.output_tokens,
            calls_remaining=max(0, self.limits.calls - calls_used),
            input_tokens_remaining=max(
                0,
                self.limits.input_tokens - input_used,
            ),
            output_tokens_remaining=max(
                0,
                self.limits.output_tokens - output_used,
            ),
            reconciliation_required=reconciled_row is not None,
            reconciliation_code=(
                reconciled_row.reconciliation_code
                if reconciled_row is not None
                else ""
            ),
            estimated_usd=estimated_usd,
            price_model=price_model,
            price_effective_date=price_effective_date,
        )

    def _effective_price(
        self,
        provider: str,
        *,
        model: str | None,
        budget_day: date,
    ) -> _PriceRecord | None:
        candidates = [
            price
            for price in self._prices
            if price.provider == provider
            and price.effective_date <= budget_day
            and (model is None or price.model == model)
        ]

        if not candidates:
            return None
        if model is None and len({item.model for item in candidates}) != 1:
            return None
        return max(
            candidates,
            key=lambda item: item.effective_date,
        )

    @staticmethod
    def _metadata_value(metadata: Any, name: str) -> Any:
        if isinstance(metadata, Mapping):
            return metadata.get(name)
        return getattr(metadata, name, None)

    @classmethod
    def _validate_prices(
        cls,
        prices: Mapping[str, Any],
    ) -> tuple[_PriceRecord, ...]:
        if not isinstance(prices, Mapping):
            raise ValueError("provider prices must be a mapping")
        records: list[_PriceRecord] = []
        identities: set[tuple[str, str, date]] = set()
        for key, metadata in prices.items():
            if not isinstance(key, str):
                raise ValueError("provider price key must be a string")
            provider, separator, label = key.partition(":")
            if (
                not separator
                or not provider.strip()
                or not label.strip()
                or provider != provider.strip()
                or label != label.strip()
            ):
                raise ValueError("provider price key must be provider:label")
            model = cls._metadata_value(metadata, "model")
            effective_date = cls._metadata_value(
                metadata,
                "effective_date",
            )
            input_rate = cls._metadata_value(
                metadata,
                "input_usd_per_million",
            )
            output_rate = cls._metadata_value(
                metadata,
                "output_usd_per_million",
            )
            if (
                not isinstance(model, str)
                or not model.strip()
                or model != model.strip()
            ):
                raise ValueError("provider price model must be non-empty")
            if type(effective_date) is not date:
                raise ValueError(
                    "provider price effective_date must be a date"
                )
            for name, rate in (
                ("input", input_rate),
                ("output", output_rate),
            ):
                if (
                    type(rate) is not Decimal
                    or not rate.is_finite()
                    or rate < 0
                ):
                    raise ValueError(
                        f"provider price {name} rate must be a "
                        "finite non-negative Decimal"
                    )
            identity = (provider, model, effective_date)
            if identity in identities:
                raise ValueError(
                    "provider price metadata must be unique by "
                    "provider, model, and effective_date"
                )
            identities.add(identity)
            records.append(
                _PriceRecord(
                    provider=provider,
                    model=model,
                    effective_date=effective_date,
                    input_rate=input_rate,
                    output_rate=output_rate,
                )
            )
        return tuple(records)

    @staticmethod
    def _validate_day(day: ProviderBudgetDay) -> None:
        if (
            not isinstance(day.provider, str)
            or not day.provider.strip()
            or type(day.budget_day) is not date
            or type(day.calls_used) is not int
            or day.calls_used < 0
            or type(day.input_tokens_used) is not int
            or day.input_tokens_used < 0
            or type(day.output_tokens_used) is not int
            or day.output_tokens_used < 0
            or type(day.reconciliation_required) is not bool
            or not isinstance(day.reconciliation_code, str)
        ):
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )
        if day.reconciliation_required != bool(
            day.reconciliation_code
        ):
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )

    @staticmethod
    def _validate_reservation(
        reservation: ProviderReservation,
    ) -> None:
        actuals_valid = all(
            value is None
            or (type(value) is int and value >= 0)
            for value in (
                reservation.input_actual,
                reservation.output_actual,
            )
        )
        if (
            not isinstance(reservation.reservation_id, str)
            or not reservation.reservation_id.strip()
            or not isinstance(reservation.provider, str)
            or not reservation.provider.strip()
            or not isinstance(reservation.category, str)
            or not reservation.category.strip()
            or not isinstance(reservation.request_id, str)
            or not reservation.request_id.strip()
            or type(reservation.budget_day) is not date
            or reservation.state not in _RESERVATION_STATES
            or type(reservation.input_reserved) is not int
            or reservation.input_reserved < 0
            or type(reservation.output_reserved) is not int
            or reservation.output_reserved < 0
            or not actuals_valid
        ):
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )
        if reservation.state == "reserved" and (
            reservation.started_at is not None
            or reservation.settled_at is not None
            or reservation.input_actual is not None
            or reservation.output_actual is not None
        ):
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )
        if reservation.state in {"started", "unknown"} and (
            reservation.started_at is None
            or reservation.settled_at is not None
            or reservation.input_actual is not None
            or reservation.output_actual is not None
        ):
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )
        if reservation.state == "settled" and (
            reservation.started_at is None
            or reservation.settled_at is None
            or reservation.input_actual is None
            or reservation.output_actual is None
        ):
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )
        if reservation.state == "released" and (
            reservation.started_at is not None
            or reservation.settled_at is not None
            or reservation.input_actual is not None
            or reservation.output_actual is not None
        ):
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )

    @staticmethod
    def _assert_no_invalid_reservation_states(
        session: Session,
    ) -> None:
        invalid = session.scalar(
            select(ProviderReservation.reservation_id)
            .where(
                or_(
                    ProviderReservation.state.is_(None),
                    ProviderReservation.state.not_in(
                        _RESERVATION_STATES
                    ),
                    ProviderReservation.input_reserved < 0,
                    ProviderReservation.output_reserved < 0,
                    ProviderReservation.input_actual < 0,
                    ProviderReservation.output_actual < 0,
                )
            )
            .limit(1)
        )
        if invalid is not None:
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )
