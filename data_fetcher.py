"""Yahoo Finance data fetching restricted to US regular trading hours."""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
MARKET_OPEN_MINUTE = 9 * 60 + 30
SECOND_BAR_START_MINUTE = 13 * 60 + 30
MARKET_CLOSE_MINUTE = 16 * 60


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    # Multi-index from yfinance with multiple tickers - flatten if so.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    return df[cols].dropna()


def _as_new_york_index(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy whose DatetimeIndex is in America/New_York."""
    if df.empty:
        return df

    out = df.copy()
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is None:
        idx = idx.tz_localize(NEW_YORK)
    else:
        idx = idx.tz_convert(NEW_YORK)
    out.index = idx
    return out


def _regular_session_only(df: pd.DataFrame) -> pd.DataFrame:
    """Keep weekday bars from 09:30 inclusive to 16:00 exclusive, New York time."""
    if df.empty:
        return df

    out = _as_new_york_index(df)
    out = out[out.index.dayofweek < 5]

    minutes = out.index.hour * 60 + out.index.minute
    in_session = (minutes >= MARKET_OPEN_MINUTE) & (minutes < MARKET_CLOSE_MINUTE)
    return out.loc[in_session]


def fetch_daily(symbol: str, period: str = "3y") -> pd.DataFrame:
    """Daily bars without pre-market or after-hours data."""
    import yfinance as yf
    df = yf.Ticker(symbol).history(
        period=period,
        interval="1d",
        auto_adjust=True,
        prepost=False,
    )
    return _normalize(df)


def fetch_hourly(symbol: str, period: str = "730d") -> pd.DataFrame:
    """Regular-session hourly bars. Yahoo caps 1H history at about 730 days."""
    import yfinance as yf
    df = yf.Ticker(symbol).history(
        period=period,
        interval="1h",
        auto_adjust=True,
        prepost=False,
    )
    return _regular_session_only(_normalize(df))


def resample_to_4h(hourly: pd.DataFrame) -> pd.DataFrame:
    """Build two bars per US regular session using New York wall-clock time.

    Bar 1 contains 09:30, 10:30, 11:30 and 12:30 hourly bars and is labelled
    13:30 ET. Bar 2 contains 13:30, 14:30 and 15:30 hourly bars and is labelled
    16:00 ET.

    Grouping each trading session separately avoids the one-hour DST drift that
    can occur when pandas resampling is anchored to a fixed historical timestamp.
    """
    if hourly.empty:
        return hourly

    hourly = _regular_session_only(hourly)
    if hourly.empty:
        return hourly

    work = hourly.copy()
    work["_session"] = work.index.normalize()
    minutes = work.index.hour * 60 + work.index.minute
    work["_bucket"] = (minutes >= SECOND_BAR_START_MINUTE).astype(int)

    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    out = work.groupby(["_session", "_bucket"], sort=True).agg(agg)

    bar_ends: list[pd.Timestamp] = []
    for session, bucket in out.index:
        if int(bucket) == 0:
            bar_end = session + pd.Timedelta(hours=13, minutes=30)
        else:
            bar_end = session + pd.Timedelta(hours=16)
        bar_ends.append(bar_end)

    out.index = pd.DatetimeIndex(bar_ends, name=hourly.index.name)
    return out.dropna(subset=["close"])


def fetch_4h(symbol: str, period: str = "730d") -> pd.DataFrame:
    return resample_to_4h(fetch_hourly(symbol, period))
