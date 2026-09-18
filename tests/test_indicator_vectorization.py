"""Independent parity coverage for the NumPy dynamic-indicator fast path."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from indicators import (
    add_all_indicators,
    barslast,
    compute_macd_divergence,
    hhv_dyn,
    llv_dyn,
    ref_dyn,
)


def reference_barslast(condition: pd.Series) -> pd.Series:
    out = np.full(len(condition), np.nan)
    last = -1
    for i, value in enumerate(condition.fillna(False).astype(bool)):
        if value:
            last = i
            out[i] = 0
        elif last >= 0:
            out[i] = i - last
    return pd.Series(out, index=condition.index)


def reference_llv(values: pd.Series, lookback: pd.Series) -> pd.Series:
    out = np.full(len(values), np.nan)
    raw = values.to_numpy(dtype=float)
    for i, lb in enumerate(lookback.to_numpy(dtype=float)):
        if np.isnan(lb):
            continue
        width = max(int(lb), 1)
        window = raw[max(0, i - width + 1):i + 1]
        finite = window[~np.isnan(window)]
        if finite.size:
            out[i] = finite.min()
    return pd.Series(out, index=values.index)


def reference_hhv(values: pd.Series, lookback: pd.Series) -> pd.Series:
    out = np.full(len(values), np.nan)
    raw = values.to_numpy(dtype=float)
    for i, lb in enumerate(lookback.to_numpy(dtype=float)):
        if np.isnan(lb):
            continue
        width = max(int(lb), 1)
        window = raw[max(0, i - width + 1):i + 1]
        finite = window[~np.isnan(window)]
        if finite.size:
            out[i] = finite.max()
    return pd.Series(out, index=values.index)


def reference_ref(values: pd.Series, lookback: pd.Series) -> pd.Series:
    out = np.full(len(values), np.nan)
    raw = values.to_numpy(dtype=float)
    for i, lb in enumerate(lookback.to_numpy(dtype=float)):
        if np.isnan(lb):
            continue
        source = i - int(lb)
        if 0 <= source < len(raw):
            out[i] = raw[source]
    return pd.Series(out, index=values.index)


def _bfalse(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(bool)


def reference_full(frame: pd.DataFrame) -> pd.DataFrame:
    """Literal pre-vectorisation formula using only local loop primitives."""
    close = frame["close"]
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea) * 2
    down = (macd.shift(1) >= 0) & (macd < 0)
    up = (macd.shift(1) <= 0) & (macd > 0)
    n1 = reference_barslast(down).where(down.astype(float).rolling(100, min_periods=1).sum() > 0, 0).fillna(0)
    mm1 = reference_barslast(up).where(up.astype(float).rolling(100, min_periods=1).sum() > 0, 0).fillna(0)

    cc1 = reference_llv(close, n1 + 1)
    cc2 = reference_ref(cc1, (mm1 + 1).clip(lower=1))
    cc3 = reference_ref(cc2, (mm1 + 1).clip(lower=1))
    difl1 = reference_llv(dif, n1 + 1)
    difl2 = reference_ref(difl1, (mm1 + 1).clip(lower=1))
    difl3 = reference_ref(difl2, (mm1 + 1).clip(lower=1))
    aaa = (cc1 < cc2) & (difl1 > difl2) & (macd.shift(1) < 0) & (dif < 0)
    bbb = (cc1 < cc3) & (difl1 < difl2) & (difl1 > difl3) & (macd.shift(1) < 0) & (dif < 0)
    ccc = (aaa | bbb) & (dif < 0)
    lll = (~_bfalse(ccc.shift(1))) & _bfalse(ccc)
    jjj = _bfalse(ccc.shift(1)) & (dif.shift(1).abs() >= dif.abs() * 1.01)
    dxdx = (~_bfalse(jjj.shift(1))) & _bfalse(jjj)

    ch1 = reference_hhv(close, mm1 + 1)
    ch2 = reference_ref(ch1, (n1 + 1).clip(lower=1))
    ch3 = reference_ref(ch2, (n1 + 1).clip(lower=1))
    difh1 = reference_hhv(dif, mm1 + 1)
    difh2 = reference_ref(difh1, (n1 + 1).clip(lower=1))
    difh3 = reference_ref(difh2, (n1 + 1).clip(lower=1))
    zj = (ch1 > ch2) & (difh1 < difh2) & (macd.shift(1) > 0) & (dif > 0)
    gx = (ch1 > ch3) & (difh1 > difh2) & (difh1 < difh3) & (macd.shift(1) > 0) & (dif > 0)
    dbbl = (zj | gx) & (dif > 0)
    dbl = (~_bfalse(dbbl.shift(1))) & _bfalse(dbbl) & (dif > dea)
    dbjg = _bfalse(dbbl.shift(1)) & (dif.shift(1) >= dif * 1.01)
    dbjgxc = (~_bfalse(dbjg.shift(1))) & _bfalse(dbjg)
    return pd.DataFrame({
        "DIF": dif, "DEA": dea, "MACD_bar": macd, "N1": n1, "MM1": mm1,
        "CC1": cc1, "CC2": cc2, "CC3": cc3, "DIFL1": difl1, "DIFL2": difl2,
        "DIFL3": difl3, "AAA": _bfalse(aaa), "BBB": _bfalse(bbb), "LLL": _bfalse(lll),
        "CCC": _bfalse(ccc), "JJJ": _bfalse(jjj), "DXDX": _bfalse(dxdx),
        "DBL": _bfalse(dbl), "DBJGXC": _bfalse(dbjgxc),
    }, index=frame.index)


def fixture_frame(n: int = 1400) -> pd.DataFrame:
    rng = np.random.default_rng(20260918)
    close = 100 + np.cumsum(rng.normal(0, 1.4, n))
    index = pd.date_range("2015-01-02", periods=n, freq="B")
    return pd.DataFrame({
        "open": close - .2,
        "high": close + rng.uniform(.1, 1.5, n),
        "low": close - rng.uniform(.1, 1.5, n),
        "close": close,
        "volume": 1_000_000,
    }, index=index)


class DynamicPrimitiveParityTests(unittest.TestCase):
    def test_random_primitives_match_literal_reference_at_100_500_and_5000_bars(self):
        rng = np.random.default_rng(78)
        for n in (100, 500, 5000):
            index = pd.RangeIndex(n)
            values = pd.Series(rng.normal(size=n), index=index)
            values.iloc[::37] = np.nan
            lookback = pd.Series(rng.integers(-3, 101, size=n).astype(float), index=index)
            lookback.iloc[::29] = np.nan
            condition = pd.Series(rng.choice([True, False, None], size=n, p=[.16, .76, .08]), index=index)
            np.testing.assert_allclose(barslast(condition), reference_barslast(condition), equal_nan=True)
            np.testing.assert_allclose(llv_dyn(values, lookback), reference_llv(values, lookback), equal_nan=True)
            np.testing.assert_allclose(hhv_dyn(values, lookback), reference_hhv(values, lookback), equal_nan=True)
            np.testing.assert_allclose(ref_dyn(values, lookback), reference_ref(values, lookback), equal_nan=True)

    def test_dynamic_boundaries_nan_and_100_bar_window_match_literal_reference(self):
        values = pd.Series([np.nan, 5, 4, np.nan, 3, 6, 2, 8, 1, 9], dtype=float)
        lookback = pd.Series([np.nan, 0, 1, 2, 100, -3, 1.9, 2.9, 100, 1], dtype=float)
        np.testing.assert_allclose(llv_dyn(values, lookback), reference_llv(values, lookback), equal_nan=True)
        np.testing.assert_allclose(hhv_dyn(values, lookback), reference_hhv(values, lookback), equal_nan=True)
        np.testing.assert_allclose(ref_dyn(values, lookback), reference_ref(values, lookback), equal_nan=True)

    def test_full_cd_docx_bottom_and_sell_side_formula_match_literal_pre_vectorisation(self):
        frame = fixture_frame()
        expected = reference_full(frame)
        actual = compute_macd_divergence(frame)
        for name in expected:
            if expected[name].dtype == bool:
                self.assertListEqual(expected[name].tolist(), actual[name].tolist(), name)
            else:
                np.testing.assert_allclose(expected[name], actual[name], equal_nan=True, err_msg=name)

    def test_ema_channels_and_daily_diagnostics_inputs_remain_identical(self):
        frame = fixture_frame(700)
        result = add_all_indicators(frame)
        for source, period, target in (("high", 23, "BLUE_UP"), ("low", 23, "BLUE_DW"), ("high", 89, "YELLOW_UP"), ("low", 89, "YELLOW_DW")):
            expected = frame[source].ewm(span=period, adjust=False).mean()
            np.testing.assert_allclose(result[target], expected, equal_nan=True, err_msg=target)
        self.assertListEqual(
            result["BLUE_ABOVE_YELLOW"].tolist(),
            ((result["BLUE_UP"] > result["YELLOW_UP"]) & (result["BLUE_DW"] > result["YELLOW_DW"])).tolist(),
        )


if __name__ == "__main__":
    unittest.main()
