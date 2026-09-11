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
from functools import lru_cache
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
)

from data_fetcher import (
    _daily_regular_session_only,
    fetch_daily,
    fetch_hourly,
    resample_to_monthly,
    resample_to_weekly,
    rth_hourly_to_daily,
)
from indicators import compute_macd_divergence
from universe import is_us_listed_stock

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
CLOSE_GRACE = pd.Timedelta(minutes=20)
MIN_BARS = 120


class _NYSEHolidayCalendar(AbstractHolidayCalendar):
    """Regular NYSE holidays needed to identify a period's final trading day."""

    rules = [
        Holiday("NewYearsDay", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday, start_date="2022-01-01"),
        Holiday("IndependenceDay", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


NYSE_HOLIDAYS = _NYSEHolidayCalendar()


@dataclass
class LongSignal:
    symbol: str
    timeframe: str  # "weekly" or "monthly"
    signal_time: datetime
    close: float
    detected_at: datetime
    push_date: str = ""
    push_price: float | None = None

    def signal_keys(self) -> list[tuple[str, datetime]]:
        return [(self.timeframe, self.signal_time)]

    def to_dict(self) -> dict:
        result = asdict(self)
        result["signal_time"] = self.signal_time.isoformat()
        result["detected_at"] = self.detected_at.isoformat()
        result["signal_price"] = self.close
        result["push_price"] = "" if self.push_price is None else self.push_price
        return result


@dataclass
class LongDiagnostic:
    symbol: str
    timeframe: str
    signal_time: datetime | None
    last_required_trading_date: datetime | None
    latest_daily_date: datetime | None
    daily_context_source: str
    signal_close: float | None
    push_date: str
    push_price: float | None
    dxdx: bool
    completeness_passed: bool

    def to_dict(self) -> dict:
        result = asdict(self)
        for key in ("signal_time", "last_required_trading_date", "latest_daily_date"):
            result[key] = result[key].isoformat() if result[key] else ""
        result["push_price"] = "" if result["push_price"] is None else result["push_price"]
        result["signal_close"] = "" if result["signal_close"] is None else result["signal_close"]
        return result


@dataclass
class LongScanStats:
    pool_count: int
    fetched_count: int = 0
    failed_count: int = 0
    insufficient_count: int = 0
    freshness_rejected_count: int = 0


def _as_new_york_time(now: datetime | pd.Timestamp | None = None) -> pd.Timestamp:
    timestamp = pd.Timestamp(now if now is not None else datetime.now(tz=NEW_YORK))
    return timestamp.tz_localize(NEW_YORK) if timestamp.tzinfo is None else timestamp.tz_convert(NEW_YORK)


def _as_new_york_index(index: pd.Index) -> pd.DatetimeIndex:
    timestamps = pd.DatetimeIndex(index)
    return timestamps.tz_localize(NEW_YORK) if timestamps.tz is None else timestamps.tz_convert(NEW_YORK)


@lru_cache(maxsize=None)
def _nyse_holidays_for_year(year: int) -> frozenset[pd.Timestamp]:
    holidays = NYSE_HOLIDAYS.holidays(start=f"{year}-01-01", end=f"{year}-12-31")
    return frozenset(pd.Timestamp(day).normalize() for day in holidays)


def _is_us_market_trading_day(day: pd.Timestamp) -> bool:
    candidate = pd.Timestamp(day).normalize().tz_localize(None)
    if candidate.dayofweek >= 5:
        return False
    return candidate not in _nyse_holidays_for_year(candidate.year)


def _previous_trading_day(day: pd.Timestamp) -> pd.Timestamp:
    candidate = _as_new_york_index(pd.DatetimeIndex([day]))[0].normalize()
    while not _is_us_market_trading_day(candidate):
        candidate -= pd.Timedelta(days=1)
    return candidate


def _next_trading_day(day: pd.Timestamp) -> pd.Timestamp:
    candidate = _as_new_york_index(pd.DatetimeIndex([day]))[0].normalize() + pd.Timedelta(days=1)
    while not _is_us_market_trading_day(candidate):
        candidate += pd.Timedelta(days=1)
    return candidate


def last_required_trading_date(label: pd.Timestamp, timeframe: str) -> pd.Timestamp:
    """Final actual US trading date that must appear in a weekly/monthly bar."""
    label_et = _as_new_york_index(pd.DatetimeIndex([label]))[0].normalize()
    if timeframe == "weekly":
        return _previous_trading_day(label_et)
    if timeframe == "monthly":
        return _previous_trading_day(label_et)
    raise ValueError(f"Unsupported long timeframe: {timeframe}")


def _bar_complete_at(label: pd.Timestamp, timeframe: str) -> pd.Timestamp:
    label_et = _as_new_york_index(pd.DatetimeIndex([label]))[0]
    if timeframe == "weekly":
        # W-FRI labels a bar with its Friday end date.
        return label_et.normalize() + pd.Timedelta(hours=16) + CLOSE_GRACE
    if timeframe == "monthly":
        # Wait through the first following trading day close. This deliberately
        # favours a conservative confirmation over a still-changing month-end.
        return _next_trading_day(label_et).normalize() + pd.Timedelta(hours=16) + CLOSE_GRACE
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


def _latest_daily_date(daily: pd.DataFrame) -> pd.Timestamp | None:
    daily = _daily_regular_session_only(daily)
    if daily.empty:
        return None
    return _as_new_york_index(daily.index)[-1].normalize()


def _replace_session_daily_bar(daily: pd.DataFrame, session_daily: pd.DataFrame) -> pd.DataFrame:
    session_date = _as_new_york_index(session_daily.index)[0].normalize()
    index_et = _as_new_york_index(daily.index)
    return pd.concat([daily.loc[index_et.normalize() != session_date], session_daily]).sort_index()


def _daily_for_required_date(
    daily: pd.DataFrame,
    required_date: pd.Timestamp,
    now: pd.Timestamp,
    hourly_provider: Callable[[], pd.DataFrame] | None,
) -> tuple[pd.DataFrame, str, pd.Timestamp | None]:
    """Ensure ``required_date`` has a completed RTH daily bar, or reject it."""
    required_date = _as_new_york_index(pd.DatetimeIndex([required_date]))[0].normalize()
    clean_daily = _daily_regular_session_only(daily)
    latest_date = _latest_daily_date(clean_daily)
    daily_dates = _as_new_york_index(clean_daily.index).normalize() if not clean_daily.empty else pd.DatetimeIndex([])
    if required_date in daily_dates:
        return daily, "yahoo_daily", latest_date
    if hourly_provider is None:
        return daily, "stale_rejected", latest_date
    fallback = rth_hourly_to_daily(
        hourly_provider(),
        required_date,
        now=now,
        close_grace=CLOSE_GRACE,
    )
    if fallback.empty:
        return daily, "stale_rejected", latest_date
    rebuilt = _replace_session_daily_bar(daily, fallback)
    return rebuilt, "rth_hourly_fallback", _latest_daily_date(rebuilt)


def _safe_close(row: pd.Series | None) -> float | None:
    if row is None:
        return None
    try:
        close = float(row.get("close"))
    except (TypeError, ValueError):
        return None
    return close if close > 0 else None


def _signals_from_daily(
    symbol: str,
    daily: pd.DataFrame,
    now: datetime | pd.Timestamp | None = None,
    *,
    hourly_provider: Callable[[], pd.DataFrame] | None = None,
    diagnostics: list[LongDiagnostic] | None = None,
) -> tuple[list[LongSignal], bool]:
    """Produce complete weekly/monthly signals; histories are evaluated separately."""
    now_et = _as_new_york_time(now)
    detected_at = now_et.to_pydatetime()
    push_date = now_et.date().isoformat()
    signals: list[LongSignal] = []
    initial_weekly = resample_to_weekly(daily)
    initial_monthly = resample_to_monthly(daily)
    enough_weekly = len(initial_weekly) >= MIN_BARS
    enough_monthly = len(initial_monthly) >= MIN_BARS

    for timeframe, initial_bars, enough in (
        ("weekly", initial_weekly, enough_weekly),
        ("monthly", initial_monthly, enough_monthly),
    ):
        if not enough:
            continue
        initial_indicator_bars = compute_macd_divergence(initial_bars)
        latest = latest_complete_long_bar(initial_indicator_bars, timeframe, now_et)
        if latest is None:
            continue
        signal_time, initial_row = latest
        required_date = last_required_trading_date(signal_time, timeframe)
        context_daily, context_source, latest_daily_date = _daily_for_required_date(
            daily,
            required_date,
            now_et,
            hourly_provider,
        )
        completeness_passed = context_source != "stale_rejected"
        row = initial_row
        if completeness_passed and context_source == "rth_hourly_fallback":
            rebuilt_bars = resample_to_weekly(context_daily) if timeframe == "weekly" else resample_to_monthly(context_daily)
            if len(rebuilt_bars) < MIN_BARS:
                completeness_passed = False
                context_source = "stale_rejected"
            else:
                rebuilt = compute_macd_divergence(rebuilt_bars)
                rebuilt_latest = latest_complete_long_bar(rebuilt, timeframe, now_et)
                if rebuilt_latest is None or _as_new_york_index(pd.DatetimeIndex([rebuilt_latest[0]]))[0].normalize() != _as_new_york_index(pd.DatetimeIndex([signal_time]))[0].normalize():
                    completeness_passed = False
                    context_source = "stale_rejected"
                else:
                    signal_time, row = rebuilt_latest
        dxdx = bool(row.get("DXDX", False))
        push_price: float | None = None
        push_source = "yahoo_daily"
        # Only a candidate needs a push-price lookup.  This preserves the
        # normal all-daily scan cost when there are no long-period DXDX events.
        if completeness_passed and dxdx:
            push_daily, push_source, _ = _daily_for_required_date(daily, now_et.normalize(), now_et, hourly_provider)
            push_daily = _daily_regular_session_only(push_daily)
            push_row = push_daily.iloc[-1] if _latest_daily_date(push_daily) == now_et.normalize() else None
            push_price = _safe_close(push_row)
            # An unavailable push-date close must never become yesterday's
            # price.  It blocks this formal signal instead.
            if push_price is None:
                completeness_passed = False
                context_source = "stale_rejected"
        if diagnostics is not None:
            diagnostics.append(LongDiagnostic(
                symbol=symbol,
                timeframe=timeframe,
                signal_time=signal_time.to_pydatetime(),
                last_required_trading_date=required_date.to_pydatetime(),
                latest_daily_date=latest_daily_date.to_pydatetime() if latest_daily_date is not None else None,
                daily_context_source=context_source if context_source != "yahoo_daily" or push_source == "yahoo_daily" else push_source,
                signal_close=_safe_close(row),
                push_date=push_date,
                push_price=push_price,
                dxdx=dxdx,
                completeness_passed=completeness_passed,
            ))
        if not completeness_passed:
            log.warning(
                "%s %s rejected: required daily %s or push-date RTH close is incomplete",
                symbol,
                timeframe,
                required_date.date().isoformat(),
            )
            continue
        if dxdx:
            signals.append(LongSignal(
                symbol=symbol,
                timeframe=timeframe,
                signal_time=signal_time.to_pydatetime(),
                close=float(row["close"]),
                detected_at=detected_at,
                push_date=push_date,
                push_price=push_price,
            ))
    return signals, not enough_weekly and not enough_monthly


def check_symbol(symbol: str, *, now: datetime | pd.Timestamp | None = None) -> list[LongSignal]:
    """Scan one eligible US stock, allowing either timeframe to be sufficient."""
    if not is_us_listed_stock(symbol):
        return []
    daily = fetch_daily(symbol, period="max")
    hourly: pd.DataFrame | None = None

    def hourly_provider() -> pd.DataFrame:
        nonlocal hourly
        if hourly is None:
            hourly = fetch_hourly(symbol, period="730d")
        return hourly

    signals, _ = _signals_from_daily(
        symbol,
        daily,
        now,
        hourly_provider=hourly_provider,
    )
    return signals


def run_long_screener(
    symbols: list[str], *, max_workers: int = 6, now: datetime | pd.Timestamp | None = None,
    diagnostics: list[LongDiagnostic] | None = None,
) -> tuple[list[LongSignal], LongScanStats]:
    """Scan the existing S&P 500 + Nasdaq-100 universe without shared failure."""
    allowed = [symbol for symbol in symbols if is_us_listed_stock(symbol)]
    stats = LongScanStats(pool_count=len(allowed))
    signals: list[LongSignal] = []

    def scan(symbol: str) -> tuple[list[LongSignal], bool, list[LongDiagnostic]]:
        daily = fetch_daily(symbol, period="max")
        symbol_diagnostics: list[LongDiagnostic] = []
        hourly: pd.DataFrame | None = None

        def hourly_provider() -> pd.DataFrame:
            nonlocal hourly
            if hourly is None:
                hourly = fetch_hourly(symbol, period="730d")
            return hourly

        symbol_signals, insufficient = _signals_from_daily(
            symbol,
            daily,
            now,
            hourly_provider=hourly_provider,
            diagnostics=symbol_diagnostics,
        )
        return symbol_signals, insufficient, symbol_diagnostics

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scan, symbol): symbol for symbol in allowed}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                symbol_signals, insufficient, symbol_diagnostics = future.result()
                stats.fetched_count += 1
                stats.insufficient_count += int(insufficient)
                signals.extend(symbol_signals)
                if diagnostics is not None:
                    diagnostics.extend(symbol_diagnostics)
                    stats.freshness_rejected_count += sum(not item.completeness_passed for item in symbol_diagnostics)
            except Exception as exc:
                stats.failed_count += 1
                log.warning("%s skipped: %s", symbol, exc)
    priority = {"monthly": 0, "weekly": 1}
    return sorted(signals, key=lambda item: (priority[item.timeframe], item.symbol)), stats
