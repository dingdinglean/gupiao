from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from indicators import add_all_indicators, compute_macd_divergence

from .config import ResonanceConfig
from .models import SignalObservation


OHLCV = ["open", "high", "low", "close", "volume"]


def _date(value: object) -> date:
    return pd.Timestamp(value).date()


def _daily_available(signal_date: date) -> date:
    # Production runs after Yahoo's official daily finalisation window on the
    # following calendar day.  This is deliberately distinct from signal_date.
    return signal_date + timedelta(days=1)


def _weekly_id(week_end: date) -> str:
    iso = week_end.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def resample_to_unified_week(daily: pd.DataFrame) -> pd.DataFrame:
    """Map equities, ETFs, anchors and 24/7 assets to Monday-Sunday weeks."""
    if daily.empty or any(column not in daily.columns for column in OHLCV):
        return pd.DataFrame(columns=OHLCV)
    return daily[OHLCV].sort_index().resample("W-SUN").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"])


class TimeframeSignalProvider:
    """Produce daily/weekly observations through the existing indicator core."""

    def __init__(self, config: ResonanceConfig):
        self.config = config

    def build(
        self,
        daily_map: dict[str, pd.DataFrame],
        *,
        as_of: date | None = None,
        start: date | None = None,
    ) -> list[SignalObservation]:
        observations: list[SignalObservation] = []
        cutoff = as_of or date.today()
        for ticker, raw in sorted(daily_map.items()):
            if raw is None or raw.empty:
                continue
            daily = raw.loc[[(_date(index) <= cutoff) for index in raw.index]].copy()
            if daily.empty:
                continue
            observations.extend(self._daily(ticker, daily, cutoff, start))
            observations.extend(self._weekly(ticker, daily, cutoff, start))
        return sorted(observations, key=lambda item: (item.available_date, item.timeframe, item.ticker, item.signal_date))

    def _daily(self, ticker: str, daily: pd.DataFrame, cutoff: date, start: date | None) -> list[SignalObservation]:
        indicator = add_all_indicators(daily)
        result: list[SignalObservation] = []
        for timestamp, row in indicator.loc[indicator["DXDX"].astype(bool)].iterrows():
            signal_date = _date(timestamp)
            available = _daily_available(signal_date)
            if available > cutoff or (start is not None and signal_date < start):
                continue
            trend = bool(row.get("BLUE_ABOVE_YELLOW", False))
            if self.config.daily_signal_mode == "daily_v1" and not trend:
                continue
            result.append(SignalObservation(
                ticker=ticker.upper(), timeframe="daily", signal_date=signal_date,
                available_date=available, close=float(row["close"]),
                blue_above_yellow=trend,
            ))
        return result

    def _weekly(self, ticker: str, daily: pd.DataFrame, cutoff: date, start: date | None) -> list[SignalObservation]:
        weekly = resample_to_unified_week(daily)
        # A Monday-Sunday week is formal only from Monday onward.  The strict
        # '< cutoff' condition rejects the still-forming current week.
        weekly = weekly.loc[[_date(index) < cutoff for index in weekly.index]]
        if weekly.empty:
            return []
        indicator = compute_macd_divergence(weekly)
        result: list[SignalObservation] = []
        for timestamp, row in indicator.loc[indicator["DXDX"].astype(bool)].iterrows():
            week_end = _date(timestamp)
            if start is not None and week_end < start:
                continue
            result.append(SignalObservation(
                ticker=ticker.upper(), timeframe="weekly", signal_date=week_end,
                available_date=week_end + timedelta(days=1), close=float(row["close"]),
                blue_above_yellow=None, weekly_id=_weekly_id(week_end),
            ))
        return result
