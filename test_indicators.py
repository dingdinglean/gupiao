"""Sanity test: build a synthetic price series and verify the indicator
pipeline runs end-to-end and produces signals.

Run:  python test_indicators.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import add_all_indicators


def make_synthetic_ohlc(n: int = 400, seed: int = 7) -> pd.DataFrame:
    """A noisy mean-reverting series with an embedded V-shaped bottom."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    trend = 100 + 0.05 * t + 6 * np.sin(t / 18)
    # Force a clean V-shape near the end (to trigger 抄底)
    v_start = n - 80
    v_low = n - 40
    v_depth = 18
    trend[v_start:v_low] -= np.linspace(0, v_depth, v_low - v_start)
    trend[v_low:] -= np.linspace(v_depth, 0, n - v_low)

    noise = rng.normal(0, 0.4, n)
    close = trend + noise
    open_ = close + rng.normal(0, 0.3, n)
    high  = np.maximum(open_, close) + rng.uniform(0.1, 0.6, n)
    low   = np.minimum(open_, close) - rng.uniform(0.1, 0.6, n)

    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                          "volume": rng.integers(1e6, 1e7, n)}, index=idx)


def main():
    df = make_synthetic_ohlc()
    out = add_all_indicators(df)

    print(f"Bars:          {len(out)}")
    print(f"DIF range:     {out['DIF'].min():.3f}  ..  {out['DIF'].max():.3f}")
    print(f"DXDX hits:     {int(out['DXDX'].sum())}")
    print(f"LLL hits:      {int(out['LLL'].sum())}")
    print(f"DBJGXC hits:   {int(out['DBJGXC'].sum())}")
    print(f"DBL hits:      {int(out['DBL'].sum())}")
    print(f"蓝>黄 (松):    {int(out['BLUE_ABOVE_YELLOW'].sum())} bars")
    print(f"蓝>黄 (严):    {int(out['BLUE_FULLY_ABOVE_YELLOW'].sum())} bars")

    last_signals = out[(out["DXDX"]) | (out["DBJGXC"])].tail(10)
    if not last_signals.empty:
        print("\nLast few signals (date, signal type, close, DIF):")
        for ts, row in last_signals.iterrows():
            kind = "抄底" if row["DXDX"] else "卖出"
            print(f"  {ts:%Y-%m-%d}  {kind}  close={row['close']:.2f}  DIF={row['DIF']:.3f}")
    else:
        print("\nNo signals fired on the synthetic data (this can happen, change seed).")


if __name__ == "__main__":
    main()
