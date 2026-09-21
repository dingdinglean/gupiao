from __future__ import annotations

from .config import ResonanceConfig
from .models import Evidence, SignalObservation


class ThemeMapper:
    def __init__(self, config: ResonanceConfig):
        self.config = config
        self._index: dict[str, list[tuple[str, object]]] = {}
        for theme in config.themes.values():
            for group in theme.groups:
                for ticker in group.tickers:
                    self._index.setdefault(ticker, []).append((theme.id, group))

    def map_observations(self, observations: list[SignalObservation]) -> list[Evidence]:
        result: list[Evidence] = []
        seen: set[tuple[str, str, str, str]] = set()
        for observation in observations:
            for theme_id, group in self._index.get(observation.ticker.upper(), []):
                theme = self.config.themes[theme_id]
                key = (theme_id, observation.timeframe, observation.ticker, observation.signal_date.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                result.append(Evidence(
                    theme_id=theme_id,
                    theme_display_name=theme.display_name,
                    subgroup_id=group.id,
                    subgroup_display_name=group.display_name,
                    role=group.role,
                    observation=observation,
                ))
        return result
