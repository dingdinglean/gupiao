from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


def _serialize(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class SignalObservation:
    ticker: str
    timeframe: str
    signal_date: date
    available_date: date
    close: float
    dxdx: bool = True
    # Diagnostic only.  Collective-behaviour eligibility is raw DXDX and
    # date alignment; this value must never be used by clustering or state.
    trend_filter_pass: bool | None = None
    weekly_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass(frozen=True)
class Evidence:
    theme_id: str
    theme_display_name: str
    subgroup_id: str
    subgroup_display_name: str
    role: str
    observation: SignalObservation

    @property
    def ticker(self) -> str:
        return self.observation.ticker

    @property
    def signal_date(self) -> date:
        return self.observation.signal_date

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return _serialize(value)


@dataclass(frozen=True)
class SignalCluster:
    theme_id: str
    theme_display_name: str
    timeframe: str
    cluster_start_date: date
    cluster_end_date: date
    cluster_center_date: date
    evidence: tuple[Evidence, ...]
    weekly_id: str = ""

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted({item.ticker for item in self.evidence}))

    @property
    def subgroups(self) -> tuple[str, ...]:
        return tuple(sorted({item.subgroup_id for item in self.evidence}))

    @property
    def first_known_date(self) -> date:
        return max(item.observation.available_date for item in self.evidence)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass(frozen=True)
class FollowUpSignal:
    ticker: str
    subgroup_id: str
    subgroup_display_name: str
    role: str
    timeframe: str
    signal_date: date
    weekly_id: str = ""
    relation: str = "follow_up"

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class ResonanceEvent:
    event_id: str
    theme_id: str
    display_name: str
    timeframe: str
    cluster_center_date: date
    cluster_start_date: date
    cluster_end_date: date
    state: str
    tickers: tuple[str, ...]
    subgroups: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    first_known_date: date
    weekly_id: str = ""
    aligned_weekly_id: str = ""
    aligned_week_start: date | None = None
    aligned_week_end: date | None = None
    etf_signaled: int = 0
    etf_total: int = 0
    follow_up_signals: tuple[FollowUpSignal, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    performance: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.state,
            self.cluster_start_date,
            self.cluster_end_date,
            self.tickers,
            self.subgroups,
            self.aligned_weekly_id,
            tuple((item.ticker, item.timeframe, item.signal_date) for item in self.follow_up_signals),
        )

    @property
    def synchronous_tickers(self) -> tuple[str, ...]:
        return self.tickers

    @property
    def synchronous_subgroups(self) -> tuple[str, ...]:
        return self.subgroups

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["synchronous_tickers"] = self.synchronous_tickers
        result["synchronous_subgroups"] = self.synchronous_subgroups
        return _serialize(result)


@dataclass(frozen=True)
class ResonanceChange:
    change_type: str
    change_display_name: str
    event: ResonanceEvent
    previous_state: str = ""
    added_tickers: tuple[str, ...] = ()
    added_subgroups: tuple[str, ...] = ()
    added_follow_ups: tuple[FollowUpSignal, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))
