"""Yahoo Finance data fetching + 4H resampling aligned to US market hours."""
from __future__ import annotations

import logging
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    # Multi-index from yfinance with multiple tickers - flatten if so
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    return df[cols].dropna()


def fetch_daily(symbol: str, period: str = "3y") -> pd.DataFrame:
    """Daily bars. 3y of history gives plenty of room for the indicator warmup."""
    df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
    return _normalize(df)


def fetch_hourly(symbol: str, period: str = "730d") -> pd.DataFrame:
    """Hourly bars. Yahoo caps 1H history at ~730 days."""
    df = yf.Ticker(symbol).history(period=period, interval="1h", auto_adjust=True)
    return _normalize(df)


def resample_to_4h(hourly: pd.DataFrame) -> pd.DataFrame:
    """Resample 1H bars to 4H bars, anchored to US market open (09:30 ET).

    For each trading day yfinance returns hourly bars at:
      09:30, 10:30, 11:30, 12:30, 13:30, 14:30, 15:30
    We bucket them as two "4H" bars per session:
      Bar 1: 09:30-13:30  (4 hourly bars)
      Bar 2: 13:30-17:30  (only 13:30/14:30/15:30 hourly bars; closes at market close)

    This matches how most US-equity 4H charts work.
    """
    if hourly.empty:
        return hourly
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

    # yfinance returns tz-aware index in market local time (America/New_York).
    # Anchor 4H buckets to 09:30 ET.
    if hourly.index.tz is None:
        hourly = hourly.tz_localize("America/New_York")

    origin = pd.Timestamp("1970-01-01 09:30:00", tz="America/New_York")
    df4 = hourly.resample("4h", origin=origin, label="right", closed="left").agg(agg)
    # Drop the buckets that have no underlying bars (weekends, pre/post-market gaps)
    df4 = df4.dropna(subset=["close"])
    return df4


def fetch_4h(symbol: str, period: str = "730d") -> pd.DataFrame:
    return resample_to_4h(fetch_hourly(symbol, period))
