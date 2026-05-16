"""Persistent dedup for alerts."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


class AlertState:
    def __init__(self, path="alert_state.json", ttl_days=7):
        self.path = Path(path)
        self.ttl = timedelta(days=ttl_days)
        self._data = {}
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
        d = hit.daily_signal_at.isoformat() if hit.daily_signal_at else "-"
        h = hit.h4_signal_at.isoformat() if hit.h4_signal_at else "-"
        return f"{hit.symbol}|{d}|{h}"

    def is_new(self, hit) -> bool:
        return self.key_for(hit) not in self._data

    def mark_sent(self, hit):
        self._data[self.key_for(hit)] = datetime.now().isoformat()

    def filter_new(self, hits):
        return [h for h in hits if self.is_new(h)]
