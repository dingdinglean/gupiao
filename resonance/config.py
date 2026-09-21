from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "themes.json"


@dataclass(frozen=True)
class ThemeGroup:
    id: str
    display_name: str
    role: str
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class ThemeConfig:
    id: str
    display_name: str
    groups: tuple[ThemeGroup, ...]

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(ticker for group in self.groups for ticker in group.tickers))

    def group_for(self, ticker: str) -> ThemeGroup | None:
        normalized = ticker.upper()
        return next((group for group in self.groups if normalized in group.tickers), None)


@dataclass(frozen=True)
class ResonanceConfig:
    daily_tolerance: int
    weekly_tolerance: int
    multi_alignment_days: int
    minimum_subgroups: int
    broad_subgroups: int
    daily_follow_up_max_trading_days: int
    weekly_follow_up_max_weeks: int
    forward_sessions: tuple[int, ...]
    status_display_names: dict[str, str]
    themes: dict[str, ThemeConfig]

    @property
    def tickers(self) -> list[str]:
        return sorted({ticker for theme in self.themes.values() for ticker in theme.tickers})


def load_resonance_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    theme_ids: list[str] | tuple[str, ...] | None = None,
) -> ResonanceConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    engine = raw["engine"]
    selected = set(theme_ids or raw["themes"])
    missing = selected.difference(raw["themes"])
    if missing:
        raise ValueError(f"Unknown theme id(s): {', '.join(sorted(missing))}")
    themes: dict[str, ThemeConfig] = {}
    for theme_id, item in raw["themes"].items():
        if theme_id not in selected:
            continue
        groups = tuple(
            ThemeGroup(
                id=group["id"],
                display_name=group["display_name"],
                role=group["role"],
                tickers=tuple(dict.fromkeys(str(ticker).upper() for ticker in group["tickers"])),
            )
            for group in item["groups"]
        )
        duplicate = [ticker for ticker in {ticker for group in groups for ticker in group.tickers} if sum(ticker in group.tickers for group in groups) > 1]
        if duplicate:
            raise ValueError(f"Theme {theme_id} assigns ticker(s) to multiple subgroups: {duplicate}")
        themes[theme_id] = ThemeConfig(theme_id, item["display_name"], groups)
    return ResonanceConfig(
        daily_tolerance=int(engine["daily_cluster_tolerance_trading_days"]),
        weekly_tolerance=int(engine["weekly_cluster_tolerance_weeks"]),
        multi_alignment_days=int(engine["multi_timeframe_alignment_calendar_days"]),
        minimum_subgroups=int(engine["minimum_resonance_subgroups"]),
        broad_subgroups=int(engine["broad_resonance_subgroups"]),
        daily_follow_up_max_trading_days=int(engine["daily_follow_up_max_trading_days"]),
        weekly_follow_up_max_weeks=int(engine["weekly_follow_up_max_weeks"]),
        forward_sessions=tuple(int(value) for value in engine["history_forward_sessions"]),
        status_display_names=dict(raw["status_display_names"]),
        themes=themes,
    )
