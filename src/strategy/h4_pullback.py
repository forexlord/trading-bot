"""H4 trend pullback — enter on retests in a confirmed trend, not on breakouts.

Motivation (see h4_trend backtest diagnostics):
- Donchian breakouts on H4 produced ~48% WR but PF 0.91: losers had high MAE
  (70 pips) and low MFE (28 pips) — classic false-break/chop entries.
- Manual trend traders more often buy dips / sell rallies after extension.
- Same H4 timeframe keeps spread+slippage ~4-5% of R (vs ~19% on M15).

Rules:
- Regime: H4 close vs EMA(h4_trend_ema) + EMA slope (same as h4_trend).
- Setup: finite-state pullback on H4 — extend >= extension_atr * ATR away
  from EMA(h4_pullback_ema), then touch back through that EMA.
- Entry: only on the M15 close that completes an H4 bar; bullish/bearish H4
  close + RSI(14) momentum confirm (same logic as trend_pullback on M15).
- SL: min/max of swing low/high and ATR floor (tighter than breakout 2N).
- TP: fixed h4_pullback_tp_r * R — take profit at a realistic target; no
  wide 8R disaster cap + chandelier (that profile fit breakouts, not pullbacks).

Pure function of data: no MT5 imports.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.indicators.ta import atr_wilder, ema, rsi_wilder, swing_high, swing_low
from src.strategy.common import Context, Signal, pip_size
from src.strategy.h4_trend import DECIDES_ON_HTF_CLOSE, HTF, _is_h4_decision_point, resample_h4

__all__ = [
    "Context",
    "Signal",
    "pip_size",
    "compute_context",
    "evaluate",
    "evaluate_with_context",
    "HTF",
    "DECIDES_ON_HTF_CLOSE",
    "resample_h4",
]


def _f(params: Any, name: str, default: Any) -> Any:
    return getattr(params, name, default)


def _pullback_state(
    h4: pd.DataFrame,
    ema_fast: pd.Series,
    atr: pd.Series,
    direction: str,
    lookback: int,
    expiry: int,
    extension_atr: float,
) -> tuple[np.ndarray, np.ndarray]:
    """NONE -> EXTENDED (stretched from fast EMA) -> ACTIVE (touched back)."""
    n = len(h4)
    active = np.zeros(n, dtype=bool)
    age = np.full(n, -1, dtype=int)

    highs = h4["high"].to_numpy()
    lows = h4["low"].to_numpy()
    closes = h4["close"].to_numpy()
    ema_vals = ema_fast.to_numpy()
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

        ext = extension_atr * atr_vals[i]
        if direction == "LONG":
            is_extended = highs[i] >= ema_vals[i] + ext
            is_touch = lows[i] <= ema_vals[i] or closes[i] < ema_vals[i]
        else:
            is_extended = lows[i] <= ema_vals[i] - ext
            is_touch = highs[i] >= ema_vals[i] or closes[i] > ema_vals[i]

        if state in ("NONE", "EXTENDED") and is_extended:
            state, extension_idx = "EXTENDED", i

        if state == "EXTENDED" and is_touch:
            state, active_since = "ACTIVE", i

        if state == "ACTIVE" and active_since is not None:
            active[i] = True
            age[i] = i - active_since

    return active, age


def _regime(h4: pd.DataFrame, params: Any) -> tuple[str, float, float, float]:
    trend_n = int(_f(params, "h4_trend_ema", 50))
    slope_lb = int(_f(params, "h4_slope_lookback", 3))

    n = len(h4)
    i = n - 1
    j = i - slope_lb
    close_now = float(h4["close"].iloc[i]) if n else float("nan")
    if n == 0 or j < 0:
        return "NONE", close_now, float("nan"), float("nan")

    ema_trend = ema(h4["close"], trend_n)
    if np.isnan(ema_trend.iloc[i]) or np.isnan(ema_trend.iloc[j]):
        return "NONE", close_now, float("nan"), float("nan")

    ema_now = float(ema_trend.iloc[i])
    slope = ema_now - float(ema_trend.iloc[j])

    if close_now > ema_now and slope > 0:
        return "LONG", close_now, ema_now, slope
    if close_now < ema_now and slope < 0:
        return "SHORT", close_now, ema_now, slope
    return "NONE", close_now, ema_now, slope


def _analyze(symbol: str, h4_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any):
    h4 = h4_df.reset_index(drop=True)
    regime, h4_close, h4_ema, slope = _regime(h4, params)

    fast_n = int(_f(params, "h4_pullback_ema", 20))
    atr_period = int(_f(params, "h4_atr_period", 14))
    ema_fast = ema(h4["close"], fast_n)
    atr_h4 = atr_wilder(h4, atr_period)
    rsi_h4 = rsi_wilder(h4["close"], int(_f(params, "rsi_period", 14)))
    i = len(h4) - 1
    pip = pip_size(symbol)

    pb_lookback = int(_f(params, "h4_pullback_lookback", 12))
    pb_expiry = int(_f(params, "h4_pullback_expiry", 4))
    ext_atr = float(_f(params, "h4_pullback_extension_atr", 1.0))

    pullback_active = False
    pullback_age: int | None = None
    if regime in ("LONG", "SHORT") and i >= 0:
        active, age = _pullback_state(
            h4, ema_fast, atr_h4, regime, pb_lookback, pb_expiry, ext_atr
        )
        pullback_active = bool(active[i])
        pullback_age = int(age[i]) if pullback_active else None

    atr_now = float(atr_h4.iloc[i]) if i >= 0 and not np.isnan(atr_h4.iloc[i]) else float("nan")
    m15_close = float(m15_df["close"].iloc[-1]) if len(m15_df) else float("nan")
    ema_fast_now = float(ema_fast.iloc[i]) if i >= 0 and not np.isnan(ema_fast.iloc[i]) else float("nan")
    rsi_now = float(rsi_h4.iloc[i]) if i >= 0 and not np.isnan(rsi_h4.iloc[i]) else float("nan")

    ctx = Context(
        regime=regime,
        h1_close=h4_close,
        h1_ema50=h4_ema,
        ema50_slope=slope,
        m15_close=m15_close,
        m15_ema20=ema_fast_now,
        rsi=rsi_now,
        atr_pips=atr_now / pip if not np.isnan(atr_now) else float("nan"),
        pullback_active=pullback_active,
        pullback_age=pullback_age,
    )
    return ctx, h4, ema_fast, atr_h4, rsi_h4, i


def compute_context(symbol: str, h4_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any) -> Context:
    ctx, *_ = _analyze(symbol, h4_df, m15_df, params)
    return ctx


def evaluate_with_context(
    symbol: str, h4_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any
) -> tuple[Context, Signal | None]:
    ctx, h4, ema_fast, atr_h4, rsi_h4, i = _analyze(symbol, h4_df, m15_df, params)
    return ctx, _signal_from(symbol, ctx, h4, ema_fast, atr_h4, rsi_h4, i, m15_df, params)


def evaluate(symbol: str, h4_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any) -> Signal | None:
    ctx, h4, ema_fast, atr_h4, rsi_h4, i = _analyze(symbol, h4_df, m15_df, params)
    return _signal_from(symbol, ctx, h4, ema_fast, atr_h4, rsi_h4, i, m15_df, params)


def _signal_from(
    symbol: str,
    ctx: Context,
    h4: pd.DataFrame,
    ema_fast: pd.Series,
    atr_h4: pd.Series,
    rsi_h4: pd.Series,
    i: int,
    m15_df: pd.DataFrame,
    params: Any,
) -> Signal | None:
    if ctx.regime == "NONE" or not ctx.pullback_active:
        return None
    if i < 2:
        return None
    if not _is_h4_decision_point(m15_df, h4):
        return None

    close = h4["close"].to_numpy()
    open_ = h4["open"].to_numpy()
    rsi_vals = rsi_h4.to_numpy()
    ema_vals = ema_fast.to_numpy()
    atr_price = float(atr_h4.iloc[i])

    if any(np.isnan(v) for v in (rsi_vals[i], rsi_vals[i - 1], rsi_vals[i - 2], ema_vals[i], atr_price)):
        return None
    if atr_price <= 0:
        return None

    pip = pip_size(symbol)
    spread_buffer = float(_f(params, "spread_buffer_pips", 1.0)) * pip
    entry = float(m15_df["close"].iloc[-1])
    sl_mult = float(_f(params, "h4_atr_sl_mult", 1.3))
    tp_r = float(_f(params, "h4_pullback_tp_r", 2.0))
    swing_lb = int(_f(params, "h4_swing_lookback", 8))

    if ctx.regime == "LONG":
        crossed_up = (rsi_vals[i] > 50 and rsi_vals[i - 1] <= 50) or (
            rsi_vals[i - 1] > 50 and rsi_vals[i - 2] <= 50
        )
        if not (close[i] > open_[i] and close[i] > ema_vals[i] and crossed_up):
            return None

        swl = swing_low(h4, swing_lb).iloc[i]
        if np.isnan(swl):
            return None
        sl = min(swl - spread_buffer, entry - sl_mult * atr_price)
        if sl >= entry:
            return None
        risk = entry - sl
        return Signal(
            symbol=symbol,
            side="LONG",
            entry=entry,
            sl=sl,
            tp=entry + tp_r * risk,
            sl_pips=risk / pip,
            context=ctx,
        )

    crossed_down = (rsi_vals[i] < 50 and rsi_vals[i - 1] >= 50) or (
        rsi_vals[i - 1] < 50 and rsi_vals[i - 2] >= 50
    )
    if not (close[i] < open_[i] and close[i] < ema_vals[i] and crossed_down):
        return None

    swh = swing_high(h4, swing_lb).iloc[i]
    if np.isnan(swh):
        return None
    sl = max(swh + spread_buffer, entry + sl_mult * atr_price)
    if sl <= entry:
        return None
    risk = sl - entry
    return Signal(
        symbol=symbol,
        side="SHORT",
        entry=entry,
        sl=sl,
        tp=entry - tp_r * risk,
        sl_pips=risk / pip,
        context=ctx,
    )
