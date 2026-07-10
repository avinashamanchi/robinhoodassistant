"""Exponential backoff with jitter for the daemon feed loop (A4).

Delay = min(cap, base * 2^(attempt-1)) with +/- up to `jitter_frac` random
jitter, so many daemons don't reconnect in lockstep. attempt starts at 1.
"""

from __future__ import annotations

import random


def next_delay(
    attempt: int,
    base: float = 1.0,
    cap: float = 60.0,
    jitter_frac: float = 0.2,
    rng: random.Random | None = None,
) -> float:
    attempt = max(attempt, 1)
    raw = min(cap, base * (2 ** (attempt - 1)))
    r = rng or random
    jitter = raw * jitter_frac * (2 * r.random() - 1)  # +/- jitter_frac
    return max(0.0, min(cap, raw + jitter))
