from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pandas as pd

from session_calendar import XNYS

from .models import Evidence, SignalCluster


def trading_day_distance(left: date, right: date) -> int:
    """Number of XNYS sessions separating two chart dates.

    Weekend-only assets are intentionally projected onto the same US-session
    axis, so a Saturday crypto bar can align with the adjacent Friday market
    bar without broadening the configured tolerance.
    """
    if left == right:
        return 0
    start, end = sorted((left, right))
    schedule = XNYS.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    if schedule.empty:
        return 0
    session_dates = [pd.Timestamp(item).date() for item in schedule.index]
    first = max((index for index, value in enumerate(session_dates) if value <= start), default=0)
    last = max((index for index, value in enumerate(session_dates) if value <= end), default=0)
    return max(0, last - first)


def _center_date(items: list[Evidence]) -> date:
    counts = Counter(item.signal_date for item in items)
    return sorted(counts, key=lambda item: (-counts[item], item))[0]


class SignalClusterBuilder:
    """Build strict date clusters before any resonance state is evaluated."""

    def __init__(self, daily_tolerance: int = 1, weekly_tolerance: int = 0):
        if daily_tolerance < 0 or weekly_tolerance < 0:
            raise ValueError("cluster tolerances cannot be negative")
        self.daily_tolerance = daily_tolerance
        self.weekly_tolerance = weekly_tolerance

    def build(self, evidence: list[Evidence], timeframe: str) -> list[SignalCluster]:
        scoped = [item for item in evidence if item.observation.timeframe == timeframe]
        by_theme: dict[str, list[Evidence]] = {}
        for item in scoped:
            by_theme.setdefault(item.theme_id, []).append(item)
        clusters: list[SignalCluster] = []
        for items in by_theme.values():
            clusters.extend(self._daily(items) if timeframe == "daily" else self._weekly(items))
        return sorted(clusters, key=lambda item: (item.cluster_center_date, item.theme_id))

    def _daily(self, items: list[Evidence]) -> list[SignalCluster]:
        groups: list[list[Evidence]] = []
        for item in sorted(items, key=lambda value: (value.signal_date, value.ticker)):
            if not groups or trading_day_distance(groups[-1][0].signal_date, item.signal_date) > self.daily_tolerance:
                groups.append([item])
            else:
                groups[-1].append(item)
        return [self._cluster(group, "daily") for group in groups]

    def _weekly(self, items: list[Evidence]) -> list[SignalCluster]:
        groups: list[list[Evidence]] = []
        for item in sorted(items, key=lambda value: (value.signal_date, value.ticker)):
            if not groups:
                groups.append([item])
                continue
            span_weeks = (item.signal_date - groups[-1][0].signal_date).days // 7
            if span_weeks > self.weekly_tolerance:
                groups.append([item])
            else:
                groups[-1].append(item)
        return [self._cluster(group, "weekly") for group in groups]

    @staticmethod
    def _cluster(group: list[Evidence], timeframe: str) -> SignalCluster:
        ordered = sorted(group, key=lambda item: (item.signal_date, item.ticker))
        center = _center_date(ordered)
        weekly_ids = {item.observation.weekly_id for item in ordered if item.observation.weekly_id}
        weekly_id = sorted(weekly_ids)[0] if len(weekly_ids) == 1 else ""
        return SignalCluster(
            theme_id=ordered[0].theme_id,
            theme_display_name=ordered[0].theme_display_name,
            timeframe=timeframe,
            cluster_start_date=ordered[0].signal_date,
            cluster_end_date=ordered[-1].signal_date,
            cluster_center_date=center,
            evidence=tuple(ordered),
            weekly_id=weekly_id,
        )


def natural_week_bounds(week_end: date) -> tuple[date, date]:
    return week_end - timedelta(days=6), week_end
