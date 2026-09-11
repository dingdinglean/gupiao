"""Yahoo Finance data fetching restricted to US regular trading hours."""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

import pandas as pd

from session_calendar import expected_hourly_starts, nyse_session

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
SECOND_BAR_START_MINUTE = 13 * 60 + 30


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
    """Keep only bars within each date's actual XNYS regular session."""
    if df.empty:
        return df

    out = _as_new_york_index(df)
    keep = pd.Series(False, index=out.index)
    for day, positions in out.groupby(out.index.normalize()).groups.items():
        bounds = nyse_session(day)
        if bounds is not None:
            index = pd.DatetimeIndex(positions)
            keep.loc[index] = (index >= bounds.open) & (index < bounds.close)
    return out.loc[keep.to_numpy()]


def _daily_regular_session_only(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only confirmed RTH daily bars, defensively rejecting intraday extras.

    Yahoo's ``interval=1d, prepost=False`` response is an official regular
    session daily bar and is normally date-labelled at midnight ET.  Those
    date labels must be retained; if an unexpected intraday row is supplied,
    only a 09:30--16:00 ET row is accepted.  This makes weekly/monthly
    aggregation safe even if a caller accidentally passes mixed data.
    """
    if df.empty:
        return df

    out = _as_new_york_index(df)
    date_labelled = out.index == out.index.normalize()
    keep = pd.Series(False, index=out.index)
    for day, positions in out.groupby(out.index.normalize()).groups.items():
        bounds = nyse_session(day)
        if bounds is None:
            continue
        index = pd.DatetimeIndex(positions)
        keep.loc[index] = date_labelled[out.index.get_indexer(index)] | ((index >= bounds.open) & (index <= bounds.close))
    return out.loc[keep.to_numpy()]


def fetch_daily(symbol: str, period: str = "3y") -> pd.DataFrame:
    """Confirmed US regular-session daily bars; no extended-hours data."""
    import yfinance as yf
    df = yf.Ticker(symbol).history(
        period=period,
        interval="1d",
        auto_adjust=True,
        prepost=False,
    )
    # ``prepost=False`` is the source-level RTH guarantee.  Keep the
    # defensive daily filter as a second line of protection for every caller.
    return _daily_regular_session_only(_normalize(df))


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
        bounds = nyse_session(session)
        if bounds is None:
            continue
        if int(bucket) == 0:
            bar_end = min(session + pd.Timedelta(hours=13, minutes=30), bounds.close)
        else:
            bar_end = bounds.close
        bar_ends.append(bar_end)

    out.index = pd.DatetimeIndex(bar_ends, name=hourly.index.name)
    return out.dropna(subset=["close"])


def rth_hourly_to_daily(
    hourly: pd.DataFrame,
    session_date: pd.Timestamp,
    *,
    now: pd.Timestamp,
    close_grace: pd.Timedelta = pd.Timedelta(minutes=20),
) -> pd.DataFrame:
    """Build one completed RTH daily bar from Yahoo's RTH 1H response.

    Yahoo can publish a completed intraday response before its ``1d`` bar.
    This helper is deliberately strict: it accepts only the expected RTH
    hourly starts for the actual XNYS session, only after that session's close
    plus the caller's grace period.
    It never fills missing bars or uses extended-hours rows.
    """
    if hourly.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    session = _as_new_york_index(hourly)
    target_date = pd.Timestamp(session_date)
    target_date = target_date.tz_localize(NEW_YORK) if target_date.tzinfo is None else target_date.tz_convert(NEW_YORK)
    target_date = target_date.normalize()
    now_et = pd.Timestamp(now)
    now_et = now_et.tz_localize(NEW_YORK) if now_et.tzinfo is None else now_et.tz_convert(NEW_YORK)
    bounds = nyse_session(target_date)
    if bounds is None or now_et < bounds.close + close_grace:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    session = _regular_session_only(session)
    session = session[session.index.normalize() == target_date]
    if session.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    expected = expected_hourly_starts(target_date)
    # Exact reindexing rejects missing, duplicated, or unexpected bar starts.
    if len(session) != len(expected) or not session.index.equals(expected):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    required = ["open", "high", "low", "close", "volume"]
    if any(column not in session.columns for column in required) or session[required].isna().any().any():
        return pd.DataFrame(columns=required)

    return pd.DataFrame(
        {
            "open": [float(session["open"].iloc[0])],
            "high": [float(session["high"].max())],
            "low": [float(session["low"].min())],
            "close": [float(session["close"].iloc[-1])],
            "volume": [float(session["volume"].sum())],
        },
        index=pd.DatetimeIndex([target_date], name=hourly.index.name),
    )


def _resample_ohlcv(daily: pd.DataFrame, frequency: str) -> pd.DataFrame:
    """Aggregate daily US bars using New York calendar boundaries.

    This intentionally works from daily data instead of Yahoo's partially
    formed ``1wk``/``1mo`` downloads.  The resulting labels are midnight in
    America/New_York on the Friday/month-end that owns each aggregate bar.
    Completion is decided by the long-timeframe scanner, not here.
    """
    if daily.empty:
        return daily

    # Weekly/monthly must never aggregate an untrusted extended-hours row.
    # Daily data returned by fetch_daily is already RTH-only, and this second
    # filter protects direct callers of the public resampling helpers too.
    work = _daily_regular_session_only(daily).sort_index()
    required = ["open", "high", "low", "close", "volume"]
    if any(column not in work.columns for column in required):
        return pd.DataFrame(columns=required)
    return work[required].resample(frequency).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"])


def resample_to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Build Friday-labelled weekly OHLCV bars from daily data in ET."""
    return _resample_ohlcv(daily, "W-FRI")


def resample_to_monthly(daily: pd.DataFrame) -> pd.DataFrame:
    """Build natural calendar-month OHLCV bars from daily data in ET."""
    return _resample_ohlcv(daily, "ME")


def fetch_4h(symbol: str, period: str = "730d") -> pd.DataFrame:
    return resample_to_4h(fetch_hourly(symbol, period))
