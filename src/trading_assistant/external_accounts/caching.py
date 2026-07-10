"""TTL caching + graceful degradation for any ExternalAccountSource.

Reads are cached for ``ttl_seconds`` so we don't hammer the broker. On a fetch
failure the last good value is served with ``stale=True`` (positions: last list;
summary: last summary flagged stale) — the system runs fine with the source down.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Optional

from .base import ExternalAccountSource, ExternalAccountSummary, ExternalPosition

log = logging.getLogger(__name__)


class CachingExternalSource:
    source_name: str

    def __init__(self, inner: ExternalAccountSource, ttl_seconds: float = 300.0) -> None:
        self._inner = inner
        self.source_name = getattr(inner, "source_name", "external")
        self._ttl = ttl_seconds
        self._pos_cache: Optional[tuple[float, list[ExternalPosition]]] = None
        self._sum_cache: Optional[tuple[float, ExternalAccountSummary]] = None
        self.stale = False

    def _fresh(self, cache) -> bool:
        return cache is not None and (time.monotonic() - cache[0]) < self._ttl

    def get_positions(self) -> list[ExternalPosition]:
        if self._fresh(self._pos_cache):
            return self._pos_cache[1]
        try:
            positions = self._inner.get_positions()
            self._pos_cache = (time.monotonic(), positions)
            self.stale = False
            return positions
        except Exception as exc:
            log.warning("external positions fetch failed (%s); serving cache", type(exc).__name__)
            self.stale = True
            return self._pos_cache[1] if self._pos_cache else []

    def get_account_summary(self) -> Optional[ExternalAccountSummary]:
        if self._fresh(self._sum_cache):
            return self._sum_cache[1]
        try:
            summary = self._inner.get_account_summary()
            self._sum_cache = (time.monotonic(), summary)
            self.stale = False
            return summary
        except Exception as exc:
            log.warning("external summary fetch failed (%s); serving cache", type(exc).__name__)
            self.stale = True
            if self._sum_cache:
                return replace(self._sum_cache[1], stale=True)
            return None

    def get_order_history(self, days: int = 30) -> list[dict]:
        try:
            return self._inner.get_order_history(days)
        except Exception as exc:
            log.warning("external order history failed (%s)", type(exc).__name__)
            return []

    def get_dividends(self, days: int = 90) -> list[dict]:
        try:
            return self._inner.get_dividends(days)
        except Exception as exc:
            log.warning("external dividends failed (%s)", type(exc).__name__)
            return []
