"""Durable, fail-closed daily budgets for LLM provider attempts."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading_assistant.db.models import (
    ProviderBudgetDay,
    ProviderReservation,
    utcnow,
)
from trading_assistant.identity import canonical_request_id
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
_USAGE_OVERRUN_CODE = "provider_usage_over_reservation"


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


def _is_aware_datetime(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


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
        tool_choices = ("auto", "any") if tools else (None,)
        return max(
            len(
                json.dumps(
                    self._builder(
                        system=system,
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                    ),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            for tool_choice in tool_choices
        )


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

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

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
        request_id = canonical_request_id(request_id)
        _require_nonnegative_int("input_tokens", input_tokens)
        _require_nonnegative_int("output_tokens", output_tokens)
        current = _as_utc(now or self._clock())
        budget_day = current.date()
        reservation_id = uuid.uuid4().hex

        with _budget_store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self._release_expired_unstarted(
                session,
                current,
                provider=provider,
            )

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
            session.flush()
            self._validate_provider_aggregates(session, provider)
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
        with _budget_store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            reservation = session.get(
                ProviderReservation,
                reservation_id,
            )
            if reservation is None:
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "provider reservation cannot be started"
                )
            self._validate_reservation(reservation)
            self._validate_provider_aggregates(
                session,
                reservation.provider,
            )
            if reservation.state != "reserved":
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "provider reservation cannot be started"
                )
            reservation.state = "started"
            reservation.started_at = current
            session.flush()
            self._validate_provider_aggregates(
                session,
                reservation.provider,
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
            self._validate_provider_aggregates(
                session,
                reservation.provider,
            )
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
                day.reconciliation_code = _USAGE_OVERRUN_CODE
            day.updated_at = current
            reservation.input_actual = input_tokens
            reservation.output_actual = output_tokens
            reservation.state = "settled"
            reservation.settled_at = current
            session.flush()
            self._validate_provider_aggregates(
                session,
                reservation.provider,
            )
            session.commit()

    def mark_unknown(
        self,
        reservation_id: str,
    ) -> None:
        _require_text("reservation_id", reservation_id)
        with _budget_store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            reservation = session.get(
                ProviderReservation,
                reservation_id,
            )
            if reservation is None:
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "provider reservation cannot be marked unknown"
                )
            self._validate_reservation(reservation)
            self._validate_provider_aggregates(
                session,
                reservation.provider,
            )
            if reservation.state != "started":
                session.rollback()
                raise ProviderBudgetUnavailable(
                    "provider reservation cannot be marked unknown"
                )
            reservation.state = "unknown"
            session.flush()
            self._validate_provider_aggregates(
                session,
                reservation.provider,
            )
            session.commit()

    def release_expired_unstarted(self, now: datetime) -> int:
        current = _as_utc(now)
        with _budget_store_session(self._session_factory) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            released = self._release_expired_unstarted(
                session,
                current,
            )
            session.commit()
            return released

    @classmethod
    def _release_expired_unstarted(
        cls,
        session: Session,
        current: datetime,
        *,
        provider: str | None = None,
    ) -> int:
        if provider is None:
            cls._validate_all_aggregates(session)
        else:
            cls._validate_provider_aggregates(session, provider)
        statement = select(ProviderReservation).where(
            ProviderReservation.state == "reserved",
            ProviderReservation.expires_at <= current,
        )
        if provider is not None:
            statement = statement.where(
                ProviderReservation.provider == provider
            )
        reservations = session.scalars(statement).all()
        affected_providers: set[str] = set()
        for reservation in reservations:
            cls._validate_reservation(reservation)
            day = session.get(
                ProviderBudgetDay,
                (reservation.provider, reservation.budget_day),
            )
            if day is None:
                raise ProviderBudgetUnavailable(
                    "provider budget day is unavailable"
                )
            day.calls_used -= 1
            day.input_tokens_used -= reservation.input_reserved
            day.output_tokens_used -= reservation.output_reserved
            day.updated_at = current
            reservation.state = "released"
            affected_providers.add(reservation.provider)
        session.flush()
        for affected_provider in affected_providers:
            cls._validate_provider_aggregates(
                session,
                affected_provider,
            )
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
            self._validate_provider_aggregates(session, provider)
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
            or not _is_aware_datetime(day.updated_at)
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
        try:
            stored_request_id = canonical_request_id(
                reservation.request_id
            )
        except ValueError:
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            ) from None
        actuals_valid = all(
            value is None
            or (type(value) is int and value >= 0)
            for value in (
                reservation.input_actual,
                reservation.output_actual,
            )
        )
        timestamps_valid = (
            _is_aware_datetime(reservation.created_at)
            and _is_aware_datetime(reservation.expires_at)
            and (
                reservation.started_at is None
                or _is_aware_datetime(reservation.started_at)
            )
            and (
                reservation.settled_at is None
                or _is_aware_datetime(reservation.settled_at)
            )
        )
        if (
            not isinstance(reservation.reservation_id, str)
            or not reservation.reservation_id.strip()
            or not isinstance(reservation.provider, str)
            or not reservation.provider.strip()
            or not isinstance(reservation.category, str)
            or not reservation.category.strip()
            or stored_request_id != reservation.request_id
            or type(reservation.budget_day) is not date
            or reservation.state not in _RESERVATION_STATES
            or type(reservation.input_reserved) is not int
            or reservation.input_reserved < 0
            or type(reservation.output_reserved) is not int
            or reservation.output_reserved < 0
            or not actuals_valid
            or not timestamps_valid
        ):
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )
        created_at = _as_utc(reservation.created_at)
        expires_at = _as_utc(reservation.expires_at)
        if (
            reservation.budget_day != created_at.date()
            or expires_at <= created_at
            or (
                reservation.started_at is not None
                and _as_utc(reservation.started_at) < created_at
            )
            or (
                reservation.settled_at is not None
                and (
                    reservation.started_at is None
                    or _as_utc(reservation.settled_at)
                    < _as_utc(reservation.started_at)
                )
            )
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

    @classmethod
    def _validate_provider_aggregates(
        cls,
        session: Session,
        provider: str,
    ) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ProviderBudgetUnavailable(
                "corrupt provider budget state"
            )
        days = session.scalars(
            select(ProviderBudgetDay).where(
                ProviderBudgetDay.provider == provider
            )
        ).all()
        reservations = session.scalars(
            select(ProviderReservation).where(
                ProviderReservation.provider == provider
            )
        ).all()
        days_by_date: dict[date, ProviderBudgetDay] = {}
        expected: dict[date, list[int]] = {}
        overruns: dict[date, bool] = {}
        for day in days:
            cls._validate_day(day)
            if day.provider != provider or day.budget_day in days_by_date:
                raise ProviderBudgetUnavailable(
                    "corrupt provider budget state"
                )
            days_by_date[day.budget_day] = day
            expected[day.budget_day] = [0, 0, 0]
            overruns[day.budget_day] = False

        for reservation in reservations:
            cls._validate_reservation(reservation)
            day = days_by_date.get(reservation.budget_day)
            if (
                reservation.provider != provider
                or day is None
                or day.provider != reservation.provider
                or day.budget_day != reservation.budget_day
            ):
                raise ProviderBudgetUnavailable(
                    "corrupt provider budget state"
                )
            if reservation.state == "released":
                continue
            totals = expected[reservation.budget_day]
            totals[0] += 1
            if reservation.state == "settled":
                totals[1] += reservation.input_actual
                totals[2] += reservation.output_actual
                if (
                    reservation.input_actual
                    > reservation.input_reserved
                    or reservation.output_actual
                    > reservation.output_reserved
                ):
                    overruns[reservation.budget_day] = True
            else:
                totals[1] += reservation.input_reserved
                totals[2] += reservation.output_reserved

        for budget_day, day in days_by_date.items():
            totals = expected[budget_day]
            if (
                day.calls_used != totals[0]
                or day.input_tokens_used != totals[1]
                or day.output_tokens_used != totals[2]
            ):
                raise ProviderBudgetUnavailable(
                    "corrupt provider budget state"
                )
            overrun = overruns[budget_day]
            if (
                day.reconciliation_required is not overrun
                or day.reconciliation_code
                != (_USAGE_OVERRUN_CODE if overrun else "")
            ):
                raise ProviderBudgetUnavailable(
                    "corrupt provider budget state"
                )

    @classmethod
    def _validate_all_aggregates(cls, session: Session) -> None:
        providers = set(
            session.scalars(select(ProviderBudgetDay.provider)).all()
        )
        providers.update(
            session.scalars(
                select(ProviderReservation.provider)
            ).all()
        )
        for provider in providers:
            cls._validate_provider_aggregates(session, provider)
