"""Quote-staleness guard.

If bars stop arriving (a halt or a data outage), the system must not keep trading
against a frozen last price. This is the check the monitoring daemon (Phase 4)
will consult before acting on a quote; it lives here so it can be unit-tested and
reused by the risk pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_MAX_AGE_SECONDS = 60.0


def is_stale(
    quote_as_of: datetime,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    """True if the quote is older than ``max_age_seconds`` — do NOT trade on it."""
    now = now or datetime.now(timezone.utc)
    if quote_as_of.tzinfo is None:
        quote_as_of = quote_as_of.replace(tzinfo=timezone.utc)
    age = (now - quote_as_of).total_seconds()
    return age > max_age_seconds
