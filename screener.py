"""Independent daily/4H DXDX pullback scanner.

This module intentionally does not rank RSI, sectors, momentum, or watchlists.
It keeps the original DXDX formula and EMA23/EMA89 trend filter.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from data_fetcher import fetch_4h, fetch_daily
from indicators import add_all_indicators
from universe import is_us_listed_stock

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
CLOSE_GRACE = pd.Timedelta(minutes=20)


@dataclass
class Signal:
    symbol: str
    signal_level: str
    daily_dxdx: bool
    h4_dxdx: bool
    daily_signal_time: datetime | None
    h4_signal_time: datetime | None
    close: float
    blue_above_yellow: bool
    detected_at: datetime

    def signal_keys(self) -> list[tuple[str, datetime]]:
        keys: list[tuple[str, datetime]] = []
        if self.daily_dxdx and self.daily_signal_time:
            keys.append(("daily", self.daily_signal_time))
        if self.h4_dxdx and self.h4_signal_time:
            keys.append(("4h", self.h4_signal_time))
        return keys

    def to_dict(self) -> dict:
        result = asdict(self)
        for key in ("daily_signal_time", "h4_signal_time", "detected_at"):
            result[key] = result[key].isoformat() if result[key] else ""
        return result


@dataclass
class ScanStats:
    pool_count: int
    fetched_count: int = 0
    failed_count: int = 0


def _as_new_york_time(now: datetime | pd.Timestamp | None = None) -> pd.Timestamp:
    timestamp = pd.Timestamp(now if now is not None else datetime.now(tz=NEW_YORK))
    return timestamp.tz_localize(NEW_YORK) if timestamp.tzinfo is None else timestamp.tz_convert(NEW_YORK)


def _closed_positions(df: pd.DataFrame, timeframe: str, now: datetime | pd.Timestamp | None = None) -> list[int]:
    """Return positions of complete US-session bars only."""
    if df.empty:
        return []
    now_et = _as_new_york_time(now)
    index = pd.DatetimeIndex(df.index)
    index_et = index.tz_localize(NEW_YORK) if index.tz is None else index.tz_convert(NEW_YORK)
    session_close = index_et.normalize() + pd.Timedelta(hours=16) + CLOSE_GRACE
    if timeframe == "daily":
        closes_at = session_close
    elif timeframe == "4h":
        closes_at = pd.DatetimeIndex([min(label + CLOSE_GRACE, close) for label, close in zip(index_et, session_close)])
    else:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return [position for position, closed in enumerate(closes_at <= now_et) if bool(closed)]


def latest_complete_daily(df: pd.DataFrame, now: datetime | pd.Timestamp | None = None) -> tuple[pd.Timestamp, pd.Series] | None:
    positions = _closed_positions(df, "daily", now)
    if not positions:
        return None
    position = positions[-1]
    return pd.Timestamp(df.index[position]), df.iloc[position]


def current_day_h4_dxdx(df: pd.DataFrame, now: datetime | pd.Timestamp | None = None) -> tuple[pd.Timestamp, pd.Series] | None:
    """Find a DXDX on either of today's two latest complete 4H bars only."""
    positions = _closed_positions(df, "4h", now)
    if not positions or "DXDX" not in df.columns:
        return None
    now_et = _as_new_york_time(now).normalize()
    candidates: list[int] = []
    for position in positions:
        timestamp = pd.Timestamp(df.index[position])
        timestamp_et = timestamp.tz_localize(NEW_YORK) if timestamp.tzinfo is None else timestamp.tz_convert(NEW_YORK)
        if timestamp_et.normalize() == now_et:
            candidates.append(position)
    # There are two 4H session bars; reverse prioritises the most recent one.
    for position in reversed(candidates[-2:]):
        row = df.iloc[position]
        if bool(row.get("DXDX", False)):
            return pd.Timestamp(df.index[position]), row
    return None


def _trend_is_bullish(daily_row: pd.Series, strict: bool) -> bool:
    column = "BLUE_FULLY_ABOVE_YELLOW" if strict else "BLUE_ABOVE_YELLOW"
    return bool(daily_row.get(column, False))


def check_symbol(symbol: str, *, require_strict_separation: bool = False, now: datetime | pd.Timestamp | None = None) -> Signal | None:
    """Classify one US stock: S daily+4H, A 4H, B daily."""
    if not is_us_listed_stock(symbol):
        return None
    try:
        daily = add_all_indicators(fetch_daily(symbol, period="3y"))
        h4 = add_all_indicators(fetch_4h(symbol, period="730d"))
    except Exception as exc:
        log.warning("%s data error: %s", symbol, exc)
        raise
    if len(daily) < 120 or len(h4) < 120:
        raise ValueError("insufficient daily or 4H history")

    daily_latest = latest_complete_daily(daily, now)
    if daily_latest is None:
        return None
    daily_time, daily_row = daily_latest
    bullish = _trend_is_bullish(daily_row, require_strict_separation)
    if not bullish:
        return None

    daily_dxdx = bool(daily_row.get("DXDX", False))
    h4_match = current_day_h4_dxdx(h4, now)
    h4_dxdx = h4_match is not None
    if not daily_dxdx and not h4_dxdx:
        return None

    level = "S" if daily_dxdx and h4_dxdx else ("A" if h4_dxdx else "B")
    h4_time = h4_match[0].to_pydatetime() if h4_match else None
    return Signal(
        symbol=symbol,
        signal_level=level,
        daily_dxdx=daily_dxdx,
        h4_dxdx=h4_dxdx,
        daily_signal_time=daily_time.to_pydatetime() if daily_dxdx else None,
        h4_signal_time=h4_time,
        close=float(daily_row["close"]),
        blue_above_yellow=True,
        detected_at=_as_new_york_time(now).to_pydatetime(),
    )


def run_screener(symbols: list[str], *, require_strict_separation: bool = False, max_workers: int = 6, now: datetime | pd.Timestamp | None = None) -> tuple[list[Signal], ScanStats]:
    allowed = [symbol for symbol in symbols if is_us_listed_stock(symbol)]
    stats = ScanStats(pool_count=len(allowed))
    signals: list[Signal] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_symbol, symbol, require_strict_separation=require_strict_separation, now=now): symbol for symbol in allowed}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result = future.result()
                stats.fetched_count += 1
                if result:
                    signals.append(result)
            except Exception as exc:
                stats.failed_count += 1
                log.warning("%s skipped: %s", symbol, exc)
    priority = {"S": 0, "A": 1, "B": 2}
    return sorted(signals, key=lambda item: (priority[item.signal_level], item.symbol)), stats
