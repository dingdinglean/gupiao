from __future__ import annotations

from datetime import date

import pandas as pd

from .models import ResonanceEvent


def calculate_event_performance(
    event: ResonanceEvent,
    daily_map: dict[str, pd.DataFrame],
    forward_sessions: tuple[int, ...] = (1, 3, 5, 10, 20),
) -> dict:
    """Equal-weight participant returns, kept separate from individual history."""
    returns: dict[int, list[float]] = {period: [] for period in forward_sessions}
    participant_count = 0
    for ticker in event.tickers:
        frame = daily_map.get(ticker)
        if frame is None or frame.empty or "close" not in frame:
            continue
        dates = [pd.Timestamp(index).date() for index in frame.index]
        baseline_positions = [index for index, value in enumerate(dates) if value <= event.first_known_date]
        if not baseline_positions:
            continue
        baseline_position = baseline_positions[-1]
        baseline = float(frame.iloc[baseline_position]["close"])
        if baseline <= 0:
            continue
        participant_count += 1
        future = frame.iloc[baseline_position + 1:]
        for period in forward_sessions:
            if len(future) >= period:
                returns[period].append((float(future.iloc[period - 1]["close"]) / baseline - 1.0) * 100.0)
    result = {
        "method": "参与标的等权收益",
        "baseline_date": event.first_known_date.isoformat(),
        "participant_count": participant_count,
    }
    for period in forward_sessions:
        values = returns[period]
        result[f"return_t{period}"] = round(sum(values) / len(values), 3) if values else ""
    return result
