"""Indicator translations from Tongdaxin/Futubull formula language to Python.

Implements:
  - MACD divergence system (抄底 = DXDX, 卖出 = DBJGXC, 底背离 = LLL, 顶背离 = DBL)
  - Dual EMA channel: BLUE (fast, period 23), YELLOW (slow, period 89)
  - "Blue above yellow" filter (蓝梯 > 黄梯)

All formulas faithfully translate the original Tongdaxin script using:
  EMA / REF / LLV / HHV / COUNT / BARSLAST primitives.
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
    """BARSLAST(cond) - bars since condition was last True (0 if current True, NaN if never)."""
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


# ---------- Dynamic-lookback primitives (lookback length varies per bar) ----------
# Tongdaxin happily accepts LLV(X, N) where N is itself a series.
# Pandas rolling does not, so we implement these explicitly.

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


# ---------- MACD divergence system (the original 抄底/卖出 formula) ----------

def compute_macd_divergence(df: pd.DataFrame) -> pd.DataFrame:
    """Compute the full MACD divergence signal set.

    Input df: must have columns ['open', 'high', 'low', 'close'].
    Adds columns:
        DIF, DEA, MACD_bar  - standard MACD components
        LLL    - 底背离首现 (first occurrence of bottom divergence)
        DXDX   - 抄底首现   (structure-confirmed bottom)
        DBL    - 顶背离首现 (first occurrence of top divergence)
        DBJGXC - 卖出首现   (structure-confirmed top)
    """
    close = df["close"]

    # Core MACD
    D = ema(close, 12) - ema(close, 26)
    A = ema(D, 9)
    M = (D - A) * 2

    # Zero-cross trackers
    down_cross = (ref(M, 1) >= 0) & (M < 0)   # M crossed below 0
    up_cross   = (ref(M, 1) <= 0) & (M > 0)   # M crossed above 0
    N1  = barslast(down_cross)
    MM1 = barslast(up_cross)

    # ----- Bottom divergence (抄底) -----
    CC1 = llv_dyn(close, N1 + 1)
    CC2 = ref_dyn(CC1, MM1 + 1)
    CC3 = ref_dyn(CC2, MM1 + 1)
    DIFL1 = llv_dyn(D, N1 + 1)
    DIFL2 = ref_dyn(DIFL1, MM1 + 1)
    DIFL3 = ref_dyn(DIFL2, MM1 + 1)

    AAA = (CC1 < CC2) & (DIFL1 > DIFL2) & (ref(M, 1) < 0) & (D < 0)            # 普通底背离
    BBB = (CC1 < CC3) & (DIFL1 < DIFL2) & (DIFL1 > DIFL3) & (ref(M, 1) < 0) & (D < 0)  # 隐藏底背离
    CCC = (AAA | BBB) & (D < 0)
    LLL = (~_bfalse(ref(CCC, 1))) & _bfalse(CCC)   # 底背离首现

    # Structure of bottom: previous bar had CCC AND |D| is shrinking >= 1%
    JJJ  = _bfalse(ref(CCC, 1)) & (ref(D, 1).abs() >= D.abs() * 1.01)
    DXDX = (~_bfalse(ref(JJJ, 1))) & _bfalse(JJJ)  # 抄底首现

    # ----- Top divergence (卖出) -----
    CH1 = hhv_dyn(close, MM1 + 1)
    CH2 = ref_dyn(CH1, N1 + 1)
    CH3 = ref_dyn(CH2, N1 + 1)
    DIFH1 = hhv_dyn(D, MM1 + 1)
    DIFH2 = ref_dyn(DIFH1, N1 + 1)
    DIFH3 = ref_dyn(DIFH2, N1 + 1)

    ZJDBL = (CH1 > CH2) & (DIFH1 < DIFH2) & (ref(M, 1) > 0) & (D > 0)            # 普通顶背离
    GXDBL = (CH1 > CH3) & (DIFH1 > DIFH2) & (DIFH1 < DIFH3) & (ref(M, 1) > 0) & (D > 0)  # 隐藏顶背离
    DBBL  = (ZJDBL | GXDBL) & (D > 0)
    DBL   = (~_bfalse(ref(DBBL, 1))) & _bfalse(DBBL) & (D > A)   # 顶背离首现

    DBJG   = _bfalse(ref(DBBL, 1)) & (ref(D, 1) >= D * 1.01)
    DBJGXC = (~_bfalse(ref(DBJG, 1))) & _bfalse(DBJG)   # 卖出首现

    out = df.copy()
    out["DIF"] = D
    out["DEA"] = A
    out["MACD_bar"] = M
    out["LLL"]    = _bfalse(LLL)
    out["DXDX"]   = _bfalse(DXDX)
    out["DBL"]    = _bfalse(DBL)
    out["DBJGXC"] = _bfalse(DBJGXC)
    return out


# ---------- Dual EMA channel (blue 23 / yellow 89) ----------

def compute_ema_channels(
    df: pd.DataFrame,
    fast_period: int = 23,
    slow_period: int = 89,
) -> pd.DataFrame:
    """Compute blue (fast=23) and yellow (slow=89) EMA channels of HIGH/LOW.

    Adds columns:
        BLUE_UP, BLUE_DW         (蓝梯 upper/lower)
        YELLOW_UP, YELLOW_DW     (黄梯 upper/lower)
        BLUE_ABOVE_YELLOW        (loose: both bounds of blue above corresponding yellow)
        BLUE_FULLY_ABOVE_YELLOW  (strict: blue's bottom above yellow's top)
    """
    out = df.copy()
    out["BLUE_UP"]   = ema(df["high"], fast_period)
    out["BLUE_DW"]   = ema(df["low"],  fast_period)
    out["YELLOW_UP"] = ema(df["high"], slow_period)
    out["YELLOW_DW"] = ema(df["low"],  slow_period)
    out["BLUE_ABOVE_YELLOW"]       = (out["BLUE_UP"] > out["YELLOW_UP"]) & (out["BLUE_DW"] > out["YELLOW_DW"])
    out["BLUE_FULLY_ABOVE_YELLOW"] = out["BLUE_DW"] > out["YELLOW_UP"]
    return out


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Pipeline: MACD divergence + EMA channels."""
    df = compute_macd_divergence(df)
    df = compute_ema_channels(df)
    return df
