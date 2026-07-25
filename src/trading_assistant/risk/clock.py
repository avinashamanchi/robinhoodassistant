"""Market clock abstraction (A7).

We never hand-roll a holiday calendar. Consumers depend only on the
:class:`MarketClock` protocol. Tests drive :class:`FakeClock`; Phase 2 adds an
``AlpacaClock`` backed by Alpaca's clock/calendar API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MarketClockObservation:
    """Coherent market state and session boundary for one trusted instant."""

    is_open: bool
    most_recent_open: datetime


@runtime_checkable
class MarketClock(Protocol):
    def observe(self, at: datetime) -> MarketClockObservation: ...
    def is_open(self, at: datetime | None = None) -> bool: ...
    def next_open(self, at: datetime | None = None) -> datetime: ...
    def next_close(self, at: datetime | None = None) -> datetime: ...
    def most_recent_open(self, at: datetime | None = None) -> datetime: ...


class CryptoClock:
    """Crypto trades 24/7 — always open. Satisfies the MarketClock protocol (Phase 7)."""

    def observe(self, at: datetime) -> MarketClockObservation:
        return MarketClockObservation(
            is_open=True,
            most_recent_open=self.most_recent_open(at),
        )

    def is_open(self, at: datetime | None = None) -> bool:
        return True

    def next_open(self, at: datetime | None = None) -> datetime:
        return at or datetime.now(timezone.utc)

    def next_close(self, at: datetime | None = None) -> datetime:
        # No close; report far future so "time until close" logic never fires.
        return datetime(9999, 1, 1, tzinfo=timezone.utc)

    def most_recent_open(self, at: datetime | None = None) -> datetime:
        now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)


class FakeClock:
    """Controllable clock for tests. Toggle ``open`` and set the next boundaries."""

    def __init__(
        self,
        is_open: bool = True,
        next_open: datetime | None = None,
        next_close: datetime | None = None,
        most_recent_open: datetime | None = None,
    ) -> None:
        self._open = is_open
        self._next_open = next_open or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._next_close = next_close or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._most_recent_open = most_recent_open

    def set_open(self, value: bool) -> None:
        self._open = value

    def observe(self, at: datetime) -> MarketClockObservation:
        return MarketClockObservation(
            is_open=self._open,
            most_recent_open=self.most_recent_open(at),
        )

    def is_open(self, at: datetime | None = None) -> bool:
        return self._open

    def next_open(self, at: datetime | None = None) -> datetime:
        return self._next_open

    def next_close(self, at: datetime | None = None) -> datetime:
        return self._next_close

    def most_recent_open(self, at: datetime | None = None) -> datetime:
        if self._most_recent_open is not None:
            return self._most_recent_open
        observed_at = at or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        else:
            observed_at = observed_at.astimezone(timezone.utc)
        return observed_at - timedelta(days=1)
