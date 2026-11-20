"""Build MarketFeatures from real bars for the live /analyze + /screen paths.

Equities: Alpaca daily bars (adjusted). Crypto: CoinGecko. SPY provides market
context. Bars are cached to parquet by the underlying loaders, but live callers
bound the cache age (``LIVE_BAR_CACHE_MAX_AGE_SECONDS``) so features keep
tracking the market instead of freezing at the first download. Kept
lazy/defensive so a missing key degrades to a clear error rather than crashing
app startup.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from ..app.limits import LimitStoreUnavailable
from ..assets import AssetClass
from ..dependencies import RequiredDependencyUnavailable
from ..security.secrets import secret_value
from ..signals.features import build_features
from ..signals.models import MarketFeatures
from ..signals.sessions import (
    completed_bars,
    decision_bars,
    decision_cutoff,
    final_instant,
    final_through,
)

# Live features must reflect recent bars. Daily bars change at most once per
# session, so a few hours of reuse keeps provider calls low without letting a
# cache from a previous week drive analysis or autopilot decisions.
LIVE_BAR_CACHE_MAX_AGE_SECONDS = 4 * 60 * 60


def _historical_attempt_gate(
    config,
    *,
    service,
    rate_limiter,
    symbol: str,
    principal: str,
):
    if service is None and rate_limiter is None:
        return None
    if service is None or rate_limiter is None or config is None:
        raise ValueError(
            "scheduled historical reads require config, service, and limiter"
        )
    from ..daemon.backoff import (
        ScheduledMarketDataDenied,
        scheduled_market_data_read,
        trip_scheduled_market_data_breaker,
    )

    def gate(operation):
        try:
            return scheduled_market_data_read(
                operation,
                rate_limiter=rate_limiter,
                limit_config=(
                    config.security.rate_limits.provider_read
                ),
                principal=principal,
            )
        except (ScheduledMarketDataDenied, LimitStoreUnavailable):
            trip_scheduled_market_data_breaker(
                service,
                symbol,
                actor="daemon:historical",
                request_id=f"historical-read:{uuid4().hex}",
                audit_reason=(
                    "daemon scheduled historical market data read"
                ),
            )
            raise RequiredDependencyUnavailable from None

    return gate


def _fetch_equity_df(
    symbol: str,
    secrets,
    years: int = 2,
    *,
    config=None,
    service=None,
    rate_limiter=None,
    client_factory=None,
    cache_dir: str | Path = ".cache/bars",
    runtime_role: str = "app",
    max_cache_age_seconds: float | None = LIVE_BAR_CACHE_MAX_AGE_SECONDS,
    refresh_if_fetched_before: datetime | None = None,
):
    from ..backtest.data import download_alpaca_bars
    from ..daemon.backoff import ALPACA_MARKET_DATA_PRINCIPAL

    return download_alpaca_bars(
        symbol,
        secret_value(secrets.alpaca_api_key),
        secret_value(secrets.alpaca_secret_key),
        timeframe="1Day",
        years=years,
        cache_dir=cache_dir,
        runtime_role=runtime_role,
        client_factory=client_factory,
        max_cache_age_seconds=max_cache_age_seconds,
        refresh_if_fetched_before=refresh_if_fetched_before,
        attempt_gate=_historical_attempt_gate(
            config,
            service=service,
            rate_limiter=rate_limiter,
            symbol=symbol,
            principal=ALPACA_MARKET_DATA_PRINCIPAL,
        ),
    )


def _fetch_crypto_df(
    symbol: str,
    days: int = 365,
    *,
    config=None,
    service=None,
    rate_limiter=None,
    http: Any = None,
    cache_dir: str | Path = ".cache/bars",
    runtime_role: str = "app",
    max_cache_age_seconds: float | None = LIVE_BAR_CACHE_MAX_AGE_SECONDS,
    refresh_if_fetched_before: datetime | None = None,
):
    from ..backtest.coingecko import CoinGeckoClient
    from ..daemon.backoff import COINGECKO_MARKET_DATA_PRINCIPAL

    return CoinGeckoClient(
        http=http,
        cache_dir=cache_dir,
        runtime_role=runtime_role,
        max_cache_age_seconds=max_cache_age_seconds,
        attempt_gate=_historical_attempt_gate(
            config,
            service=service,
            rate_limiter=rate_limiter,
            symbol=symbol,
            principal=COINGECKO_MARKET_DATA_PRINCIPAL,
        ),
    ).bars(
        symbol,
        days=days,
        refresh_if_fetched_before=refresh_if_fetched_before,
    )


def _session_cutoff(
    *,
    scheduled_service=None,
    market_clock=None,
    now: Callable[[], datetime] | None = None,
    require_market_clock: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> Callable[[AssetClass], date]:
    """Latest usable session per asset class at the decision instant.

    Combines one clock observation (memoised for a minute, since the Alpaca
    clock reads the exchange calendar) with bar finality (``signals.sessions
    .decision_cutoff``). A clock that cannot be read fails closed. Without a
    clock, analysis-only callers get the conservative fallback; callers that
    pass ``require_market_clock`` (anything that can lead to an order) fail
    closed instead.

    The memo can only make a decision more conservative near a boundary: a
    cached "open" observation keeps the current session excluded for up to a
    minute after the close, and a cached "closed" observation before the open
    still points at the previous session.
    """
    clock_for = market_clock
    if clock_for is None and scheduled_service is not None:
        clock_for = getattr(scheduled_service, "market_clock", None)
    if clock_for is None and require_market_clock:
        raise RequiredDependencyUnavailable
    current = now or (lambda: datetime.now(timezone.utc))
    memo: dict[AssetClass, tuple[float, date]] = {}

    def cutoff(asset_class: AssetClass) -> date:
        cached = memo.get(asset_class)
        if cached is not None and monotonic() - cached[0] < 60:
            return cached[1]
        at = current()
        observation = None
        if clock_for is not None:
            try:
                observation = clock_for(asset_class).observe(at)
            except Exception:
                raise RequiredDependencyUnavailable from None
        through = decision_cutoff(
            now=at,
            asset_class=asset_class,
            observation=observation,
        )
        memo[asset_class] = (monotonic(), through)
        return through

    return cutoff


def build_live_feature_provider(
    config,
    secrets,
    *,
    scheduled_service=None,
    rate_limiter=None,
    alpaca_client_factory=None,
    coingecko_http: Any = None,
    cache_dir: str | Path = ".cache/bars",
    runtime_role: str = "app",
    market_clock: Callable[[AssetClass], Any] | None = None,
    now: Callable[[], datetime] | None = None,
    require_market_clock: bool = False,
) -> Callable[[str], MarketFeatures]:
    """Features on completed, final sessions over the backtest's window.

    See ``signals.sessions`` for the shared decision-time bar policy. A cached
    frame fetched before the latest usable session's bar became final is
    refreshed, whatever its age; each frame is then capped by its own fetch
    time, so a mid-session capture can never stand in for a final bar.
    """
    cutoff = _session_cutoff(
        scheduled_service=scheduled_service,
        market_clock=market_clock,
        now=now,
        require_market_clock=require_market_clock,
    )

    def provider(symbol: str) -> MarketFeatures:
        ac = AssetClass.for_symbol(symbol)
        through = cutoff(ac)
        try:
            df = (
                _fetch_crypto_df(
                    symbol,
                    config=config,
                    service=scheduled_service,
                    rate_limiter=rate_limiter,
                    http=coingecko_http,
                    cache_dir=cache_dir,
                    runtime_role=runtime_role,
                    refresh_if_fetched_before=final_instant(through, ac),
                )
                if ac is AssetClass.CRYPTO
                else _fetch_equity_df(
                    symbol,
                    secrets,
                    config=config,
                    service=scheduled_service,
                    rate_limiter=rate_limiter,
                    client_factory=alpaca_client_factory,
                    cache_dir=cache_dir,
                    runtime_role=runtime_role,
                    refresh_if_fetched_before=final_instant(through, ac),
                )
            )
        except RequiredDependencyUnavailable:
            raise
        except Exception:
            raise RequiredDependencyUnavailable from None
        df = decision_bars(
            df,
            asset_class=ac,
            through=through,
            fetched_at=df.attrs.get("fetched_at"),
        )
        if df.empty:
            raise RequiredDependencyUnavailable
        spy_df = None
        try:
            spy_through = cutoff(AssetClass.EQUITY)
            spy_raw = _fetch_equity_df(
                "SPY",
                secrets,
                config=config,
                service=scheduled_service,
                rate_limiter=rate_limiter,
                client_factory=alpaca_client_factory,
                cache_dir=cache_dir,
                runtime_role=runtime_role,
                refresh_if_fetched_before=final_instant(
                    spy_through, AssetClass.EQUITY
                ),
            )
            spy_df = decision_bars(
                spy_raw,
                asset_class=AssetClass.EQUITY,
                through=spy_through,
                fetched_at=spy_raw.attrs.get("fetched_at"),
            )
        except Exception:
            spy_df = None
        return build_features(symbol, ac, df, spy_df=spy_df)

    return provider


def build_screen_source(
    universe: list[str],
    secrets,
    *,
    config=None,
    scheduled_service=None,
    rate_limiter=None,
    alpaca_client_factory=None,
    coingecko_http: Any = None,
    cache_dir: str | Path = ".cache/bars",
    runtime_role: str = "app",
    market_clock: Callable[[AssetClass], Any] | None = None,
    now: Callable[[], datetime] | None = None,
):
    """Build a DataSource across the universe (+ SPY) from cached bars.

    Frames hold completed, final sessions only (``signals.sessions``), and
    caches captured before the latest usable session became final are
    refreshed, exactly as for the live feature provider.
    """
    from ..backtest.data import DataSource

    cutoff = _session_cutoff(
        scheduled_service=scheduled_service,
        market_clock=market_clock,
        now=now,
    )
    requested = set(universe)
    frames = {}
    for sym in requested | {"SPY"}:
        ac = AssetClass.for_symbol(sym)
        try:
            through = cutoff(ac)
            if ac is AssetClass.CRYPTO:
                raw = _fetch_crypto_df(
                    sym,
                    config=config,
                    service=scheduled_service,
                    rate_limiter=rate_limiter,
                    http=coingecko_http,
                    cache_dir=cache_dir,
                    runtime_role=runtime_role,
                    refresh_if_fetched_before=final_instant(through, ac),
                )
            else:
                raw = _fetch_equity_df(
                    sym,
                    secrets,
                    config=config,
                    service=scheduled_service,
                    rate_limiter=rate_limiter,
                    client_factory=alpaca_client_factory,
                    cache_dir=cache_dir,
                    runtime_role=runtime_role,
                    refresh_if_fetched_before=final_instant(through, ac),
                )
        except Exception:
            continue
        fetched = raw.attrs.get("fetched_at")
        if fetched is not None:
            through = min(through, final_through(fetched, ac))
        frames[sym] = completed_bars(raw, asset_class=ac, through=through)
    if not requested.intersection(frames):
        raise RequiredDependencyUnavailable
    return DataSource(frames)


class RefreshingScreenSource:
    """A screen source that rebuilds itself when its bars go stale.

    The app and daemon used to build their screen ``DataSource`` once per
    process, so screening, shadow analysis and the digest kept serving the
    bars from process start. This wrapper exposes the two members consumers
    use (``symbols``, ``full``) and rebuilds through ``build`` when a newer
    equity session has become final since the last build (the freshness that
    matters for daily bars), or after ``max_age_seconds`` as a backstop.

    Rebuilds are serialised so concurrent requests build once. A failed
    rebuild raises and keeps nothing stale in its place: the previous source
    is discarded, so stale bars are never served as current.
    """

    def __init__(
        self,
        build: Callable[[], Any],
        *,
        max_age_seconds: float = LIVE_BAR_CACHE_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._build = build
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._source = None
        self._built_at = 0.0
        self._built_for: date | None = None
        self._current()

    def _final_session(self) -> date:
        return final_through(self._now(), AssetClass.EQUITY)

    def _current(self):
        with self._lock:
            final_session = self._final_session()
            if (
                self._source is None
                or self._built_for != final_session
                or self._clock() - self._built_at > self._max_age_seconds
            ):
                self._source = None
                self._source = self._build()
                self._built_at = self._clock()
                self._built_for = final_session
            return self._source

    @property
    def symbols(self) -> list[str]:
        return self._current().symbols

    def full(self, symbol: str):
        return self._current().full(symbol)
