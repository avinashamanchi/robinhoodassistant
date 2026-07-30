"""Exponential backoff with jitter for the daemon feed loop (A4).

Delay = min(cap, base * 2^(attempt-1)) with +/- up to `jitter_frac` random
jitter, so many daemons don't reconnect in lockstep. attempt starts at 1.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from ..app.limits import LimitSpec
from ..assets import AssetClass
from ..config import WindowLimitConfig
from ..risk.breakers import BreakerScope

_T = TypeVar("_T")
RETRIABLE_READ_ERRORS = (TimeoutError, ConnectionError)
ALPACA_MARKET_DATA_PRINCIPAL = "provider:alpaca:market-data"
COINGECKO_MARKET_DATA_PRINCIPAL = "provider:coingecko:market-data"
SCHEDULED_MARKET_DATA_PRINCIPAL = ALPACA_MARKET_DATA_PRINCIPAL


class ScheduledMarketDataDenied(RuntimeError):
    """The durable scheduled-read allowance was denied before provider I/O."""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_seconds: float = 1.0
    cap_seconds: float = 30.0
    jitter_fraction: float = 0.2

    def __post_init__(self) -> None:
        if (
            self.attempts < 1
            or self.base_seconds < 0
            or self.cap_seconds < 0
            or not 0 <= self.jitter_fraction <= 1
        ):
            raise ValueError("invalid read retry policy")


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


def retry_read(
    operation: Callable[[], _T],
    policy: RetryPolicy = RetryPolicy(),
    *,
    sleep: Callable[[float], object] = time.sleep,
) -> _T:
    for attempt in range(1, policy.attempts + 1):
        try:
            return operation()
        except RETRIABLE_READ_ERRORS:
            if attempt == policy.attempts:
                raise
            sleep(
                next_delay(
                    attempt,
                    base=policy.base_seconds,
                    cap=policy.cap_seconds,
                    jitter_frac=policy.jitter_fraction,
                )
            )
    raise RuntimeError("unreachable")


def scheduled_market_data_read(
    operation: Callable[[], _T],
    *,
    rate_limiter,
    limit_config: WindowLimitConfig,
    principal: str = SCHEDULED_MARKET_DATA_PRINCIPAL,
    retry_policy: RetryPolicy = RetryPolicy(),
    sleep: Callable[[float], object] = time.sleep,
) -> _T:
    """Require a durable allowance before every retryable provider attempt."""

    if rate_limiter is None:
        raise ValueError("scheduled market-data limiter is required")
    if not principal.strip():
        raise ValueError("scheduled market-data principal is required")
    spec = LimitSpec(
        name="provider_read",
        principal_requests=limit_config.requests,
        global_requests=limit_config.global_requests,
        window_seconds=limit_config.window_seconds,
        principal_daily_requests=limit_config.daily_requests,
        global_daily_requests=limit_config.global_daily_requests,
    )

    def authorized_attempt():
        decision = rate_limiter.consume_pair(
            spec,
            principal=principal,
        )
        if not decision.allowed:
            raise ScheduledMarketDataDenied(
                "scheduled market data allowance denied"
            )
        return operation()

    return retry_read(
        authorized_attempt,
        retry_policy,
        sleep=sleep,
    )


def trip_scheduled_market_data_breaker(
    service,
    symbol: str,
    *,
    actor: str,
    request_id: str,
    audit_reason: str,
    now=None,
):
    """Persist the shared stale-data breaker for a denied scheduled read."""

    return service.breakers.trip(
        BreakerScope.data(AssetClass.for_symbol(symbol)),
        "scheduled market data allowance unavailable",
        actor,
        request_id=request_id,
        now=now,
        audit_reason=audit_reason,
    )


async def retry_async_read(
    operation: Callable[[], Awaitable[_T]],
    policy: RetryPolicy = RetryPolicy(),
    *,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> _T:
    for attempt in range(1, policy.attempts + 1):
        try:
            return await operation()
        except RETRIABLE_READ_ERRORS:
            if attempt == policy.attempts:
                raise
            await sleep(
                next_delay(
                    attempt,
                    base=policy.base_seconds,
                    cap=policy.cap_seconds,
                    jitter_frac=policy.jitter_fraction,
                )
            )
    raise RuntimeError("unreachable")
