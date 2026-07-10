"""A small in-process fixed-window rate limiter.

Protects the endpoints that cost money or hit the broker (chat -> LLM tokens,
approve -> live order). Not a distributed limiter — adequate for a single-host
assistant; swap for Redis if this ever scales out.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True
