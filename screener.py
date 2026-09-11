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

from data_fetcher import fetch_daily, fetch_hourly, resample_to_4h, rth_hourly_to_daily
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
class H4Diagnostic:
    """Auditable inputs for every current-session 4H DXDX candidate.

    This record is observational only.  In particular, it never adds a
    condition to DXDX; it makes the existing calculation and the daily trend
    context visible in the workflow artifact.
    """

    symbol: str
    h4_signal_time: datetime
    h4_open: float
    h4_high: float
    h4_low: float
    h4_close: float
    daily_bar_date: datetime | None
    daily_close: float | None
    dif: float | None
    dea: float | None
    macd_bar: float | None
    ccc: bool
    jjj: bool
    dxdx: bool
    blue_above_yellow: bool
    daily_fresh_for_h4: bool
    daily_context_source: str

    def to_dict(self) -> dict:
        result = asdict(self)
        for key in ("h4_signal_time", "daily_bar_date"):
            result[key] = result[key].isoformat() if result[key] else ""
        return result


@dataclass
class ScanStats:
    pool_count: int
    fetched_count: int = 0
    failed_count: int = 0
    stale_daily_h4_count: int = 0


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


def _session_date(value: datetime | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return (timestamp.tz_localize(NEW_YORK) if timestamp.tzinfo is None else timestamp.tz_convert(NEW_YORK)).normalize()


def _number(row: pd.Series, column: str) -> float | None:
    value = row.get(column)
    return None if value is None or pd.isna(value) else float(value)


def _build_h4_diagnostic(
    symbol: str,
    h4_time: pd.Timestamp,
    h4_row: pd.Series,
    daily_time: pd.Timestamp | None,
    daily_row: pd.Series | None,
    *,
    require_strict_separation: bool,
    daily_context_source: str,
) -> H4Diagnostic:
    blue_above_yellow = bool(daily_row is not None and _trend_is_bullish(daily_row, require_strict_separation))
    daily_fresh = daily_time is not None and _session_date(daily_time) == _session_date(h4_time)
    return H4Diagnostic(
        symbol=symbol,
        h4_signal_time=h4_time.to_pydatetime(),
        h4_open=float(h4_row["open"]),
        h4_high=float(h4_row["high"]),
        h4_low=float(h4_row["low"]),
        h4_close=float(h4_row["close"]),
        daily_bar_date=daily_time.to_pydatetime() if daily_time is not None else None,
        daily_close=_number(daily_row, "close") if daily_row is not None else None,
        dif=_number(h4_row, "DIF"),
        dea=_number(h4_row, "DEA"),
        macd_bar=_number(h4_row, "MACD_bar"),
        ccc=bool(h4_row.get("CCC", False)),
        jjj=bool(h4_row.get("JJJ", False)),
        dxdx=bool(h4_row.get("DXDX", False)),
        blue_above_yellow=blue_above_yellow,
        daily_fresh_for_h4=daily_fresh,
        daily_context_source=daily_context_source,
    )


def _replace_session_daily_bar(daily: pd.DataFrame, session_daily: pd.DataFrame) -> pd.DataFrame:
    """Replace a date-labelled daily bar before rerunning the existing indicators."""
    session_date = _session_date(session_daily.index[0])
    index = pd.DatetimeIndex(daily.index)
    index_et = index.tz_localize(NEW_YORK) if index.tz is None else index.tz_convert(NEW_YORK)
    earlier = daily.loc[index_et.normalize() < session_date]
    return pd.concat([earlier, session_daily]).sort_index()


def _check_symbol_with_diagnostic(
    symbol: str,
    *,
    require_strict_separation: bool = False,
    now: datetime | pd.Timestamp | None = None,
) -> tuple[Signal | None, H4Diagnostic | None, bool]:
    """Classify one stock and retain a trace for any current 4H DXDX bar."""
    if not is_us_listed_stock(symbol):
        return None, None, False
    try:
        daily_raw = fetch_daily(symbol, period="3y")
        hourly = fetch_hourly(symbol, period="730d")
        daily = add_all_indicators(daily_raw)
        h4 = add_all_indicators(resample_to_4h(hourly))
    except Exception as exc:
        log.warning("%s data error: %s", symbol, exc)
        raise
    if len(daily) < 120 or len(h4) < 120:
        raise ValueError("insufficient daily or 4H history")

    daily_latest = latest_complete_daily(daily, now)
    daily_time, daily_row = daily_latest if daily_latest is not None else (None, None)
    h4_match = current_day_h4_dxdx(h4, now)
    daily_context_source = "yahoo_daily"

    # When Yahoo's completed 1D endpoint trails its completed RTH 1H endpoint,
    # reconstruct only today's daily bar from those already-RTH-filtered hourly
    # bars.  Then run the *same* existing daily indicator pipeline over the
    # amended history.  No 4H/DXDX math is changed.
    if h4_match is not None and (daily_time is None or _session_date(daily_time) != _session_date(h4_match[0])):
        fallback_daily = rth_hourly_to_daily(
            hourly,
            _session_date(h4_match[0]),
            now=_as_new_york_time(now),
            close_grace=CLOSE_GRACE,
        )
        if not fallback_daily.empty:
            daily = add_all_indicators(_replace_session_daily_bar(daily_raw, fallback_daily))
            daily_latest = latest_complete_daily(daily, now)
            daily_time, daily_row = daily_latest if daily_latest is not None else (None, None)
            if daily_time is not None and _session_date(daily_time) == _session_date(h4_match[0]):
                daily_context_source = "rth_hourly_fallback"
            else:
                daily_context_source = "stale_rejected"
        else:
            daily_context_source = "stale_rejected"

    diagnostic = (
        _build_h4_diagnostic(
            symbol,
            h4_match[0],
            h4_match[1],
            daily_time,
            daily_row,
            require_strict_separation=require_strict_separation,
            daily_context_source=daily_context_source,
        )
        if h4_match is not None
        else None
    )
    if daily_latest is None:
        if h4_match is not None:
            log.warning(
                "%s suppressing 4H DXDX at %s: no completed daily bar is available",
                symbol,
                h4_match[0].isoformat(),
            )
        return None, diagnostic, h4_match is not None

    stale_daily_h4 = False
    if h4_match is not None and _session_date(daily_time) != _session_date(h4_match[0]):
        # A post-close 4H signal cannot borrow yesterday's daily EMA channel.
        # Yahoo occasionally finalises hourly data before its daily response.
        # Suppress only the A/S 4H portion and let a later run evaluate it once
        # the matching RTH daily bar is complete.
        stale_daily_h4 = True
        log.warning(
            "%s suppressing 4H DXDX at %s: latest completed daily bar %s is stale",
            symbol,
            h4_match[0].isoformat(),
            daily_time.isoformat(),
        )
        h4_match = None

    bullish = _trend_is_bullish(daily_row, require_strict_separation)
    if not bullish:
        return None, diagnostic, stale_daily_h4

    daily_dxdx = bool(daily_row.get("DXDX", False))
    h4_dxdx = h4_match is not None
    if not daily_dxdx and not h4_dxdx:
        return None, diagnostic, stale_daily_h4

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
    ), diagnostic, stale_daily_h4


def check_symbol(symbol: str, *, require_strict_separation: bool = False, now: datetime | pd.Timestamp | None = None) -> Signal | None:
    """Classify one US stock: S daily+4H, A 4H, B daily."""
    signal, _, _ = _check_symbol_with_diagnostic(
        symbol,
        require_strict_separation=require_strict_separation,
        now=now,
    )
    return signal


def run_screener(
    symbols: list[str],
    *,
    require_strict_separation: bool = False,
    max_workers: int = 6,
    now: datetime | pd.Timestamp | None = None,
    diagnostics: list[H4Diagnostic] | None = None,
) -> tuple[list[Signal], ScanStats]:
    allowed = [symbol for symbol in symbols if is_us_listed_stock(symbol)]
    stats = ScanStats(pool_count=len(allowed))
    signals: list[Signal] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        check = _check_symbol_with_diagnostic if diagnostics is not None else check_symbol
        futures = {executor.submit(check, symbol, require_strict_separation=require_strict_separation, now=now): symbol for symbol in allowed}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result = future.result()
                stats.fetched_count += 1
                if diagnostics is not None:
                    result, diagnostic, stale_daily_h4 = result
                    if diagnostic is not None:
                        diagnostics.append(diagnostic)
                    if stale_daily_h4:
                        stats.stale_daily_h4_count += 1
                if result:
                    signals.append(result)
            except Exception as exc:
                stats.failed_count += 1
                log.warning("%s skipped: %s", symbol, exc)
    priority = {"S": 0, "A": 1, "B": 2}
    return sorted(signals, key=lambda item: (priority[item.signal_level], item.symbol)), stats
