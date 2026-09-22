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
    "ENHANCED": "集体行为增强",
    "FOLLOW_UP": "后续接力",
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

    def apply(
        self,
        events: list[ResonanceEvent],
        *,
        notify_after: date | None = None,
        detected_at: datetime | None = None,
    ) -> list[ResonanceChange]:
        changes: list[ResonanceChange] = []
        stored = self.data.setdefault("events", {})
        now = (detected_at or datetime.now(timezone.utc)).isoformat()
        for event in events:
            previous = stored.get(event.event_id)
            change = self._change(previous, event)
            same_state_version = False
            if previous:
                event.created_at = previous.get("created_at") or event.created_at
                same_state_version = (
                    previous.get("state") == event.state
                    and previous.get("aligned_weekly_id", "") == event.aligned_weekly_id
                )
                if same_state_version:
                    event.detected_at = previous.get("detected_at") or event.detected_at
            if change and not same_state_version:
                event.detected_at = now
            if not change and previous:
                event.notified_at = previous.get("notified_at", "")
            elif change:
                # A new state version has been detected but is not considered
                # notified until the transport succeeds.
                event.notified_at = ""
            event.updated_at = now
            current = event.to_dict()
            current["updated_at"] = now
            stored[event.event_id] = current
            if change and (notify_after is None or event.first_known_date >= notify_after):
                changes.append(change)
        self.data["updated_at"] = now
        return changes

    def mark_notified(self, changes: list[ResonanceChange], *, notified_at: datetime | None = None) -> None:
        """Persist the actual successful notification time for changed events."""
        stamp = (notified_at or datetime.now(timezone.utc)).isoformat()
        stored = self.data.setdefault("events", {})
        for change in changes:
            change.event.notified_at = stamp
            current = stored.get(change.event.event_id)
            if current is not None:
                current["notified_at"] = stamp
                current["updated_at"] = stamp
        self.data["updated_at"] = stamp

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
        old_follow_ups = {
            (item.get("ticker"), item.get("timeframe"), item.get("signal_date"))
            for item in previous.get("follow_up_signals") or []
        }
        added_follow_ups = tuple(
            item for item in event.follow_up_signals
            if (item.ticker, item.timeframe, item.signal_date.isoformat()) not in old_follow_ups
        )
        if added_follow_ups:
            return ResonanceChange(
                "FOLLOW_UP", CHANGE_NAMES["FOLLOW_UP"], event,
                previous_state=previous_state, added_follow_ups=added_follow_ups,
            )
        return None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
