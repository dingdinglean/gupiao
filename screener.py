"""Multi-timeframe screener.

Criteria for a hit:
  - Daily timeframe:   DXDX (抄底首现) within `daily_lookback_bars` bars AND blue > yellow
  - 4H timeframe:      DXDX (抄底首现) within `h4_lookback_bars` bars AND blue > yellow

The two-timeframe alignment is the "resonance" the user asked for.
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
    daily_signal_at: datetime
    h4_signal_at: datetime
    daily_close: float
    daily_dif: float
    h4_dif: float
    blue_strict_daily: bool
    blue_strict_h4: bool
    detected_at: datetime

    def to_text(self) -> str:
        return (
            f"{self.symbol:6s}  ${self.daily_close:>8.2f}  "
            f"日 {self.daily_signal_at:%Y-%m-%d}  "
            f"4H {self.h4_signal_at:%Y-%m-%d %H:%M}"
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("daily_signal_at", "h4_signal_at", "detected_at"):
            d[k] = d[k].isoformat()
        return d


def _last_signal_within(df: pd.DataFrame, col: str, blue_col: str, n_bars: int) -> pd.Timestamp | None:
    """Return timestamp of the most recent bar in the last `n_bars` where
    both `col` and `blue_col` are True, or None."""
    if df.empty:
        return None
    tail = df.tail(n_bars)
    matches = tail.index[tail[col] & tail[blue_col]]
    if len(matches) == 0:
        return None
    return matches[-1]


def check_symbol(
    symbol: str,
    h4_lookback_bars: int = 2,
    daily_lookback_bars: int = 3,
    require_strict_separation: bool = False,
) -> Hit | None:
    """Run the full check on one symbol.

    Returns a Hit if the symbol passes, else None.
    """
    try:
        df_d = fetch_daily(symbol, period="3y")
        if len(df_d) < 120:   # need enough warmup for EMA-89 + divergence
            return None
        df_d = add_all_indicators(df_d)

        df_h = fetch_4h(symbol, period="730d")
        if len(df_h) < 120:
            return None
        df_h = add_all_indicators(df_h)
    except Exception as e:
        log.warning(f"{symbol}: data error - {e}")
        return None

    blue_col = "BLUE_FULLY_ABOVE_YELLOW" if require_strict_separation else "BLUE_ABOVE_YELLOW"

    # Recent DXDX signal with blue > yellow at the signal bar
    daily_ts = _last_signal_within(df_d, "DXDX", blue_col, daily_lookback_bars)
    if daily_ts is None:
        return None
    h4_ts = _last_signal_within(df_h, "DXDX", blue_col, h4_lookback_bars)
    if h4_ts is None:
        return None

    daily_row = df_d.loc[daily_ts]
    h4_row    = df_h.loc[h4_ts]
    latest_d  = df_d.iloc[-1]

    return Hit(
        symbol=symbol,
        daily_signal_at=daily_ts.to_pydatetime(),
        h4_signal_at=h4_ts.to_pydatetime(),
        daily_close=float(latest_d["close"]),
        daily_dif=float(daily_row["DIF"]),
        h4_dif=float(h4_row["DIF"]),
        blue_strict_daily=bool(daily_row["BLUE_FULLY_ABOVE_YELLOW"]),
        blue_strict_h4=bool(h4_row["BLUE_FULLY_ABOVE_YELLOW"]),
        detected_at=datetime.now(),
    )


def run_screener(
    symbols: list[str],
    h4_lookback_bars: int = 2,
    daily_lookback_bars: int = 3,
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
