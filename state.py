"""Persistent dedup for alerts.

Stores keys like 'AAPL|2026-05-13|2026-05-14T13:30' so the same signal
isn't pushed twice. Keys older than `ttl_days` are pruned on load.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


class AlertState:
    def __init__(self, path: str | Path = "alert_state.json", ttl_days: int = 7):
        self.path = Path(path)
        self.ttl = timedelta(days=ttl_days)
        self._data: dict[str, str] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                self._data = {}
        self._prune()

    def _prune(self):
        cutoff = datetime.now() - self.ttl
        kept = {}
        for k, v in self._data.items():
            try:
                if datetime.fromisoformat(v) > cutoff:
                    kept[k] = v
            except Exception:
                pass
        self._data = kept

    def save(self):
        self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False))

    @staticmethod
    def key_for(hit) -> str:
        daily = hit.daily_signal_at.isoformat() if hit.daily_signal_at else "-"
        h4 = hit.h4_signal_at.isoformat() if hit.h4_signal_at else "-"
        return f"{hit.symbol}|{daily}|{h4}"

    def is_new(self, hit) -> bool:
        return self.key_for(hit) not in self._data

    def mark_sent(self, hit):
        self._data[self.key_for(hit)] = datetime.now().isoformat()

    def filter_new(self, hits: list) -> list:
        return [h for h in hits if self.is_new(h)]
