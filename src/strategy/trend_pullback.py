"""Trend-following pullback strategy (v1).

Pure function of data: (symbol, h1_df, m15_df, params) -> Signal | None.
No MT5 imports here — this exact code path is shared by the backtester and
the live bot. All decisions use the last (just-closed) row of each dataframe;
callers must never pass a dataframe containing an in-progress candle.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.indicators.ta import atr_wilder, ema, rsi_wilder, swing_high, swing_low
from src.strategy.common import Context, Signal, pip_size

__all__ = ["Context", "Signal", "pip_size", "compute_context", "evaluate"]



def _regime(h1_df: pd.DataFrame, params: Any) -> tuple[str, float, float, float]:
    h1 = h1_df.reset_index(drop=True)
    ema50 = ema(h1["close"], params.trend_ema)
    n = len(h1)
    idx_now = n - 1
    idx_prev = idx_now - params.h1_slope_lookback

    h1_close = float(h1["close"].iloc[idx_now])
    if idx_prev < 0 or np.isnan(ema50.iloc[idx_now]) or np.isnan(ema50.iloc[idx_prev]):
        return "NONE", h1_close, float("nan"), float("nan")

    ema_now = float(ema50.iloc[idx_now])
    ema_prev = float(ema50.iloc[idx_prev])
    slope = ema_now - ema_prev

    if h1_close > ema_now and slope > 0:
        regime = "LONG"
    elif h1_close < ema_now and slope < 0:
        regime = "SHORT"
    else:
        regime = "NONE"
    return regime, h1_close, ema_now, slope


def _pullback_state(
    m15: pd.DataFrame,
    ema20: pd.Series,
    atr: pd.Series,
    direction: str,
    lookback: int,
    expiry: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Finite-state scan: NONE -> EXTENDED (price stretched away from EMA20) ->
    ACTIVE (price has touched/closed back through EMA20). ACTIVE expires after
    `expiry` candles; EXTENDED expires if no touch-back within `lookback` candles.
    """
    n = len(m15)
    active = np.zeros(n, dtype=bool)
    age = np.full(n, -1, dtype=int)

    highs = m15["high"].to_numpy()
    lows = m15["low"].to_numpy()
    closes = m15["close"].to_numpy()
    ema_vals = ema20.to_numpy()
    atr_vals = atr.to_numpy()

    state = "NONE"
    extension_idx: int | None = None
    active_since: int | None = None

    for i in range(n):
        if np.isnan(ema_vals[i]) or np.isnan(atr_vals[i]):
            continue

        if state == "ACTIVE" and active_since is not None and (i - active_since) >= expiry:
            state, active_since = "NONE", None

        if state == "EXTENDED" and extension_idx is not None and (i - extension_idx) > lookback:
            state, extension_idx = "NONE", None

        if direction == "LONG":
            is_extended = highs[i] >= ema_vals[i] + atr_vals[i]
            is_touch = lows[i] <= ema_vals[i] or closes[i] < ema_vals[i]
        else:
            is_extended = lows[i] <= ema_vals[i] - atr_vals[i]
            is_touch = highs[i] >= ema_vals[i] or closes[i] > ema_vals[i]

        if state in ("NONE", "EXTENDED") and is_extended:
            state, extension_idx = "EXTENDED", i

        if state == "EXTENDED" and is_touch:
            state, active_since = "ACTIVE", i

        if state == "ACTIVE" and active_since is not None:
            active[i] = True
            age[i] = i - active_since

    return active, age


def _analyze(
    symbol: str, h1_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any
) -> tuple[Context, pd.DataFrame, pd.Series, pd.Series, pd.Series, int]:
    regime, h1_close, h1_ema50, slope = _regime(h1_df, params)

    m15 = m15_df.reset_index(drop=True)
    ema20 = ema(m15["close"], params.pullback_ema)
    atr14 = atr_wilder(m15, params.atr_period)
    rsi14 = rsi_wilder(m15["close"], params.rsi_period)
    i = len(m15) - 1
    pip = pip_size(symbol)

    pullback_active = False
    pullback_age = None
    if regime in ("LONG", "SHORT") and i >= 0:
        active, age = _pullback_state(
            m15, ema20, atr14, regime, params.pullback_lookback, params.pullback_expiry
        )
        pullback_active = bool(active[i])
        pullback_age = int(age[i]) if pullback_active else None

    def _val(series: pd.Series) -> float:
        v = series.iloc[i]
        return float(v) if not np.isnan(v) else float("nan")

    ctx = Context(
        regime=regime,
        h1_close=h1_close,
        h1_ema50=h1_ema50,
        ema50_slope=slope,
        m15_close=float(m15["close"].iloc[i]),
        m15_ema20=_val(ema20),
        rsi=_val(rsi14),
        atr_pips=_val(atr14) / pip if not np.isnan(_val(atr14)) else float("nan"),
        pullback_active=pullback_active,
        pullback_age=pullback_age,
    )
    return ctx, m15, ema20, atr14, rsi14, i


def compute_context(symbol: str, h1_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any) -> Context:
    ctx, *_ = _analyze(symbol, h1_df, m15_df, params)
    return ctx


def evaluate(symbol: str, h1_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any) -> Signal | None:
    ctx, m15, ema20, atr14, rsi14, i = _analyze(symbol, h1_df, m15_df, params)

    if ctx.regime == "NONE" or not ctx.pullback_active:
        return None
    if i < 2 or any(np.isnan(v) for v in (ctx.m15_ema20, ctx.rsi, ctx.atr_pips)):
        return None

    close = m15["close"].to_numpy()
    open_ = m15["open"].to_numpy()
    rsi_vals = rsi14.to_numpy()
    ema_vals = ema20.to_numpy()

    if np.isnan(rsi_vals[i - 2]):
        return None

    pip = pip_size(symbol)
    spread_buffer = params.spread_buffer_pips * pip
    entry = float(close[i])
    atr_price = float(atr14.iloc[i])

    if ctx.regime == "LONG":
        crossed_up = (rsi_vals[i] > 50 and rsi_vals[i - 1] <= 50) or (
            rsi_vals[i - 1] > 50 and rsi_vals[i - 2] <= 50
        )
        if not (close[i] > open_[i] and close[i] > ema_vals[i] and crossed_up):
            return None

        swl = swing_low(m15, params.swing_lookback).iloc[i]
        if np.isnan(swl):
            return None
        sl = min(swl - spread_buffer, entry - params.atr_sl_mult * atr_price)
        if sl >= entry:
            return None
        risk = entry - sl
        return Signal(
            symbol=symbol,
            side="LONG",
            entry=entry,
            sl=sl,
            tp=entry + params.tp_r_multiple * risk,
            sl_pips=risk / pip,
            context=ctx,
        )

    crossed_down = (rsi_vals[i] < 50 and rsi_vals[i - 1] >= 50) or (
        rsi_vals[i - 1] < 50 and rsi_vals[i - 2] >= 50
    )
    if not (close[i] < open_[i] and close[i] < ema_vals[i] and crossed_down):
        return None

    swh = swing_high(m15, params.swing_lookback).iloc[i]
    if np.isnan(swh):
        return None
    sl = max(swh + spread_buffer, entry + params.atr_sl_mult * atr_price)
    if sl <= entry:
        return None
    risk = sl - entry
    return Signal(
        symbol=symbol,
        side="SHORT",
        entry=entry,
        sl=sl,
        tp=entry - params.tp_r_multiple * risk,
        sl_pips=risk / pip,
        context=ctx,
    )
