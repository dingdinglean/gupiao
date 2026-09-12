"""Independent, literal cd.docx bottom-side reference parity tests."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from indicators import compute_macd_divergence


def reference_bottom(close: pd.Series) -> pd.DataFrame:
    """Deliberate standalone bar-by-bar translation; no production helpers."""
    n = len(close)
    dif = np.empty(n); dea = np.empty(n)
    for i, value in enumerate(close.astype(float)):
        dif[i] = 0.0 if i == 0 else dif[i - 1]
        # Literal recursive EMA seed = first value for each input series.
        fast = value if i == 0 else fast + 2 / 13 * (value - fast)
        slow = value if i == 0 else slow + 2 / 27 * (value - slow)
        dif[i] = fast - slow
        dea[i] = dif[i] if i == 0 else dea[i - 1] + 2 / 10 * (dif[i] - dea[i - 1])
    macd = (dif - dea) * 2
    n1 = np.zeros(n); mm1 = np.zeros(n)
    for i in range(n):
        down = [j for j in range(max(1, i - 99), i + 1) if macd[j - 1] >= 0 and macd[j] < 0]
        up = [j for j in range(max(1, i - 99), i + 1) if macd[j - 1] <= 0 and macd[j] > 0]
        n1[i] = i - down[-1] if down else 0
        mm1[i] = i - up[-1] if up else 0
    cc1 = np.empty(n); cc2 = np.full(n, np.nan); cc3 = np.full(n, np.nan)
    dl1 = np.empty(n); dl2 = np.full(n, np.nan); dl3 = np.full(n, np.nan)
    for i in range(n):
        cc1[i] = np.min(close.iloc[max(0, i - int(n1[i])):i + 1])
        dl1[i] = np.min(dif[max(0, i - int(n1[i])):i + 1])
        offset = max(1, int(mm1[i]) + 1)
        if i >= offset:
            cc2[i], dl2[i] = cc1[i - offset], dl1[i - offset]
        if i >= offset:
            # REF(CC2, MAX(1, MM1+1)): the second REF uses today's dynamic
            # offset against the already-computed CC2/DIFL2 series.
            cc3[i], dl3[i] = cc2[i - offset], dl2[i - offset]
    aaa = np.zeros(n, dtype=bool); bbb = np.zeros(n, dtype=bool)
    ccc = np.zeros(n, dtype=bool); jjj = np.zeros(n, dtype=bool); dxdx = np.zeros(n, dtype=bool)
    for i in range(1, n):
        aaa[i] = cc1[i] < cc2[i] and dl1[i] > dl2[i] and macd[i - 1] < 0 and dif[i] < 0
        bbb[i] = cc1[i] < cc3[i] and dl1[i] < dl2[i] and dl1[i] > dl3[i] and macd[i - 1] < 0 and dif[i] < 0
        ccc[i] = (aaa[i] or bbb[i]) and dif[i] < 0
        jjj[i] = ccc[i - 1] and abs(dif[i - 1]) >= abs(dif[i]) * 1.01
        dxdx[i] = (not jjj[i - 1]) and jjj[i]
    return pd.DataFrame({"DIF": dif, "DEA": dea, "MACD_bar": macd, "N1": n1, "MM1": mm1,
                         "CC1": cc1, "CC2": cc2, "CC3": cc3, "DIFL1": dl1, "DIFL2": dl2,
                         "DIFL3": dl3, "AAA": aaa, "BBB": bbb, "CCC": ccc, "JJJ": jjj,
                         "DXDX": dxdx}, index=close.index)


class DXDXFormulaParityTests(unittest.TestCase):
    def test_cd_docx_reference_matches_production_for_multiple_regimes(self):
        rng = np.random.default_rng(42)
        close = np.r_[np.linspace(100, 60, 140), np.linspace(60, 95, 80),
                      np.linspace(95, 45, 130), np.linspace(45, 80, 90)] + rng.normal(0, .15, 440)
        index = pd.date_range("2024-01-02", periods=len(close), freq="B")
        frame = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1}, index=index)
        expected, actual = reference_bottom(frame["close"]), compute_macd_divergence(frame)
        for name in expected.columns:
            if expected[name].dtype == bool:
                self.assertListEqual(expected[name].tolist(), actual[name].tolist(), name)
            else:
                np.testing.assert_allclose(expected[name], actual[name], equal_nan=True, err_msg=name)

    def test_100_bar_reset_and_first_jjj_only_dxdx(self):
        close = pd.Series(np.r_[np.linspace(100, 50, 150), np.linspace(50, 51, 120)], index=pd.date_range("2024-01-02", periods=270, freq="B"))
        expected, actual = reference_bottom(close), compute_macd_divergence(pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1}))
        np.testing.assert_allclose(expected["N1"], actual["N1"])
        self.assertListEqual(expected["DXDX"].tolist(), actual["DXDX"].tolist())
