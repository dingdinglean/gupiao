"""Persistent, per-candle DXDX alert deduplication."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


class AlertState:
    def __init__(self, path: str | Path = "alert_state.json", ttl_days: int = 30):
        self.path = Path(path)
        self.ttl = timedelta(days=ttl_days)
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, json.JSONDecodeError):
            self._data = {}
        self._prune()

    def _prune(self) -> None:
        cutoff = datetime.now() - self.ttl
        self._data = {key: value for key, value in self._data.items() if self._timestamp_is_recent(value, cutoff)}

    @staticmethod
    def _timestamp_is_recent(value: str, cutoff: datetime) -> bool:
        try:
            timestamp = datetime.fromisoformat(value)
            return timestamp.replace(tzinfo=None) > cutoff
        except (TypeError, ValueError):
            return False

    @staticmethod
    def key_for(symbol: str, timeframe: str, signal_bar_timestamp: datetime) -> str:
        return f"{symbol.upper()}|{timeframe.upper()}|{signal_bar_timestamp.isoformat()}"

    def keys_for(self, signal) -> list[str]:
        return [self.key_for(signal.symbol, timeframe, timestamp) for timeframe, timestamp in signal.signal_keys()]

    def is_new(self, signal) -> bool:
        return any(key not in self._data for key in self.keys_for(signal))

    def filter_new(self, signals: list) -> list:
        return [signal for signal in signals if self.is_new(signal)]

    def mark_sent(self, signal) -> None:
        recorded_at = datetime.now().isoformat()
        for key in self.keys_for(signal):
            self._data[key] = recorded_at

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
