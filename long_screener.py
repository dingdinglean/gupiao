"""Independent weekly/monthly DXDX pullback scanner.

Unlike ``screener.py``, this module deliberately has no EMA23/EMA89 filter:
large-timeframe bottoms often occur during deep trend pullbacks.  It uses the
same unmodified MACD/DXDX implementation from ``indicators.py``.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from data_fetcher import fetch_daily, resample_to_monthly, resample_to_weekly
from indicators import compute_macd_divergence
from universe import is_us_listed_stock

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
CLOSE_GRACE = pd.Timedelta(minutes=20)
MIN_BARS = 120


@dataclass
class LongSignal:
    symbol: str
    timeframe: str  # "weekly" or "monthly"
    signal_time: datetime
    close: float
    detected_at: datetime

    def signal_keys(self) -> list[tuple[str, datetime]]:
        return [(self.timeframe, self.signal_time)]

    def to_dict(self) -> dict:
        result = asdict(self)
        result["signal_time"] = self.signal_time.isoformat()
        result["detected_at"] = self.detected_at.isoformat()
        return result


@dataclass
class LongScanStats:
    pool_count: int
    fetched_count: int = 0
    failed_count: int = 0
    insufficient_count: int = 0


def _as_new_york_time(now: datetime | pd.Timestamp | None = None) -> pd.Timestamp:
    timestamp = pd.Timestamp(now if now is not None else datetime.now(tz=NEW_YORK))
    return timestamp.tz_localize(NEW_YORK) if timestamp.tzinfo is None else timestamp.tz_convert(NEW_YORK)


def _as_new_york_index(index: pd.Index) -> pd.DatetimeIndex:
    timestamps = pd.DatetimeIndex(index)
    return timestamps.tz_localize(NEW_YORK) if timestamps.tz is None else timestamps.tz_convert(NEW_YORK)


def _next_weekday(day: pd.Timestamp) -> pd.Timestamp:
    """Return the following Monday-Friday date (holiday calendar is unavailable)."""
    candidate = day.normalize() + pd.Timedelta(days=1)
    while candidate.dayofweek >= 5:
        candidate += pd.Timedelta(days=1)
    return candidate


def _bar_complete_at(label: pd.Timestamp, timeframe: str) -> pd.Timestamp:
    label_et = _as_new_york_index(pd.DatetimeIndex([label]))[0]
    if timeframe == "weekly":
        # W-FRI labels a bar with its Friday end date.
        return label_et.normalize() + pd.Timedelta(hours=16) + CLOSE_GRACE
    if timeframe == "monthly":
        # Wait through the first following trading day close. This deliberately
        # favours a conservative confirmation over a still-changing month-end.
        return _next_weekday(label_et).normalize() + pd.Timedelta(hours=16) + CLOSE_GRACE
    raise ValueError(f"Unsupported long timeframe: {timeframe}")


def latest_complete_long_bar(
    df: pd.DataFrame,
    timeframe: str,
    now: datetime | pd.Timestamp | None = None,
) -> tuple[pd.Timestamp, pd.Series] | None:
    """Return the latest weekly/monthly bar that is safe to use for alerts."""
    if df.empty:
        return None
    now_et = _as_new_york_time(now)
    index_et = _as_new_york_index(df.index)
    complete = [position for position, label in enumerate(index_et) if _bar_complete_at(label, timeframe) <= now_et]
    if not complete:
        return None
    position = complete[-1]
    return pd.Timestamp(df.index[position]), df.iloc[position]


def _signals_from_daily(symbol: str, daily: pd.DataFrame, now: datetime | pd.Timestamp | None = None) -> tuple[list[LongSignal], bool]:
    """Produce weekly and/or monthly signals; histories are evaluated separately."""
    detected_at = _as_new_york_time(now).to_pydatetime()
    signals: list[LongSignal] = []
    weekly = resample_to_weekly(daily)
    monthly = resample_to_monthly(daily)
    enough_weekly = len(weekly) >= MIN_BARS
    enough_monthly = len(monthly) >= MIN_BARS

    for timeframe, bars, enough in (("weekly", weekly, enough_weekly), ("monthly", monthly, enough_monthly)):
        if not enough:
            continue
        # Use only the original MACD divergence/DXDX calculation.  No EMA
        # channel is computed or checked in this independent scanner.
        indicator_bars = compute_macd_divergence(bars)
        latest = latest_complete_long_bar(indicator_bars, timeframe, now)
        if latest is None:
            continue
        signal_time, row = latest
        if bool(row.get("DXDX", False)):
            signals.append(LongSignal(
                symbol=symbol,
                timeframe=timeframe,
                signal_time=signal_time.to_pydatetime(),
                close=float(row["close"]),
                detected_at=detected_at,
            ))
    return signals, not enough_weekly and not enough_monthly


def check_symbol(symbol: str, *, now: datetime | pd.Timestamp | None = None) -> list[LongSignal]:
    """Scan one eligible US stock, allowing either timeframe to be sufficient."""
    if not is_us_listed_stock(symbol):
        return []
    daily = fetch_daily(symbol, period="max")
    signals, _ = _signals_from_daily(symbol, daily, now)
    return signals


def run_long_screener(
    symbols: list[str], *, max_workers: int = 6, now: datetime | pd.Timestamp | None = None,
) -> tuple[list[LongSignal], LongScanStats]:
    """Scan the existing S&P 500 + Nasdaq-100 universe without shared failure."""
    allowed = [symbol for symbol in symbols if is_us_listed_stock(symbol)]
    stats = LongScanStats(pool_count=len(allowed))
    signals: list[LongSignal] = []

    def scan(symbol: str) -> tuple[list[LongSignal], bool]:
        daily = fetch_daily(symbol, period="max")
        return _signals_from_daily(symbol, daily, now)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scan, symbol): symbol for symbol in allowed}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                symbol_signals, insufficient = future.result()
                stats.fetched_count += 1
                stats.insufficient_count += int(insufficient)
                signals.extend(symbol_signals)
            except Exception as exc:
                stats.failed_count += 1
                log.warning("%s skipped: %s", symbol, exc)
    priority = {"monthly": 0, "weekly": 1}
    return sorted(signals, key=lambda item: (priority[item.timeframe], item.symbol)), stats
