"""End-to-end indicator smoke test.

The historical root script only printed results, so `unittest discover -s tests`
never executed it. These assertions use the same synthetic series.
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from indicators import add_all_indicators


def make_synthetic_ohlc(n: int = 400, seed: int = 7) -> pd.DataFrame:
    """A noisy mean-reverting series with an embedded V-shaped bottom."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    trend = 100 + 0.05 * t + 6 * np.sin(t / 18)
    v_start = n - 80
    v_low = n - 40
    v_depth = 18
    trend[v_start:v_low] -= np.linspace(0, v_depth, v_low - v_start)
    trend[v_low:] -= np.linspace(v_depth, 0, n - v_low)

    noise = rng.normal(0, 0.4, n)
    close = trend + noise
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 0.6, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 0.6, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": rng.integers(1e6, 1e7, n)},
        index=idx,
    )


def main() -> None:
    out = add_all_indicators(make_synthetic_ohlc())
    print(f"Bars:          {len(out)}")
    print(f"DIF range:     {out['DIF'].min():.3f}  ..  {out['DIF'].max():.3f}")
    print(f"DXDX hits:     {int(out['DXDX'].sum())}")
    print(f"LLL hits:      {int(out['LLL'].sum())}")
    print(f"DBJGXC hits:   {int(out['DBJGXC'].sum())}")
    print(f"DBL hits:      {int(out['DBL'].sum())}")
    print(f"蓝>黄 (松):    {int(out['BLUE_ABOVE_YELLOW'].sum())} bars")
    print(f"蓝>黄 (严):    {int(out['BLUE_FULLY_ABOVE_YELLOW'].sum())} bars")


class IndicatorPipelineTests(unittest.TestCase):
    def test_synthetic_series_runs_existing_indicator_pipeline(self):
        out = add_all_indicators(make_synthetic_ohlc())
        self.assertEqual(len(out), 400)
        for column in ("DIF", "DXDX", "LLL", "DBJGXC", "DBL", "BLUE_ABOVE_YELLOW", "BLUE_FULLY_ABOVE_YELLOW"):
            self.assertIn(column, out.columns)
        self.assertTrue(np.isfinite(out["DIF"].dropna()).all())
        # Counts recorded from the seed=7 series on the pinned dependency set.
        self.assertEqual(int(out["DXDX"].sum()), 3)
        self.assertEqual(int(out["LLL"].sum()), 2)
        self.assertEqual(int(out["DBJGXC"].sum()), 0)
        self.assertEqual(int(out["DBL"].sum()), 1)
        self.assertEqual(int(out["BLUE_ABOVE_YELLOW"].sum()), 232)
        self.assertEqual(int(out["BLUE_FULLY_ABOVE_YELLOW"].sum()), 194)


if __name__ == "__main__":
    main()
    unittest.main()
