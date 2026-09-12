"""Indicator translations from Tongdaxin/Futubull formula language to Python.

Implements:
  - MACD divergence system (抄底 = DXDX, 卖出 = DBJGXC, 底背离 = LLL, 顶背离 = DBL)
  - Dual EMA channel: BLUE (fast, period 23), YELLOW (slow, period 89)
  - "Blue above yellow" filter (蓝梯 > 黄梯)

The MACD formula follows cd.docx exactly, including its 100-bar zero-cross
window. If no corresponding MACD zero crossing exists within the latest 100
bars, N1/MM1 must reset to 0 instead of looking back indefinitely.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------- Tongdaxin / TDX primitives ----------

def ema(series: pd.Series, n: int) -> pd.Series:
    """EMA (Tongdaxin: alpha = 2/(n+1), no adjust, seed = first value)."""
    return series.ewm(span=n, adjust=False).mean()


def ref(series: pd.Series, n: int) -> pd.Series:
    """REF(X, N) - value N bars ago."""
    return series.shift(n)


def llv(series: pd.Series, n: int) -> pd.Series:
    """LLV(X, N) - lowest in last N bars (inclusive of current)."""
    return series.rolling(n, min_periods=1).min()


def hhv(series: pd.Series, n: int) -> pd.Series:
    """HHV(X, N) - highest in last N bars (inclusive of current)."""
    return series.rolling(n, min_periods=1).max()


def count(condition: pd.Series, n: int) -> pd.Series:
    """COUNT(cond, N) - count of True in last N bars."""
    return condition.astype(float).rolling(n, min_periods=1).sum()


def barslast(condition: pd.Series) -> pd.Series:
    """BARSLAST(cond) - bars since condition was last True."""
    cond_arr = condition.fillna(False).astype(bool).values
    result = np.full(len(cond_arr), np.nan)
    last_idx = -1
    for i in range(len(cond_arr)):
        if cond_arr[i]:
            last_idx = i
            result[i] = 0
        elif last_idx >= 0:
            result[i] = i - last_idx
    return pd.Series(result, index=condition.index)


# ---------- Dynamic-lookback primitives ----------

def _to_int_lookback(value, fallback: int = 1) -> int:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return -1
    v = int(value)
    return v if v >= 1 else fallback


def llv_dyn(series: pd.Series, lookback: pd.Series) -> pd.Series:
    """LLV with per-bar variable lookback. lookback can contain NaN."""
    values = series.values.astype(float)
    lb = lookback.values
    out = np.full(len(series), np.nan)
    for i in range(len(series)):
        n = _to_int_lookback(lb[i])
        if n < 1:
            continue
        start = max(0, i - n + 1)
        window = values[start:i + 1]
        window = window[~np.isnan(window)]
        if window.size:
            out[i] = window.min()
    return pd.Series(out, index=series.index)


def hhv_dyn(series: pd.Series, lookback: pd.Series) -> pd.Series:
    values = series.values.astype(float)
    lb = lookback.values
    out = np.full(len(series), np.nan)
    for i in range(len(series)):
        n = _to_int_lookback(lb[i])
        if n < 1:
            continue
        start = max(0, i - n + 1)
        window = values[start:i + 1]
        window = window[~np.isnan(window)]
        if window.size:
            out[i] = window.max()
    return pd.Series(out, index=series.index)


def ref_dyn(series: pd.Series, lookback: pd.Series) -> pd.Series:
    """REF with per-bar variable lookback."""
    values = series.values.astype(float)
    lb = lookback.values
    out = np.full(len(series), np.nan)
    for i in range(len(series)):
        if np.isnan(lb[i]):
            continue
        idx = i - int(lb[i])
        if 0 <= idx < len(values):
            out[i] = values[idx]
    return pd.Series(out, index=series.index)


def _bfalse(s: pd.Series) -> pd.Series:
    """Coerce bool series, NaN -> False."""
    return s.fillna(False).astype(bool)


def _cd_zero_cross_lookback(condition: pd.Series) -> pd.Series:
    """Exact cd.docx N1/MM1 rule.

    IF(COUNT(condition, 100) > 0, BARSLAST(condition), 0)

    The old implementation used BARSLAST across all available history. That
    can create false DXDX signals when the last zero crossing is older than
    100 bars, because cd.docx explicitly resets the lookback to zero.
    """
    recent_cross_exists = count(condition, 100) > 0
    return barslast(condition).where(recent_cross_exists, 0).fillna(0)


# ---------- MACD divergence system (the original 抄底/卖出 formula) ----------

def compute_macd_divergence(df: pd.DataFrame) -> pd.DataFrame:
    """Compute the full MACD divergence signal set from cd.docx.

    Input df must have columns ['open', 'high', 'low', 'close'].
    Adds DIF, DEA, MACD_bar, LLL, DXDX, DBL and DBJGXC.  ``CCC`` and
    ``JJJ`` are also exposed as diagnostic-only intermediate values; they do
    not participate in any additional filtering or alter the formula.
    """
    close = df["close"]

    # Core MACD
    D = ema(close, 12) - ema(close, 26)
    A = ema(D, 9)
    M = (D - A) * 2

    # Exact cd.docx zero-cross trackers: only the latest 100 bars count.
    down_cross = (ref(M, 1) >= 0) & (M < 0)
    up_cross = (ref(M, 1) <= 0) & (M > 0)
    N1 = _cd_zero_cross_lookback(down_cross)
    MM1 = _cd_zero_cross_lookback(up_cross)

    # ----- Bottom divergence (抄底) -----
    CC1 = llv_dyn(close, N1 + 1)
    CC2 = ref_dyn(CC1, (MM1 + 1).clip(lower=1))
    CC3 = ref_dyn(CC2, (MM1 + 1).clip(lower=1))
    DIFL1 = llv_dyn(D, N1 + 1)
    DIFL2 = ref_dyn(DIFL1, (MM1 + 1).clip(lower=1))
    DIFL3 = ref_dyn(DIFL2, (MM1 + 1).clip(lower=1))

    AAA = (CC1 < CC2) & (DIFL1 > DIFL2) & (ref(M, 1) < 0) & (D < 0)
    BBB = (CC1 < CC3) & (DIFL1 < DIFL2) & (DIFL1 > DIFL3) & (ref(M, 1) < 0) & (D < 0)
    CCC = (AAA | BBB) & (D < 0)
    LLL = (~_bfalse(ref(CCC, 1))) & _bfalse(CCC)

    # cd.docx:
    # JJJ  := REF(CCC,1) AND ABS(REF(DIFF,1)) >= ABS(DIFF) * 1.01
    # DXDX := REF(JJJ,1)=0 AND JJJ
    JJJ = _bfalse(ref(CCC, 1)) & (ref(D, 1).abs() >= D.abs() * 1.01)
    DXDX = (~_bfalse(ref(JJJ, 1))) & _bfalse(JJJ)

    # ----- Top divergence (卖出) -----
    CH1 = hhv_dyn(close, MM1 + 1)
    CH2 = ref_dyn(CH1, (N1 + 1).clip(lower=1))
    CH3 = ref_dyn(CH2, (N1 + 1).clip(lower=1))
    DIFH1 = hhv_dyn(D, MM1 + 1)
    DIFH2 = ref_dyn(DIFH1, (N1 + 1).clip(lower=1))
    DIFH3 = ref_dyn(DIFH2, (N1 + 1).clip(lower=1))

    ZJDBL = (CH1 > CH2) & (DIFH1 < DIFH2) & (ref(M, 1) > 0) & (D > 0)
    GXDBL = (CH1 > CH3) & (DIFH1 > DIFH2) & (DIFH1 < DIFH3) & (ref(M, 1) > 0) & (D > 0)
    DBBL = (ZJDBL | GXDBL) & (D > 0)
    DBL = (~_bfalse(ref(DBBL, 1))) & _bfalse(DBBL) & (D > A)

    DBJG = _bfalse(ref(DBBL, 1)) & (ref(D, 1) >= D * 1.01)
    DBJGXC = (~_bfalse(ref(DBJG, 1))) & _bfalse(DBJG)

    out = df.copy()
    out["DIF"] = D
    out["DEA"] = A
    out["MACD_bar"] = M
    # Bottom-side values are emitted verbatim for independent formula parity
    # and daily-source diagnostics.  They do not add any signal condition.
    out["N1"] = N1
    out["MM1"] = MM1
    out["CC1"] = CC1
    out["CC2"] = CC2
    out["CC3"] = CC3
    out["DIFL1"] = DIFL1
    out["DIFL2"] = DIFL2
    out["DIFL3"] = DIFL3
    out["AAA"] = _bfalse(AAA)
    out["BBB"] = _bfalse(BBB)
    out["LLL"] = _bfalse(LLL)
    # Keep the exact formula above, but surface its two bottom-side
    # intermediates for an auditable 4H candidate trace.
    out["CCC"] = _bfalse(CCC)
    out["JJJ"] = _bfalse(JJJ)
    out["DXDX"] = _bfalse(DXDX)
    out["DBL"] = _bfalse(DBL)
    out["DBJGXC"] = _bfalse(DBJGXC)
    return out


# ---------- Dual EMA channel (blue 23 / yellow 89) ----------

def compute_ema_channels(
    df: pd.DataFrame,
    fast_period: int = 23,
    slow_period: int = 89,
) -> pd.DataFrame:
    """Compute blue (fast=23) and yellow (slow=89) EMA channels."""
    out = df.copy()
    out["BLUE_UP"] = ema(df["high"], fast_period)
    out["BLUE_DW"] = ema(df["low"], fast_period)
    out["YELLOW_UP"] = ema(df["high"], slow_period)
    out["YELLOW_DW"] = ema(df["low"], slow_period)
    out["BLUE_ABOVE_YELLOW"] = (
        (out["BLUE_UP"] > out["YELLOW_UP"])
        & (out["BLUE_DW"] > out["YELLOW_DW"])
    )
    out["BLUE_FULLY_ABOVE_YELLOW"] = out["BLUE_DW"] > out["YELLOW_UP"]
    return out


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Pipeline: cd.docx MACD divergence + EMA channels."""
    return compute_ema_channels(compute_macd_divergence(df))
