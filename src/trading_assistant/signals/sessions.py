"""Decision-time bar policy shared by backtests and live consumers.

The daily strategies decide on **completed sessions only**. In the backtest
engine a decision at bar ``t`` sees bars up to and including ``t`` (its
``DataView`` is bounded at ``t``) and fills at the next bar's open, so its
information cutoff is the close of the last completed session. Live consumers
(the autopilot, ``/analyze``, ``/screen``, shadow analysis) must use the same
cutoff, so this module defines the policy once:

* A daily bar belongs to the session named by its timestamp's calendar date in
  the asset class's session timezone: America/New_York for equities (Alpaca
  labels daily bars at local midnight, 04:00 or 05:00 UTC depending on DST),
  UTC for crypto (24/7 daily candles).
* A session is complete once its close has passed, decided from one coherent
  market-clock observation for the decision instant, never a hand-rolled
  calendar. Shortened sessions, holidays and weekends therefore follow the
  exchange calendar:

  - market open now → only sessions *before* the current one are complete;
  - market closed now → the most recently opened session has closed, so it
    and everything before it are complete (a pre-open morning still points
    at the previous session, so a pre-market bar for today is excluded).

* With no clock available the policy is conservative: only sessions before
  the current calendar date (in the session timezone) count as complete.
  Order-placing consumers must not use this fallback; they require a clock.
* A session's bar is only *final* if the frame holding it was fetched after
  the bar could no longer change: 20:00 New York time on the session date for
  equities (after extended hours; also covers early closes), one hour after
  UTC midnight for crypto. A frame fetched at 15:00 therefore never supplies
  that day's close, even after the market closes; the consumer refreshes it.
  ``decision_cutoff`` combines clock completion with that finality rule.
* Duplicate bars for one session keep the last row; bars stay sorted.
* Features are computed on the same trailing window the backtest uses
  (``FEATURE_LOOKBACK`` bars), because exponential averages, MACD and ADX
  depend on where the window starts.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pandas as pd

from ..assets import AssetClass
from ..risk.clock import MarketClockObservation

FEATURE_LOOKBACK = 320
EXCHANGE_TIMEZONE = ZoneInfo("America/New_York")
EQUITY_BAR_FINAL_AT = time(20, 0)        # New York local time on the session date
CRYPTO_BAR_FINAL_DELAY = timedelta(hours=1)  # after the UTC day ends


def session_timezone(asset_class: AssetClass) -> tzinfo:
    return timezone.utc if asset_class is AssetClass.CRYPTO else EXCHANGE_TIMEZONE


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def session_date(timestamp: datetime, asset_class: AssetClass) -> date:
    """The session a daily bar belongs to (naive timestamps are UTC)."""
    return _aware(timestamp).astimezone(session_timezone(asset_class)).date()


def completed_through(
    *,
    now: datetime,
    asset_class: AssetClass,
    observation: MarketClockObservation | None,
) -> date:
    """Inclusive calendar bound: every session dated on or before it closed.

    The bound need not be a trading day: during a session it is the calendar
    day before the current session (possibly a weekend or holiday with no
    bar), which excludes exactly the in-progress session.
    """
    zone = session_timezone(asset_class)
    if observation is None:
        return _aware(now).astimezone(zone).date() - timedelta(days=1)
    current = _aware(observation.most_recent_open).astimezone(zone).date()
    if observation.is_open:
        return current - timedelta(days=1)
    return current


def final_instant(session: date, asset_class: AssetClass) -> datetime:
    """The UTC instant after which a session's daily bar no longer changes."""
    if asset_class is AssetClass.CRYPTO:
        return (
            datetime.combine(session + timedelta(days=1), time(0), timezone.utc)
            + CRYPTO_BAR_FINAL_DELAY
        )
    return datetime.combine(
        session, EQUITY_BAR_FINAL_AT, EXCHANGE_TIMEZONE
    ).astimezone(timezone.utc)


def final_through(fetched_at: datetime, asset_class: AssetClass) -> date:
    """Latest session whose bar was already final when a frame was fetched."""
    moment = _aware(fetched_at)
    if asset_class is AssetClass.CRYPTO:
        shifted = moment.astimezone(timezone.utc) - CRYPTO_BAR_FINAL_DELAY
        return shifted.date() - timedelta(days=1)
    local = moment.astimezone(EXCHANGE_TIMEZONE)
    if local.time() >= EQUITY_BAR_FINAL_AT:
        return local.date()
    return local.date() - timedelta(days=1)


def decision_cutoff(
    *,
    now: datetime,
    asset_class: AssetClass,
    observation: MarketClockObservation | None,
) -> date:
    """Latest session a decision at ``now`` may use: closed per the clock
    *and* old enough that its bar is final."""
    return min(
        completed_through(
            now=now, asset_class=asset_class, observation=observation
        ),
        final_through(now, asset_class),
    )


def completed_bars(
    frame: pd.DataFrame,
    *,
    asset_class: AssetClass,
    through: date,
) -> pd.DataFrame:
    """Rows for sessions on or before ``through``, one row per session."""
    if frame.empty:
        return frame
    ordered = frame.sort_index(kind="stable")
    sessions = pd.Index(
        [session_date(ts.to_pydatetime(), asset_class) for ts in ordered.index]
    )
    keep = (sessions <= through) & ~sessions.duplicated(keep="last")
    return ordered.loc[keep]


def decision_bars(
    frame: pd.DataFrame,
    *,
    asset_class: AssetClass,
    through: date,
    fetched_at: datetime | None = None,
) -> pd.DataFrame:
    """Completed, final sessions, trimmed to the backtest's feature window.

    ``fetched_at`` (when the frame left the provider) caps ``through`` so a
    bar captured mid-session is never treated as that session's final bar.
    """
    if fetched_at is not None:
        through = min(through, final_through(fetched_at, asset_class))
    return completed_bars(frame, asset_class=asset_class, through=through).tail(
        FEATURE_LOOKBACK
    )
