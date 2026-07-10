"""Market clock abstraction (A7).

We never hand-roll a holiday calendar. Consumers depend only on the
:class:`MarketClock` protocol. Tests drive :class:`FakeClock`; Phase 2 adds an
``AlpacaClock`` backed by Alpaca's clock/calendar API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class MarketClock(Protocol):
    def is_open(self, at: datetime | None = None) -> bool: ...
    def next_open(self, at: datetime | None = None) -> datetime: ...
    def next_close(self, at: datetime | None = None) -> datetime: ...


class CryptoClock:
    """Crypto trades 24/7 — always open. Satisfies the MarketClock protocol (Phase 7)."""

    def is_open(self, at: datetime | None = None) -> bool:
        return True

    def next_open(self, at: datetime | None = None) -> datetime:
        return at or datetime.now(timezone.utc)

    def next_close(self, at: datetime | None = None) -> datetime:
        # No close; report far future so "time until close" logic never fires.
        return datetime(9999, 1, 1, tzinfo=timezone.utc)


class FakeClock:
    """Controllable clock for tests. Toggle ``open`` and set the next boundaries."""

    def __init__(
        self,
        is_open: bool = True,
        next_open: datetime | None = None,
        next_close: datetime | None = None,
    ) -> None:
        self._open = is_open
        self._next_open = next_open or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._next_close = next_close or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def set_open(self, value: bool) -> None:
        self._open = value

    def is_open(self, at: datetime | None = None) -> bool:
        return self._open

    def next_open(self, at: datetime | None = None) -> datetime:
        return self._next_open

    def next_close(self, at: datetime | None = None) -> datetime:
        return self._next_close
