"""Independent ETF orchestration over the production official-daily scanner."""
from __future__ import annotations

import pandas as pd

from etf_universe import ETF_UNIVERSE, is_etf_symbol
from screener import DailyDiagnostic, ScanStats, Signal, run_confirmation_screener


def run_etf_confirmation_screener(
    session_dates: list[pd.Timestamp],
    *,
    require_strict_separation: bool = False,
    batch_size: int = 50,
) -> tuple[list[Signal], ScanStats, list[DailyDiagnostic]]:
    """Scan the fixed ETF whitelist with the shared batch fetch and indicators."""
    return run_confirmation_screener(
        list(ETF_UNIVERSE),
        session_dates,
        max_workers=1,
        require_strict_separation=require_strict_separation,
        batch_size=batch_size,
        symbol_validator=is_etf_symbol,
        source_radar="etf_daily",
    )
