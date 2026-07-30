"""Strict multi-timeframe screener aligned with the live chart.

A symbol is a hit only when the latest available bar satisfies either:
  - Daily: DXDX (cd.docx 抄底) AND blue > yellow
  - 4H:    DXDX (cd.docx 抄底) AND blue > yellow

The latest available bar may still be forming. This intentionally matches the
real-time indicator shown in Futu/other charting apps during US market hours.
Older bars and lower-timeframe signals never trigger.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime

import pandas as pd

from data_fetcher import fetch_daily, fetch_4h
from indicators import add_all_indicators

log = logging.getLogger(__name__)


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
        for key in ("daily_signal_at", "h4_signal_at", "detected_at"):
            d[key] = d[key].isoformat() if d[key] else None
        return d


def _is_true(value) -> bool:
    return False if pd.isna(value) else bool(value)


def _latest_available_row(df: pd.DataFrame) -> tuple[pd.Timestamp, pd.Series] | None:
    """Return the latest bar supplied by the data source, including a live bar."""
    if df.empty:
        return None
    return pd.Timestamp(df.index[-1]), df.iloc[-1]


def _signal_on_latest_available(
    df: pd.DataFrame,
    signal_col: str,
    blue_col: str,
) -> tuple[pd.Timestamp | None, pd.Series | None]:
    """Check only the latest available bar; never search backward for old signals."""
    if df.empty or signal_col not in df.columns or blue_col not in df.columns:
        return None, None

    latest = _latest_available_row(df)
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
    """Run the strict live-bar check on one symbol.

    The lookback arguments remain only for compatibility with older callers.
    They are ignored: an older DXDX is never allowed to trigger a push.
    """
    del h4_lookback_bars, daily_lookback_bars

    df_d = pd.DataFrame()
    df_h = pd.DataFrame()

    try:
        daily = fetch_daily(symbol, period="3y")
        if len(daily) >= 120:
            df_d = add_all_indicators(daily)
    except Exception as exc:
        log.warning(f"{symbol}: daily data error - {exc}")

    try:
        h4 = fetch_4h(symbol, period="730d")
        if len(h4) >= 120:
            df_h = add_all_indicators(h4)
    except Exception as exc:
        log.warning(f"{symbol}: 4H data error - {exc}")

    if df_d.empty and df_h.empty:
        return None

    blue_col = (
        "BLUE_FULLY_ABOVE_YELLOW"
        if require_strict_separation
        else "BLUE_ABOVE_YELLOW"
    )

    # Hard rule: only the newest available daily or 4H bar is eligible.
    # DXDX and blue>yellow must both be true on that same timeframe/bar.
    daily_ts, daily_latest = _signal_on_latest_available(df_d, "DXDX", blue_col)
    h4_ts, h4_latest = _signal_on_latest_available(df_h, "DXDX", blue_col)

    if daily_ts is None and h4_ts is None:
        return None

    daily_signal_row = df_d.loc[daily_ts] if daily_ts is not None else None
    h4_signal_row = df_h.loc[h4_ts] if h4_ts is not None else None

    # Prefer the live daily close for display; fall back to 4H only if daily failed.
    price_row = daily_latest if daily_latest is not None else h4_latest
    if price_row is None:
        return None

    detected = pd.Timestamp(now).to_pydatetime() if now is not None else datetime.now()

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
    """Scan symbols in parallel and return strict latest-live-bar hits."""
    hits: list[Hit] = []
    total = len(symbols)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                check_symbol,
                symbol,
                h4_lookback_bars,
                daily_lookback_bars,
                require_strict_separation,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            done += 1
            symbol = futures[future]
            try:
                result = future.result()
                if result is not None:
                    log.info(f"HIT  [{done}/{total}] {result.to_text()}")
                    hits.append(result)
                elif done % 50 == 0:
                    log.info(f"... progress {done}/{total}")
            except Exception as exc:
                log.warning(f"{symbol}: {exc}")
    return hits
