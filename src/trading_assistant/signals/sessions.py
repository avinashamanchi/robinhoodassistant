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
* Duplicate bars for one session keep the last row; bars stay sorted.
* Features are computed on the same trailing window the backtest uses
  (``FEATURE_LOOKBACK`` bars), because exponential averages, MACD and ADX
  depend on where the window starts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pandas as pd

from ..assets import AssetClass
from ..risk.clock import MarketClockObservation

FEATURE_LOOKBACK = 320
EXCHANGE_TIMEZONE = ZoneInfo("America/New_York")


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
) -> pd.DataFrame:
    """Completed sessions, trimmed to the backtest's feature window."""
    return completed_bars(frame, asset_class=asset_class, through=through).tail(
        FEATURE_LOOKBACK
    )
