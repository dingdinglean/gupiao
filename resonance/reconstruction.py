from __future__ import annotations

from datetime import date, datetime, time, timezone

from .engine import ResonanceEngine
from .models import ResonanceEvent, SignalObservation
from .performance import calculate_event_performance


class HistoricalReconstructor:
    """Replay knowledge dates sequentially so past decisions see no future bars."""

    def __init__(self, engine: ResonanceEngine):
        self.engine = engine

    def reconstruct(
        self,
        observations: list[SignalObservation],
        daily_map: dict,
        *,
        start: date,
        end: date,
    ) -> list[ResonanceEvent]:
        knowledge_dates = sorted({item.available_date for item in observations if start <= item.available_date <= end})
        history: dict[str, ResonanceEvent] = {}
        for current in knowledge_dates:
            visible = [item for item in observations if item.available_date <= current]
            replay_time = datetime.combine(current, time.min, tzinfo=timezone.utc)
            for event in self.engine.evaluate(visible, now=replay_time):
                if event.state != "WATCH" and start <= event.first_known_date <= end:
                    previous = history.get(event.event_id)
                    # Historical detected_at is the deterministic first
                    # knowable timestamp for this event state.  Re-running in
                    # another workflow timezone cannot move it.
                    if previous and previous.state == event.state and previous.aligned_weekly_id == event.aligned_weekly_id:
                        event.detected_at = previous.detected_at
                        event.created_at = previous.created_at
                    history[event.event_id] = event
        result = sorted(history.values(), key=lambda item: (item.first_known_date, item.theme_id, item.cluster_center_date))
        for event in result:
            event.performance = calculate_event_performance(event, daily_map, self.engine.config.forward_sessions)
        return result
