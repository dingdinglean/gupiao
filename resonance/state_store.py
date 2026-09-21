from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from .models import ResonanceChange, ResonanceEvent


STATE_RANK = {
    "WATCH": 0,
    "DAILY_RESONANCE": 1,
    "WEEKLY_RESONANCE": 1,
    "BROAD_RESONANCE": 2,
    "MULTI_TIMEFRAME_RESONANCE": 3,
}

CHANGE_NAMES = {
    "NEW": "新增",
    "UPGRADE": "级别升级",
    "TIMEFRAME_ADDED": "新增周期",
    "SUBGROUP_ADDED": "新增子组",
    "ENHANCED": "共振增强",
}


class ResonanceStateStore:
    def __init__(self, path: str | Path = "data/resonance_state.json"):
        self.path = Path(path)
        self.data: dict = {"version": 1, "events": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("events"), dict):
                self.data = loaded
        except (OSError, json.JSONDecodeError):
            self.data = {"version": 1, "events": {}}

    def apply(self, events: list[ResonanceEvent], *, notify_after: date | None = None) -> list[ResonanceChange]:
        changes: list[ResonanceChange] = []
        stored = self.data.setdefault("events", {})
        now = datetime.now(timezone.utc).isoformat()
        for event in events:
            current = event.to_dict()
            previous = stored.get(event.event_id)
            if previous:
                current["created_at"] = previous.get("created_at") or current["created_at"]
            current["updated_at"] = now
            change = self._change(previous, event)
            stored[event.event_id] = current
            if change and (notify_after is None or event.first_known_date >= notify_after):
                changes.append(change)
        self.data["updated_at"] = now
        return changes

    @staticmethod
    def _change(previous: dict | None, event: ResonanceEvent) -> ResonanceChange | None:
        if event.state == "WATCH":
            return None
        if not previous or previous.get("state") == "WATCH":
            return ResonanceChange("NEW", CHANGE_NAMES["NEW"], event, previous_state=(previous or {}).get("state", ""))
        previous_state = str(previous.get("state", ""))
        if event.aligned_weekly_id and not previous.get("aligned_weekly_id"):
            return ResonanceChange("TIMEFRAME_ADDED", CHANGE_NAMES["TIMEFRAME_ADDED"], event, previous_state=previous_state)
        if STATE_RANK.get(event.state, 0) > STATE_RANK.get(previous_state, 0):
            return ResonanceChange("UPGRADE", CHANGE_NAMES["UPGRADE"], event, previous_state=previous_state)
        old_subgroups = set(previous.get("subgroups") or [])
        added_subgroups = tuple(sorted(set(event.subgroups) - old_subgroups))
        if added_subgroups:
            return ResonanceChange("SUBGROUP_ADDED", CHANGE_NAMES["SUBGROUP_ADDED"], event, previous_state=previous_state, added_subgroups=added_subgroups)
        old_tickers = set(previous.get("tickers") or [])
        added_tickers = tuple(sorted(set(event.tickers) - old_tickers))
        if added_tickers:
            return ResonanceChange("ENHANCED", CHANGE_NAMES["ENHANCED"], event, previous_state=previous_state, added_tickers=added_tickers)
        return None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
