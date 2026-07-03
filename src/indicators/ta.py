"""Technical indicators, implemented directly on pandas — no TA-lib dependency.

RSI and ATR use Wilder's original smoothing: seed with a simple average over
the first `period` values, then recursively smooth as
`avg = (avg_prev * (period - 1) + value) / period`. This differs from
`Series.ewm(adjust=False)` (which seeds from the first raw observation) and
matches what MT5/MT4 and most charting platforms compute.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, seeded with the SMA of the first `period` values."""
    values = series.to_numpy(dtype=float)
    n = len(values)
    out = np.full(n, np.nan)
    if n < period:
        return pd.Series(out, index=series.index)

    alpha = 2.0 / (period + 1)
    out[period - 1] = values[:period].mean()
    for i in range(period, n):
        out[i] = values[i] * alpha + out[i - 1] * (1 - alpha)
    return pd.Series(out, index=series.index)


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi_wilder(series: pd.Series, period: int) -> pd.Series:
    """RSI(period) using Wilder smoothing on gains/losses."""
    values = series.to_numpy(dtype=float)
    n = len(values)
    out = np.full(n, np.nan)
    if n <= period:
        return pd.Series(out, index=series.index)

    deltas = np.diff(values)
    gains = np.clip(deltas, a_min=0, a_max=None)
    losses = np.clip(-deltas, a_min=0, a_max=None)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from_avgs(avg_gain, avg_loss)

    return pd.Series(out, index=series.index)


def true_range(df: pd.DataFrame) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr_wilder(df: pd.DataFrame, period: int) -> pd.Series:
    """ATR(period) using Wilder smoothing of the true range."""
    tr = true_range(df).to_numpy(dtype=float)
    n = len(tr)
    out = np.full(n, np.nan)
    if n < period:
        return pd.Series(out, index=df.index)

    out[period - 1] = tr[:period].mean()
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return pd.Series(out, index=df.index)


def swing_high(df: pd.DataFrame, lookback: int) -> pd.Series:
    return df["high"].rolling(window=lookback, min_periods=lookback).max()


def swing_low(df: pd.DataFrame, lookback: int) -> pd.Series:
    return df["low"].rolling(window=lookback, min_periods=lookback).min()
