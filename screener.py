"""Multi-timeframe screener.

Criteria for a hit:
  - Daily timeframe:   DXDX (抄底首现) within `daily_lookback_bars` bars AND blue > yellow
  - OR 4H timeframe:   DXDX (抄底首现) within `h4_lookback_bars` bars AND blue > yellow
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
        for k in ("daily_signal_at", "h4_signal_at", "detected_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


def _last_signal_within(df: pd.DataFrame, col: str, blue_col: str, n_bars: int) -> pd.Timestamp | None:
    """Return timestamp of the most recent bar in the last `n_bars` where
    both `col` and `blue_col` are True, or None."""
    if df.empty or col not in df.columns or blue_col not in df.columns:
        return None
    tail = df.tail(n_bars)
    mask = tail[col].fillna(False).astype(bool) & tail[blue_col].fillna(False).astype(bool)
    matches = tail.index[mask]
    if len(matches) == 0:
        return None
    return matches[-1]


def check_symbol(
    symbol: str,
    h4_lookback_bars: int = 12,
    daily_lookback_bars: int = 5,
    require_strict_separation: bool = False,
) -> Hit | None:
    """Run the full check on one symbol.

    Returns a Hit if the symbol passes, else None.
    """
    df_d = pd.DataFrame()
    df_h = pd.DataFrame()

    try:
        daily = fetch_daily(symbol, period="3y")
        if len(daily) >= 120:   # need enough warmup for EMA-89 + divergence
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

    # Recent DXDX signal with blue > yellow at the signal bar
    daily_ts = _last_signal_within(df_d, "DXDX", blue_col, daily_lookback_bars)
    h4_ts = _last_signal_within(df_h, "DXDX", blue_col, h4_lookback_bars)

    # OR logic: either daily or 4H can trigger an alert.
    if daily_ts is None and h4_ts is None:
        return None

    daily_row = df_d.loc[daily_ts] if daily_ts is not None else None
    h4_row = df_h.loc[h4_ts] if h4_ts is not None else None
    latest = df_d.iloc[-1] if not df_d.empty else df_h.iloc[-1]

    return Hit(
        symbol=symbol,
        daily_signal_at=daily_ts.to_pydatetime() if daily_ts is not None else None,
        h4_signal_at=h4_ts.to_pydatetime() if h4_ts is not None else None,
        daily_close=float(latest["close"]),
        daily_dif=float(daily_row["DIF"]) if daily_row is not None else None,
        h4_dif=float(h4_row["DIF"]) if h4_row is not None else None,
        blue_strict_daily=bool(daily_row["BLUE_FULLY_ABOVE_YELLOW"]) if daily_row is not None else None,
        blue_strict_h4=bool(h4_row["BLUE_FULLY_ABOVE_YELLOW"]) if h4_row is not None else None,
        detected_at=datetime.now(),
    )


def run_screener(
    symbols: list[str],
    h4_lookback_bars: int = 12,
    daily_lookback_bars: int = 5,
    require_strict_separation: bool = False,
    max_workers: int = 8,
) -> list[Hit]:
    """Scan a list of symbols in parallel. Returns all hits."""
    hits: list[Hit] = []
    total = len(symbols)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(
                check_symbol, s,
                h4_lookback_bars, daily_lookback_bars, require_strict_separation,
            ): s for s in symbols
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
