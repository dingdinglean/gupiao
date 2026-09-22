"""Authoritative NYSE session boundaries shared by every RTH fallback.

``pandas_market_calendars`` ships the XNYS exchange calendar, including
holidays and scheduled early closes.  Keeping this in one small module stops
the daily, 4H, weekly, and monthly paths from drifting into different ideas of
what a completed US session means.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal


NEW_YORK = ZoneInfo("America/New_York")
XNYS = mcal.get_calendar("XNYS")


@dataclass(frozen=True)
class NYSESession:
    """One scheduled NYSE regular session in America/New_York."""

    open: pd.Timestamp
    close: pd.Timestamp


def _date_key(day: pd.Timestamp | str) -> str:
    timestamp = pd.Timestamp(day)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(NEW_YORK)
    return timestamp.date().isoformat()


@lru_cache(maxsize=4096)
def _session_for_date_key(date_key: str) -> NYSESession | None:
    schedule = XNYS.schedule(start_date=date_key, end_date=date_key)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return NYSESession(
        open=pd.Timestamp(row["market_open"]).tz_convert(NEW_YORK),
        close=pd.Timestamp(row["market_close"]).tz_convert(NEW_YORK),
    )


def nyse_session(day: pd.Timestamp | str) -> NYSESession | None:
    """Return the actual scheduled NYSE RTH bounds for ``day``, if open."""
    return _session_for_date_key(_date_key(day))


@lru_cache(maxsize=256)
def _session_dates_for_year(year: int) -> pd.DatetimeIndex:
    """NYSE session dates for one year, cached for bulk daily validation."""
    schedule = XNYS.schedule(
        start_date=f"{year:04d}-01-01",
        end_date=f"{year:04d}-12-31",
    )
    return pd.DatetimeIndex(schedule.index).normalize()


def nyse_trading_date_mask(days: pd.Index) -> np.ndarray:
    """Vectorised membership mask for midnight-labelled NYSE daily bars.

    Fetching one exchange schedule per year is equivalent to calling
    ``nyse_session`` for every date, including holidays, while avoiding the
    cache churn caused by repeating decades of one-day schedule queries for
    hundreds of symbols.
    """
    index = pd.DatetimeIndex(days)
    if index.empty:
        return index.isin(pd.DatetimeIndex([]))
    if index.tz is not None:
        index = index.tz_convert(NEW_YORK).tz_localize(None)
    normalized = index.normalize()
    yearly = [
        _session_dates_for_year(year)
        for year in sorted(set(int(value) for value in normalized.year))
    ]
    valid = yearly[0].append(yearly[1:])
    return normalized.isin(valid)


def is_nyse_trading_day(day: pd.Timestamp | str) -> bool:
    return nyse_session(day) is not None


def previous_nyse_trading_day(day: pd.Timestamp | str) -> pd.Timestamp:
    """Return the latest actual NYSE trading date at midnight ET."""
    candidate = pd.Timestamp(_date_key(day), tz=NEW_YORK).normalize()
    while nyse_session(candidate) is None:
        candidate -= pd.Timedelta(days=1)
    return candidate


def next_nyse_trading_day(day: pd.Timestamp | str) -> pd.Timestamp:
    """Return the next actual NYSE trading date at midnight ET."""
    candidate = pd.Timestamp(_date_key(day), tz=NEW_YORK).normalize() + pd.Timedelta(days=1)
    while nyse_session(candidate) is None:
        candidate += pd.Timedelta(days=1)
    return candidate


def expected_hourly_starts(day: pd.Timestamp | str) -> pd.DatetimeIndex:
    """Yahoo 1H bar starts wholly/partly within the actual regular session."""
    session = nyse_session(day)
    if session is None:
        return pd.DatetimeIndex([], tz=NEW_YORK)
    starts: list[pd.Timestamp] = []
    current = session.open
    while current < session.close:
        starts.append(current)
        current += pd.Timedelta(hours=1)
    return pd.DatetimeIndex(starts)
