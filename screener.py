"""Multi-timeframe screener.

触发条件:
  - 蓝梯 > 黄梯
  - 且 (日线 DXDX  或  4H DXDX)
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from data_fetcher import fetch_daily, fetch_4h
from indicators import add_all_indicators

log = logging.getLogger(__name__)


@dataclass
class Hit:
    symbol: str
    daily_close: float
    daily_signal_at: Optional[datetime] = None
    h4_signal_at: Optional[datetime] = None
    detected_at: datetime = field(default_factory=datetime.now)

    def daily_str(self) -> str:
        return self.daily_signal_at.strftime("%Y-%m-%d") if self.daily_signal_at else "—"

    def h4_str(self) -> str:
        return self.h4_signal_at.strftime("%Y-%m-%d %H:%M") if self.h4_signal_at else "—"

    def to_text(self) -> str:
        return (
            f"{self.symbol:6s}  ${self.daily_close:>8.2f}  "
            f"日 {self.daily_str()}  4H {self.h4_str()}"
        )


def _last_signal_within(df, col, blue_col, n_bars):
    if df.empty:
        return None
    tail = df.tail(n_bars)
    matches = tail.index[tail[col] & tail[blue_col]]
    if len(matches) == 0:
        return None
    return matches[-1]


def check_symbol(symbol, h4_lookback_bars=2, daily_lookback_bars=3, require_strict_separation=False):
    try:
        df_d = fetch_daily(symbol, period="3y")
        if len(df_d) < 120:
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

    daily_ts = _last_signal_within(df_d, "DXDX", blue_col, daily_lookback_bars)
    h4_ts = _last_signal_within(df_h, "DXDX", blue_col, h4_lookback_bars)

    # OR 逻辑:至少一个时间级别触发
    if daily_ts is None and h4_ts is None:
        return None

    hit = Hit(symbol=symbol, daily_close=float(df_d.iloc[-1]["close"]))
    if daily_ts is not None:
        hit.daily_signal_at = daily_ts.to_pydatetime()
    if h4_ts is not None:
        hit.h4_signal_at = h4_ts.to_pydatetime()
    return hit


def run_screener(symbols, h4_lookback_bars=2, daily_lookback_bars=3,
                 require_strict_separation=False, max_workers=8):
    hits = []
    total = len(symbols)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(check_symbol, s, h4_lookback_bars, daily_lookback_bars,
                      require_strict_separation): s for s in symbols
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
