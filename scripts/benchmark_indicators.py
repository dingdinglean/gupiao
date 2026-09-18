"""Non-network benchmark for the vectorised cd.docx indicator primitives.

Run with ``python scripts/benchmark_indicators.py``.  It intentionally uses a
fixed synthetic history and a literal loop reference so timing never changes
the formula contract.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from indicators import compute_macd_divergence  # noqa: E402
from tests.test_indicator_vectorization import reference_full  # noqa: E402


def frame_for_benchmark(bars: int) -> pd.DataFrame:
    rng = np.random.default_rng(20260918)
    close = 100 + np.cumsum(rng.normal(0, 1.2, bars))
    return pd.DataFrame({
        "open": close - .2, "high": close + 1, "low": close - 1,
        "close": close, "volume": 1_000_000,
    }, index=pd.date_range("1980-01-01", periods=bars, freq="B"))


def main() -> int:
    bars = 10_000
    frame = frame_for_benchmark(bars)
    started = time.perf_counter()
    old = reference_full(frame)
    old_seconds = time.perf_counter() - started
    started = time.perf_counter()
    new = compute_macd_divergence(frame)
    new_seconds = time.perf_counter() - started
    for column in old:
        if old[column].dtype == bool:
            if old[column].tolist() != new[column].tolist():
                raise AssertionError(f"DXDX formula parity failed: {column}")
        else:
            np.testing.assert_allclose(old[column], new[column], equal_nan=True, err_msg=column)
    print(f"bars={bars}")
    print(f"old_runtime_seconds={old_seconds:.6f}")
    print(f"new_runtime_seconds={new_seconds:.6f}")
    print(f"speedup={old_seconds / new_seconds:.2f}x")
    print("dxdx_parity=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
