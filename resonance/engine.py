from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .cluster_builder import SignalClusterBuilder, natural_week_bounds
from .config import ResonanceConfig
from .mapper import ThemeMapper
from .models import Evidence, ResonanceEvent, SignalCluster, SignalObservation


def _first_resonance_known(cluster: SignalCluster, minimum: int) -> date:
    subgroups: set[str] = set()
    for item in sorted(cluster.evidence, key=lambda value: (value.observation.available_date, value.signal_date, value.ticker)):
        subgroups.add(item.subgroup_id)
        if len(subgroups) >= minimum:
            return item.observation.available_date
    return cluster.first_known_date


class ResonanceEngine:
    def __init__(self, config: ResonanceConfig):
        self.config = config
        self.mapper = ThemeMapper(config)
        self.cluster_builder = SignalClusterBuilder(config.daily_tolerance, config.weekly_tolerance)

    def evaluate(self, observations: list[SignalObservation], *, now: datetime | None = None) -> list[ResonanceEvent]:
        evidence = self.mapper.map_observations(observations)
        daily_clusters = self.cluster_builder.build(evidence, "daily")
        weekly_clusters = self.cluster_builder.build(evidence, "weekly")
        daily_events = [self._event(cluster) for cluster in daily_clusters]
        weekly_events = [self._event(cluster) for cluster in weekly_clusters]
        aligned_weekly_ids: set[str] = set()
        for daily_event in daily_events:
            if len(daily_event.subgroups) < self.config.minimum_subgroups:
                continue
            candidates = [
                event for event in weekly_events
                if event.theme_id == daily_event.theme_id
                and len(event.subgroups) >= self.config.minimum_subgroups
                and self._aligned(daily_event, event)
            ]
            if not candidates:
                continue
            weekly = min(candidates, key=lambda item: abs((item.cluster_center_date - daily_event.cluster_center_date).days))
            aligned_weekly_ids.add(weekly.event_id)
            daily_event.state = "MULTI_TIMEFRAME_RESONANCE"
            daily_event.aligned_weekly_id = weekly.weekly_id
            daily_event.aligned_week_start, daily_event.aligned_week_end = natural_week_bounds(weekly.cluster_center_date)
            combined = list(daily_event.evidence)
            seen = {(item.ticker, item.observation.timeframe, item.signal_date) for item in combined}
            for item in weekly.evidence:
                key = (item.ticker, item.observation.timeframe, item.signal_date)
                if key not in seen:
                    combined.append(item)
                    seen.add(key)
            daily_event.evidence = tuple(sorted(combined, key=lambda item: (item.observation.timeframe, item.signal_date, item.ticker)))
            daily_event.tickers = tuple(sorted({item.ticker for item in combined}))
            daily_event.subgroups = tuple(sorted({item.subgroup_id for item in combined}))
            daily_event.first_known_date = max(daily_event.first_known_date, weekly.first_known_date)
        # An aligned weekly cluster is represented by the richer daily event,
        # preventing duplicate notifications for one market low region.
        result = daily_events + [event for event in weekly_events if event.event_id not in aligned_weekly_ids]
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        for event in result:
            event.created_at = event.created_at or stamp
            event.updated_at = stamp
        return sorted(result, key=lambda item: (item.first_known_date, item.theme_id, item.timeframe, item.cluster_center_date))

    def _event(self, cluster: SignalCluster) -> ResonanceEvent:
        subgroups = cluster.subgroups
        subgroup_count = len(subgroups)
        if subgroup_count < self.config.minimum_subgroups:
            state = "WATCH"
        elif subgroup_count >= self.config.broad_subgroups:
            state = "BROAD_RESONANCE"
        else:
            state = "DAILY_RESONANCE" if cluster.timeframe == "daily" else "WEEKLY_RESONANCE"
        theme = self.config.themes[cluster.theme_id]
        etf_total = sum(len(group.tickers) for group in theme.groups if group.role == "etf")
        etf_signaled = len({item.ticker for item in cluster.evidence if item.role == "etf"})
        suffix = cluster.cluster_start_date.isoformat() if cluster.timeframe == "daily" else (cluster.weekly_id or cluster.cluster_center_date.isoformat())
        return ResonanceEvent(
            event_id=f"{cluster.theme_id}:{cluster.timeframe}:{suffix}",
            theme_id=cluster.theme_id,
            display_name=cluster.theme_display_name,
            timeframe=cluster.timeframe,
            cluster_center_date=cluster.cluster_center_date,
            cluster_start_date=cluster.cluster_start_date,
            cluster_end_date=cluster.cluster_end_date,
            state=state,
            tickers=cluster.tickers,
            subgroups=subgroups,
            evidence=cluster.evidence,
            first_known_date=_first_resonance_known(cluster, self.config.minimum_subgroups) if subgroup_count >= self.config.minimum_subgroups else cluster.first_known_date,
            weekly_id=cluster.weekly_id,
            etf_signaled=etf_signaled,
            etf_total=etf_total,
        )

    def _aligned(self, daily: ResonanceEvent, weekly: ResonanceEvent) -> bool:
        week_start, week_end = natural_week_bounds(weekly.cluster_center_date)
        margin = timedelta(days=self.config.multi_alignment_days)
        return daily.cluster_end_date >= week_start - margin and daily.cluster_start_date <= week_end + margin
