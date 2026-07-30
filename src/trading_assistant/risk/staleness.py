"""Quote-staleness guard.

If bars stop arriving (a halt or a data outage), the system must not keep trading
against a frozen last price. This is the check the monitoring daemon (Phase 4)
will consult before acting on a quote; it lives here so it can be unit-tested and
reused by the risk pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_MAX_AGE_SECONDS = 60.0
DEFAULT_MAX_FUTURE_SKEW_SECONDS = 5.0


def is_stale(
    quote_as_of: datetime | None,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    max_future_skew_seconds: float = DEFAULT_MAX_FUTURE_SKEW_SECONDS,
) -> bool:
    """True when a source time is absent, too old, or implausibly future."""
    if not isinstance(quote_as_of, datetime):
        return True
    now = now or datetime.now(timezone.utc)
    if quote_as_of.tzinfo is None:
        quote_as_of = quote_as_of.replace(tzinfo=timezone.utc)
    else:
        quote_as_of = quote_as_of.astimezone(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    age = (now - quote_as_of).total_seconds()
    return (
        age > max_age_seconds
        or age < -max_future_skew_seconds
    )
