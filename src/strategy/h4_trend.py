"""H4 Donchian trend-following (v3) — research-backed replacement for the
M15 strategies.

Why this design (see README research notes / backtest diagnostics):
- The M15 strategies had zero gross edge and spread+slippage consumed ~19% of
  R per trade. H4 stops are ~50-70 pips, so the same 2-2.5 pip cost is ~4-5%
  of R — the cost problem is structural to the timeframe, not the pattern.
- Time-series momentum / trend-following is the best-documented systematic FX
  effect, and it lives at daily-ish horizons, not intraday. Donchian breakouts
  only work with a regime filter and a trailing exit that lets winners run
  (the M15 runs capped winners at 1.5R while MFE tails reached 110 pips).

Rules (Turtle-adapted):
- H4 candles are resampled from H1 inside the strategy (pure function).
- Regime: H4 close vs EMA(h4_trend_ema) + EMA slope over h4_slope_lookback bars.
- Entry: H4 close breaks the prior h4_breakout_lookback-bar Donchian channel,
  in the regime direction, evaluated ONLY on the M15 close that completes an
  H4 bar (6 decision points per day per pair).
- Initial SL: entry -/+ h4_atr_sl_mult * ATR_H4 (Turtle's 2N).
- TP: far disaster cap at h4_tp_r_cap * R — the trail is the real exit.
- Trail: chandelier — highest (lowest) H4 close since entry -/+
  h4_trail_atr_mult * ATR_H4, exposed via update_stop(); callers must only
  ever tighten (ratchet), never loosen.

Pure function of data: no MT5 imports.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from src.indicators.ta import atr_wilder, ema
from src.strategy.common import Context, Signal, pip_size

__all__ = ["Context", "Signal", "pip_size", "compute_context", "evaluate", "update_stop"]

H4 = pd.Timedelta(hours=4)
H1 = pd.Timedelta(hours=1)
M15 = pd.Timedelta(minutes=15)


def _f(params: Any, name: str, default: Any) -> Any:
    return getattr(params, name, default)


def resample_h4(h1_df: pd.DataFrame) -> pd.DataFrame:
    """H1 -> H4 OHLC. Only groups whose final H1 bar is present are kept
    (an H4 bar is complete when the H1 bar opening at group_start+3h has
    closed; callers pass only closed H1 bars).
    """
    if h1_df.empty:
        return h1_df.iloc[0:0].copy()
    h1 = h1_df.reset_index(drop=True)
    group_start = h1["time"].dt.floor("4h")
    agg = h1.groupby(group_start).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        last_h1=("time", "max"),
    )
    complete = agg[agg["last_h1"] == agg.index + pd.Timedelta(hours=3)]
    out = complete.reset_index().rename(columns={"time": "time"})
    return out[["time", "open", "high", "low", "close"]]


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


def _is_h4_decision_point(m15_df: pd.DataFrame, h4: pd.DataFrame) -> bool:
    """True only on the M15 close that completes the newest H4 bar."""
    if m15_df.empty or h4.empty:
        return False
    m15_end = m15_df["time"].iloc[-1] + M15
    h4_end = h4["time"].iloc[-1] + H4
    return bool(m15_end == h4_end)


def _analyze(symbol: str, h1_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any):
    h4 = resample_h4(h1_df)
    regime, h4_close, h4_ema, slope = _regime(h4, params)

    atr_period = int(_f(params, "h4_atr_period", 14))
    atr_h4 = atr_wilder(h4, atr_period) if len(h4) else pd.Series(dtype=float)
    i = len(h4) - 1
    pip = pip_size(symbol)

    lookback = int(_f(params, "h4_breakout_lookback", 20))
    setup_active = False
    setup_age: int | None = None
    if regime in ("LONG", "SHORT") and i >= lookback:
        prior_high = float(h4["high"].iloc[i - lookback : i].max())
        prior_low = float(h4["low"].iloc[i - lookback : i].min())
        close = float(h4["close"].iloc[i])
        if regime == "LONG" and close > prior_high:
            setup_active, setup_age = True, 0
        elif regime == "SHORT" and close < prior_low:
            setup_active, setup_age = True, 0

    atr_now = float(atr_h4.iloc[i]) if i >= 0 and not np.isnan(atr_h4.iloc[i]) else float("nan")
    m15_close = float(m15_df["close"].iloc[-1]) if len(m15_df) else float("nan")

    ctx = Context(
        regime=regime,
        h1_close=h4_close,       # H4 close (field names shared for log compat)
        h1_ema50=h4_ema,
        ema50_slope=slope,
        m15_close=m15_close,
        m15_ema20=float("nan"),  # not used by this strategy
        rsi=float("nan"),        # not used by this strategy
        atr_pips=atr_now / pip if not np.isnan(atr_now) else float("nan"),
        pullback_active=setup_active,
        pullback_age=setup_age,
    )
    return ctx, h4, atr_now


def compute_context(symbol: str, h1_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any) -> Context:
    ctx, *_ = _analyze(symbol, h1_df, m15_df, params)
    return ctx


def evaluate(symbol: str, h1_df: pd.DataFrame, m15_df: pd.DataFrame, params: Any) -> Signal | None:
    ctx, h4, atr_now = _analyze(symbol, h1_df, m15_df, params)

    if ctx.regime == "NONE" or not ctx.pullback_active:
        return None
    if np.isnan(atr_now) or atr_now <= 0:
        return None
    # Act only on the M15 close that completed this H4 bar — otherwise the
    # same breakout would re-fire on all 16 M15 closes inside the next H4 bar.
    if not _is_h4_decision_point(m15_df, h4):
        return None

    pip = pip_size(symbol)
    sl_mult = float(_f(params, "h4_atr_sl_mult", 2.0))
    tp_r_cap = float(_f(params, "h4_tp_r_cap", 8.0))
    entry = float(m15_df["close"].iloc[-1])

    if ctx.regime == "LONG":
        sl = entry - sl_mult * atr_now
        risk = entry - sl
        if risk <= 0:
            return None
        return Signal(
            symbol=symbol, side="LONG", entry=entry, sl=sl,
            tp=entry + tp_r_cap * risk, sl_pips=risk / pip, context=ctx,
        )

    sl = entry + sl_mult * atr_now
    risk = sl - entry
    if risk <= 0:
        return None
    return Signal(
        symbol=symbol, side="SHORT", entry=entry, sl=sl,
        tp=entry - tp_r_cap * risk, sl_pips=risk / pip, context=ctx,
    )


def update_stop(
    symbol: str,
    side: str,
    entry: float,
    entry_time: datetime | pd.Timestamp,
    current_sl: float,
    h1_df: pd.DataFrame,
    m15_df: pd.DataFrame,
    params: Any,
) -> float | None:
    """Chandelier trail: highest (lowest) H4 close since entry -/+ trail*ATR.

    Returns a PROPOSED stop or None. Callers must ratchet: apply only if it
    tightens (long: higher than current_sl; short: lower). The data window
    may not reach back to entry_time on long trades — that is fine, because
    the ratchet makes the effective trail the running max of proposals.
    """
    h4 = resample_h4(h1_df)
    if h4.empty:
        return None

    atr_period = int(_f(params, "h4_atr_period", 14))
    trail_mult = float(_f(params, "h4_trail_atr_mult", 3.0))
    atr_h4 = atr_wilder(h4, atr_period)
    atr_now = float(atr_h4.iloc[-1]) if not np.isnan(atr_h4.iloc[-1]) else float("nan")
    if np.isnan(atr_now) or atr_now <= 0:
        return None

    entry_ts = pd.Timestamp(entry_time)
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize("UTC")
    since = h4[h4["time"] >= entry_ts.floor("4h")]
    if since.empty:
        since = h4.tail(1)

    if side == "LONG":
        anchor = float(since["close"].max())
        proposal = anchor - trail_mult * atr_now
        return proposal if proposal > current_sl else None
    anchor = float(since["close"].min())
    proposal = anchor + trail_mult * atr_now
    return proposal if proposal < current_sl else None
