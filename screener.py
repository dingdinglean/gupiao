"""Strict multi-timeframe screener.

A symbol is a hit only when the latest fully closed candle satisfies either:
  - Daily: DXDX (抄底首现) AND blue > yellow
  - 4H:    DXDX (抄底首现) AND blue > yellow

Older signals, lower-timeframe signals, and still-forming candles never trigger.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from data_fetcher import fetch_daily, fetch_4h
from indicators import add_all_indicators

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
CLOSE_GRACE = pd.Timedelta(minutes=20)


@dataclass
class Hit:
    symbol: str
    daily_signal_at: datetime | None
    h4_signal_at: datetime | None
    daily_close: float
    daily_dif: float | None
    h4_dif: float | None
    blue_strict_daily: bool | None
    blue_strict_h4: bool | None
    detected_at: datetime

    def to_text(self) -> str:
        daily = f"{self.daily_signal_at:%Y-%m-%d}" if self.daily_signal_at else "-"
        h4 = f"{self.h4_signal_at:%Y-%m-%d %H:%M}" if self.h4_signal_at else "-"
        return (
            f"{self.symbol:6s}  ${self.daily_close:>8.2f}  "
            f"日 {daily}  "
            f"4H {h4}"
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("daily_signal_at", "h4_signal_at", "detected_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


def _as_new_york_time(now: datetime | pd.Timestamp | None = None) -> pd.Timestamp:
    ts = pd.Timestamp(now if now is not None else datetime.now(tz=NEW_YORK))
    if ts.tzinfo is None:
        return ts.tz_localize(NEW_YORK)
    return ts.tz_convert(NEW_YORK)


def _latest_closed_position(
    df: pd.DataFrame,
    timeframe: str,
    now: datetime | pd.Timestamp | None = None,
) -> int | None:
    """Return the position of the latest fully closed candle.

    A 20-minute grace period avoids selecting Yahoo bars that are still being
    finalized. For the second US-session 4H bucket, the actual close is capped
    at the regular-session close (16:00 ET), not the synthetic 17:30 label.
    """
    if df.empty:
        return None

    now_et = _as_new_york_time(now)
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx_et = idx.tz_localize(NEW_YORK)
    else:
        idx_et = idx.tz_convert(NEW_YORK)

    session_close = idx_et.normalize() + pd.Timedelta(hours=16) + CLOSE_GRACE

    if timeframe == "daily":
        closed_at = session_close
    elif timeframe == "4h":
        label_close = idx_et + CLOSE_GRACE
        closed_at = pd.DatetimeIndex(
            [min(label_time, market_close) for label_time, market_close in zip(label_close, session_close)]
        )
    else:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    positions = [i for i, is_closed in enumerate(closed_at <= now_et) if bool(is_closed)]
    return positions[-1] if positions else None


def _latest_closed_row(
    df: pd.DataFrame,
    timeframe: str,
    now: datetime | pd.Timestamp | None = None,
) -> tuple[pd.Timestamp, pd.Series] | None:
    pos = _latest_closed_position(df, timeframe, now)
    if pos is None:
        return None
    return pd.Timestamp(df.index[pos]), df.iloc[pos]


def _is_true(value) -> bool:
    return False if pd.isna(value) else bool(value)


def _signal_on_latest_closed(
    df: pd.DataFrame,
    signal_col: str,
    blue_col: str,
    timeframe: str,
    now: datetime | pd.Timestamp | None = None,
) -> tuple[pd.Timestamp | None, pd.Series | None]:
    """Check only the latest closed candle; never search older candles."""
    if df.empty or signal_col not in df.columns or blue_col not in df.columns:
        return None, None

    latest = _latest_closed_row(df, timeframe, now)
    if latest is None:
        return None, None

    ts, row = latest
    if _is_true(row[signal_col]) and _is_true(row[blue_col]):
        return ts, row
    return None, row


def check_symbol(
    symbol: str,
    h4_lookback_bars: int | None = None,
    daily_lookback_bars: int | None = None,
    require_strict_separation: bool = False,
    now: datetime | pd.Timestamp | None = None,
) -> Hit | None:
    """Run the strict check on one symbol.

    The lookback arguments remain only for backward compatibility with older
    callers. They are intentionally ignored: only the latest closed candle is
    eligible to trigger a push.
    """
    del h4_lookback_bars, daily_lookback_bars

    df_d = pd.DataFrame()
    df_h = pd.DataFrame()

    try:
        daily = fetch_daily(symbol, period="3y")
        if len(daily) >= 120:  # enough warmup for EMA-89 + divergence
            df_d = add_all_indicators(daily)
    except Exception as e:
        log.warning(f"{symbol}: daily data error - {e}")

    try:
        h4 = fetch_4h(symbol, period="730d")
        if len(h4) >= 120:
            df_h = add_all_indicators(h4)
    except Exception as e:
        log.warning(f"{symbol}: 4H data error - {e}")

    if df_d.empty and df_h.empty:
        return None

    blue_col = "BLUE_FULLY_ABOVE_YELLOW" if require_strict_separation else "BLUE_ABOVE_YELLOW"

    # Hard rule: latest fully closed daily OR latest fully closed 4H candle.
    # The signal and blue>yellow condition must be true on that same candle.
    daily_ts, daily_latest = _signal_on_latest_closed(df_d, "DXDX", blue_col, "daily", now)
    h4_ts, h4_latest = _signal_on_latest_closed(df_h, "DXDX", blue_col, "4h", now)

    if daily_ts is None and h4_ts is None:
        return None

    daily_signal_row = df_d.loc[daily_ts] if daily_ts is not None else None
    h4_signal_row = df_h.loc[h4_ts] if h4_ts is not None else None

    price_row = daily_latest if daily_latest is not None else h4_latest
    if price_row is None:
        return None

    detected = _as_new_york_time(now).to_pydatetime() if now is not None else datetime.now()

    return Hit(
        symbol=symbol,
        daily_signal_at=daily_ts.to_pydatetime() if daily_ts is not None else None,
        h4_signal_at=h4_ts.to_pydatetime() if h4_ts is not None else None,
        daily_close=float(price_row["close"]),
        daily_dif=float(daily_signal_row["DIF"]) if daily_signal_row is not None else None,
        h4_dif=float(h4_signal_row["DIF"]) if h4_signal_row is not None else None,
        blue_strict_daily=(
            _is_true(daily_signal_row["BLUE_FULLY_ABOVE_YELLOW"])
            if daily_signal_row is not None else None
        ),
        blue_strict_h4=(
            _is_true(h4_signal_row["BLUE_FULLY_ABOVE_YELLOW"])
            if h4_signal_row is not None else None
        ),
        detected_at=detected,
    )


def run_screener(
    symbols: list[str],
    h4_lookback_bars: int | None = None,
    daily_lookback_bars: int | None = None,
    require_strict_separation: bool = False,
    max_workers: int = 8,
) -> list[Hit]:
    """Scan symbols in parallel and return strict latest-closed-bar hits."""
    hits: list[Hit] = []
    total = len(symbols)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(
                check_symbol,
                s,
                h4_lookback_bars,
                daily_lookback_bars,
                require_strict_separation,
            ): s
            for s in symbols
        }
        for fut in as_completed(futures):
            done += 1
            sym = futures[fut]
            try:
                result = fut.result()
                if result is not None:
                    log.info(f"HIT  [{done}/{total}] {result.to_text()}")
                    hits.append(result)
                elif done % 50 == 0:
                    log.info(f"... progress {done}/{total}")
            except Exception as e:
                log.warning(f"{sym}: {e}")
    return hits
