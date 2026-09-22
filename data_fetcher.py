"""Yahoo Finance data fetching restricted to US regular trading hours."""
from __future__ import annotations

import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from session_calendar import expected_hourly_starts, nyse_session, nyse_trading_date_mask

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
SECOND_BAR_START_MINUTE = 13 * 60 + 30
DAILY_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
YF_BATCH_THREADS = 8
YF_BATCH_TIMEOUT_SECONDS = 12
YF_SINGLE_TIMEOUT_SECONDS = 10
YF_RETRY_BATCH_SIZE = 10

_YF_RUNTIME_LOCK = threading.Lock()
_YF_RUNTIME_READY = False
_YF_CACHE_WARMED = False
_YF_CACHE_DIR: str | None = None


@dataclass
class BatchDailyFetchStats:
    """Counters and timings for one official-daily batch download pass."""

    universe_count: int = 0
    batch_size: int = 50
    batch_count: int = 0
    batch_success_count: int = 0
    mini_batch_retry_count: int = 0
    mini_batch_retry_symbols: int = 0
    mini_batch_success_count: int = 0
    single_retry_count: int = 0
    single_retry_success_count: int = 0
    fallback_retry_count: int = 0
    fallback_success_count: int = 0
    final_failed_count: int = 0
    cache_warmup_seconds: float = 0.0
    batch_download_seconds: float = 0.0
    mini_batch_retry_seconds: float = 0.0
    single_retry_seconds: float = 0.0
    fallback_retry_seconds: float = 0.0
    normalization_seconds: float = 0.0


def configure_yfinance_runtime() -> str:
    """Give this process an isolated yfinance SQLite/timezone cache exactly once."""
    global _YF_RUNTIME_READY, _YF_CACHE_DIR
    if _YF_RUNTIME_READY:
        return _YF_CACHE_DIR or ""

    with _YF_RUNTIME_LOCK:
        if _YF_RUNTIME_READY:
            return _YF_CACHE_DIR or ""
        import yfinance as yf

        _YF_CACHE_DIR = tempfile.mkdtemp(prefix="gupiao-yfinance-")
        # This must precede every Ticker/history/download request.  In
        # particular, it prevents first-use SQLite contention in yf.download's
        # internal worker pool on GitHub runners.
        set_cache = getattr(yf, "set_tz_cache_location", None)
        if callable(set_cache):
            set_cache(_YF_CACHE_DIR)
        else:  # Lightweight unit-test doubles may expose only Ticker/history.
            log.warning("yfinance runtime has no set_tz_cache_location capability")
        try:
            yf.config.network.retries = 2
        except Exception:
            pass
        _YF_RUNTIME_READY = True
        log.info("yfinance runtime cache_dir=%s", _YF_CACHE_DIR)
        return _YF_CACHE_DIR


def warm_yfinance_cache() -> tuple[bool, float]:
    """Initialise yfinance cache/cookie state before threaded production batches."""
    global _YF_CACHE_WARMED
    configure_yfinance_runtime()
    with _YF_RUNTIME_LOCK:
        if _YF_CACHE_WARMED:
            return True, 0.0
        import yfinance as yf

        started = time.perf_counter()
        success = True
        try:
            # Warm-up data is intentionally not returned to callers or used by
            # indicators, diagnostics, state, or production signals.
            yf.download(
                tickers=["SPY"], period="5d", interval="1d",
                auto_adjust=True, prepost=False, progress=False,
                threads=False, timeout=YF_SINGLE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            success = False
            log.warning("yfinance cache warmup failed: %s", exc)
        elapsed = time.perf_counter() - started
        _YF_CACHE_WARMED = True
        log.info("yfinance cache warmup_seconds=%.3f warmup_success=%s", elapsed, str(success).lower())
        return success, elapsed


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
    keep = date_labelled.copy()
    if bool(date_labelled.any()):
        keep[date_labelled] = nyse_trading_date_mask(out.index[date_labelled])
    # Official Yahoo daily data takes the vectorised path above.  Unexpected
    # intraday rows remain subject to the exact per-session open/close bounds,
    # preserving the defensive extended-hours rejection behaviour.
    for position in (~date_labelled).nonzero()[0]:
        timestamp = out.index[position]
        bounds = nyse_session(timestamp)
        keep[position] = bool(bounds is not None and bounds.open <= timestamp <= bounds.close)
    return out.iloc[keep]


def _normalize_official_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise Yahoo's official 1D response without needless calendar scans.

    A standard ``interval=1d, prepost=False`` response is midnight-labelled in
    New York time.  It is already one official RTH OHLCV bar per date, so it
    does not need a per-row XNYS lookup.  Any unexpected intraday/mixed input
    deliberately falls back to the existing strict defensive filter.
    """
    out = _as_new_york_index(_normalize(df))
    if out.empty:
        return out
    if bool((out.index == out.index.normalize()).all()):
        return out.sort_index()
    return _daily_regular_session_only(out)


def fetch_daily(symbol: str, period: str = "3y") -> pd.DataFrame:
    """Confirmed US regular-session daily bars; no extended-hours data."""
    configure_yfinance_runtime()
    import yfinance as yf
    df = yf.Ticker(symbol).history(
        period=period,
        interval="1d",
        auto_adjust=True,
        prepost=False,
        timeout=YF_SINGLE_TIMEOUT_SECONDS,
    )
    # ``prepost=False`` is the source-level RTH guarantee.  Keep the
    # defensive daily filter as a second line of protection for every caller.
    return _normalize_official_daily(df)


def _valid_daily_ohlcv(df: pd.DataFrame) -> bool:
    """Whether a normalised official-daily frame is safe for indicator input."""
    return (
        not df.empty
        and all(column in df.columns for column in DAILY_OHLCV_COLUMNS)
        and not df[DAILY_OHLCV_COLUMNS].isna().any().any()
    )


def _extract_batch_symbol(downloaded: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Extract one ticker from either yfinance MultiIndex column orientation."""
    if downloaded is None or downloaded.empty:
        return pd.DataFrame()
    if not isinstance(downloaded.columns, pd.MultiIndex):
        return _normalize_official_daily(downloaded.copy())

    wanted = str(symbol).upper()
    for level in range(downloaded.columns.nlevels):
        labels = downloaded.columns.get_level_values(level)
        matches = [label for label in labels.unique() if str(label).upper() == wanted]
        if not matches:
            continue
        frame = downloaded.xs(matches[0], axis=1, level=level, drop_level=True)
        # A pathological three-level result is still handled defensively by
        # selecting the price-field level before the usual normalisation.
        if isinstance(frame.columns, pd.MultiIndex):
            for nested_level in range(frame.columns.nlevels):
                fields = {str(value).lower() for value in frame.columns.get_level_values(nested_level)}
                if {"open", "high", "low", "close"}.issubset(fields):
                    frame.columns = frame.columns.get_level_values(nested_level)
                    break
        return _normalize_official_daily(frame)
    return pd.DataFrame()


def _download_daily_batch(yf, symbols: list[str], *, period: str, threads: int) -> pd.DataFrame:
    """One bounded Yahoo batch request; callers own retry policy."""
    return yf.download(
        tickers=symbols,
        period=period,
        interval="1d",
        auto_adjust=True,
        prepost=False,
        progress=False,
        threads=threads,
        timeout=YF_BATCH_TIMEOUT_SECONDS,
    )


def _extract_valid_batch(downloaded: pd.DataFrame, symbols: list[str], metrics: BatchDailyFetchStats) -> tuple[dict[str, pd.DataFrame], list[str]]:
    accepted: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for symbol in symbols:
        started = time.perf_counter()
        frame = _extract_batch_symbol(downloaded, symbol)
        metrics.normalization_seconds += time.perf_counter() - started
        if _valid_daily_ohlcv(frame):
            accepted[symbol] = frame
        else:
            missing.append(symbol)
    return accepted, missing


def fetch_daily_batch(
    symbols: list[str],
    period: str = "max",
    batch_size: int = 50,
    *,
    stats: BatchDailyFetchStats | None = None,
    max_single_retries: int | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch official 1D history via bounded batches and layered retries.

    ``max_single_retries=None`` preserves the historical behaviour of trying
    every symbol left unresolved by the two batch layers.  Large-universe
    callers can set a finite cap so a broad Yahoo outage cannot degrade into
    hundreds of slow serial ``Ticker.history`` calls.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if max_single_retries is not None and max_single_retries < 0:
        raise ValueError("max_single_retries must be non-negative or None")

    ordered = list(dict.fromkeys(str(symbol).upper() for symbol in symbols if symbol))
    metrics = stats if stats is not None else BatchDailyFetchStats()
    metrics.universe_count = len(ordered)
    metrics.batch_size = batch_size
    metrics.batch_count = (len(ordered) + batch_size - 1) // batch_size
    result: dict[str, pd.DataFrame] = {}

    configure_yfinance_runtime()
    warmup_success, metrics.cache_warmup_seconds = warm_yfinance_cache()
    if not warmup_success:
        log.warning("continuing daily batches despite unsuccessful yfinance warmup")
    import yfinance as yf

    unresolved: list[str] = []
    for batch_number, start in enumerate(range(0, len(ordered), batch_size), start=1):
        batch = ordered[start : start + batch_size]
        log.info("daily batch %s/%s start symbols=%s", batch_number, metrics.batch_count, len(batch))
        started = time.perf_counter()
        try:
            downloaded = _download_daily_batch(yf, batch, period=period, threads=YF_BATCH_THREADS)
        except Exception as exc:
            log.warning("daily batch %s/%s failed: %s", batch_number, metrics.batch_count, exc)
            downloaded = pd.DataFrame()
        elapsed = time.perf_counter() - started
        metrics.batch_download_seconds += elapsed
        accepted, missing = _extract_valid_batch(downloaded, batch, metrics)
        result.update(accepted)
        metrics.batch_success_count += len(accepted)
        unresolved.extend(missing)
        log.info("daily batch %s/%s done elapsed=%.3f success=%s missing=%s", batch_number, metrics.batch_count, elapsed, len(accepted), len(missing))

    still_missing: list[str] = []
    mini_count = (len(unresolved) + YF_RETRY_BATCH_SIZE - 1) // YF_RETRY_BATCH_SIZE
    for retry_number, start in enumerate(range(0, len(unresolved), YF_RETRY_BATCH_SIZE), start=1):
        batch = unresolved[start : start + YF_RETRY_BATCH_SIZE]
        metrics.mini_batch_retry_count += 1
        metrics.mini_batch_retry_symbols += len(batch)
        log.info("daily retry batch %s/%s start symbols=%s", retry_number, mini_count, len(batch))
        started = time.perf_counter()
        try:
            downloaded = _download_daily_batch(yf, batch, period=period, threads=4)
        except Exception as exc:
            log.warning("daily retry batch %s/%s failed: %s", retry_number, mini_count, exc)
            downloaded = pd.DataFrame()
        elapsed = time.perf_counter() - started
        metrics.mini_batch_retry_seconds += elapsed
        accepted, missing = _extract_valid_batch(downloaded, batch, metrics)
        result.update(accepted)
        metrics.mini_batch_success_count += len(accepted)
        still_missing.extend(missing)
        log.info("daily retry batch %s/%s done elapsed=%.3f success=%s missing=%s", retry_number, mini_count, elapsed, len(accepted), len(missing))

    retry_symbols = still_missing if max_single_retries is None else still_missing[:max_single_retries]
    skipped_symbols = still_missing[len(retry_symbols):]
    if skipped_symbols:
        metrics.final_failed_count += len(skipped_symbols)
        log.warning(
            "daily single retry cap reached cap=%s unresolved_without_single_retry=%s",
            max_single_retries,
            len(skipped_symbols),
        )

    for symbol in retry_symbols:
        metrics.single_retry_count += 1
        metrics.fallback_retry_count += 1  # Backward-compatible report field.
        log.info("daily single retry %s start", symbol)
        retry_started = time.perf_counter()
        try:
            frame = fetch_daily(symbol, period=period)
        except Exception as exc:
            log.warning("daily single retry %s failed: %s", symbol, exc)
            frame = pd.DataFrame()
        elapsed = time.perf_counter() - retry_started
        metrics.single_retry_seconds += elapsed
        metrics.fallback_retry_seconds += elapsed
        if _valid_daily_ohlcv(frame):
            result[symbol] = frame
            metrics.single_retry_success_count += 1
            metrics.fallback_success_count += 1
            log.info("daily single retry %s done elapsed=%.3f success=true", symbol, elapsed)
        else:
            metrics.final_failed_count += 1
            log.warning("daily single retry %s done elapsed=%.3f success=false", symbol, elapsed)

    return result


def fetch_hourly(symbol: str, period: str = "730d") -> pd.DataFrame:
    """Regular-session hourly bars. Yahoo caps 1H history at about 730 days."""
    configure_yfinance_runtime()
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
