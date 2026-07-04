"""H1-aligned Donchian breakout strategy (v2).

Addresses weaknesses seen in trend_pullback on Exness cent data:
- Weak H1 filter (close vs EMA50 only) → require EMA stack + minimum separation
- Late/noisy pullback entries → enter only on M15 channel breakout with impulse candle
- Hard 2R with wide stops → default 1.5R and ATR-based stop

Pure function of data: (symbol, h1_df, m15_df, params) -> Signal | None.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.indicators.ta import atr_wilder, ema, rsi_wilder
from src.strategy.common import Context, Signal, pip_size

__all__ = ["Context", "Signal", "pip_size", "compute_context", "evaluate"]


def _f(params: Any, name: str, default: Any) -> Any:
    return getattr(params, name, default)


def _regime(h1_df: pd.DataFrame, params: Any) -> tuple[str, float, float, float, float]:
    """Return (regime, h1_close, slow_ema, fast_minus_slow, atr_pips_proxy)."""
    h1 = h1_df.reset_index(drop=True)
    fast_n = int(_f(params, "pullback_ema", 20))
    slow_n = int(_f(params, "trend_ema", 50))
    slope_lb = int(_f(params, "h1_slope_lookback", 5))
    min_sep = float(_f(params, "min_trend_atr_frac", 0.15))

    ema_fast = ema(h1["close"], fast_n)
    ema_slow = ema(h1["close"], slow_n)
    atr = atr_wilder(h1, int(_f(params, "atr_period", 14)))
    n = len(h1)
    i = n - 1
    j = i - slope_lb
    h1_close = float(h1["close"].iloc[i])

    if j < 0 or any(np.isnan(x) for x in (ema_fast.iloc[i], ema_slow.iloc[i], atr.iloc[i], ema_slow.iloc[j])):
        return "NONE", h1_close, float("nan"), float("nan"), float("nan")

    fast_now = float(ema_fast.iloc[i])
    slow_now = float(ema_slow.iloc[i])
    slow_prev = float(ema_slow.iloc[j])
    atr_now = float(atr.iloc[i])
    sep = fast_now - slow_now
    slope = slow_now - slow_prev

    if atr_now <= 0:
        return "NONE", h1_close, slow_now, sep, float("nan")

    sep_ok = abs(sep) >= min_sep * atr_now
    if h1_close > slow_now and fast_now > slow_now and slope > 0 and sep_ok:
        return "LONG", h1_close, slow_now, sep, atr_now
    if h1_close < slow_now and fast_now < slow_now and slope < 0 and sep_ok:
        return "SHORT", h1_close, slow_now, sep, atr_now
    return "NONE", h1_close, slow_now, sep, atr_now


def _analyze(symbol: str, h1_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any):
    regime, h1_close, h1_ema50, slope, _ = _regime(h1_df, params)
    m15 = m15_df.reset_index(drop=True)
    lookback = int(_f(params, "breakout_lookback", 20))
    ema20 = ema(m15["close"], int(_f(params, "pullback_ema", 20)))
    atr14 = atr_wilder(m15, int(_f(params, "atr_period", 14)))
    rsi14 = rsi_wilder(m15["close"], int(_f(params, "rsi_period", 14)))
    i = len(m15) - 1
    pip = pip_size(symbol)

    setup_active = False
    setup_age: int | None = None
    if regime in ("LONG", "SHORT") and i >= lookback:
        prior_high = float(m15["high"].iloc[i - lookback : i].max())
        prior_low = float(m15["low"].iloc[i - lookback : i].min())
        close = float(m15["close"].iloc[i])
        if regime == "LONG" and close > prior_high:
            setup_active, setup_age = True, 0
        elif regime == "SHORT" and close < prior_low:
            setup_active, setup_age = True, 0

    def _val(series: pd.Series) -> float:
        v = series.iloc[i]
        return float(v) if not np.isnan(v) else float("nan")

    atr_v = _val(atr14)
    ctx = Context(
        regime=regime,
        h1_close=h1_close,
        h1_ema50=h1_ema50,
        ema50_slope=slope,
        m15_close=float(m15["close"].iloc[i]),
        m15_ema20=_val(ema20),
        rsi=_val(rsi14),
        atr_pips=atr_v / pip if not np.isnan(atr_v) else float("nan"),
        pullback_active=setup_active,
        pullback_age=setup_age,
    )
    return ctx, m15, atr14, rsi14, i


def compute_context(symbol: str, h1_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any) -> Context:
    ctx, *_ = _analyze(symbol, h1_df, m15_df, params)
    return ctx


def evaluate(symbol: str, h1_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any) -> Signal | None:
    ctx, m15, atr14, rsi14, i = _analyze(symbol, h1_df, m15_df, params)
    lookback = int(_f(params, "breakout_lookback", 20))
    require_impulse = bool(_f(params, "require_impulse_candle", True))
    rsi_long_max = float(_f(params, "breakout_rsi_long_max", 70.0))
    rsi_short_min = float(_f(params, "breakout_rsi_short_min", 30.0))

    if ctx.regime == "NONE" or not ctx.pullback_active:
        return None
    if i < lookback or any(np.isnan(v) for v in (ctx.rsi, ctx.atr_pips)):
        return None

    close = float(m15["close"].iloc[i])
    open_ = float(m15["open"].iloc[i])
    low = float(m15["low"].iloc[i])
    high = float(m15["high"].iloc[i])
    atr_price = float(atr14.iloc[i])
    if np.isnan(atr_price) or atr_price <= 0:
        return None

    pip = pip_size(symbol)
    spread_buffer = float(_f(params, "spread_buffer_pips", 1.0)) * pip
    atr_sl_mult = float(_f(params, "atr_sl_mult", 1.2))
    tp_r = float(_f(params, "tp_r_multiple", 1.5))
    entry = close

    if ctx.regime == "LONG":
        if require_impulse and not (close > open_):
            return None
        if ctx.rsi > rsi_long_max:
            return None
        # Stop below breakout bar and ATR distance.
        sl = min(low - spread_buffer, entry - atr_sl_mult * atr_price)
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

    if require_impulse and not (close < open_):
        return None
    if ctx.rsi < rsi_short_min:
        return None
    sl = max(high + spread_buffer, entry + atr_sl_mult * atr_price)
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
