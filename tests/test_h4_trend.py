from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.strategy.h4_trend import compute_context, evaluate, resample_h4, update_stop

PARAMS = SimpleNamespace(
    h4_breakout_lookback=5,
    h4_trend_ema=10,
    h4_slope_lookback=2,
    h4_atr_period=5,
    h4_atr_sl_mult=2.0,
    h4_trail_atr_mult=3.0,
    h4_tp_r_cap=8.0,
)


def _h1_bars(start: str, closes: list[float], spread_range: float = 0.0005) -> pd.DataFrame:
    times = pd.date_range(start, periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": [c - 0.0001 for c in closes],
            "high": [c + spread_range for c in closes],
            "low": [c - spread_range for c in closes],
            "close": closes,
        }
    )


def _h4_bars(start: str, closes: list[float], spread_range: float = 0.0008) -> pd.DataFrame:
    times = pd.date_range(start, periods=len(closes), freq="4h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": [c - 0.0002 for c in closes],
            "high": [c + spread_range for c in closes],
            "low": [c - spread_range for c in closes],
            "close": closes,
        }
    )


def _m15_ending_at(end_exclusive: pd.Timestamp, n: int, close: float) -> pd.DataFrame:
    # Last bar is stamped end_exclusive - 15min, i.e. it CLOSES exactly at end_exclusive.
    times = pd.date_range(end=end_exclusive - pd.Timedelta(minutes=15), periods=n, freq="15min")
    return pd.DataFrame(
        {
            "time": times,
            "open": [close] * n,
            "high": [close + 0.0002] * n,
            "low": [close - 0.0002] * n,
            "close": [close] * n,
        }
    )


def test_resample_h4_aggregates_and_drops_incomplete_groups():
    # Used only by run_backtest's one-time fallback for stores without native
    # H4 history. 9 H1 bars -> two complete H4 bars, one incomplete (dropped).
    closes = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8]
    h1 = _h1_bars("2024-01-01 00:00", closes)
    h4 = resample_h4(h1)

    assert len(h4) == 2
    assert h4["time"].iloc[0] == pd.Timestamp("2024-01-01 00:00", tz="UTC")
    assert h4["open"].iloc[0] == pytest.approx(1.0 - 0.0001)
    assert h4["close"].iloc[0] == pytest.approx(1.3)  # last H1 close in group
    assert h4["high"].iloc[0] == pytest.approx(1.3 + 0.0005)
    assert h4["low"].iloc[0] == pytest.approx(1.0 - 0.0005)
    assert h4["close"].iloc[1] == pytest.approx(1.7)


def _trending_h4(n: int = 40, step: float = 0.0020, start_price: float = 1.1000) -> pd.DataFrame:
    closes = [start_price + step * (i + 1) for i in range(n)]
    return _h4_bars("2024-01-01 00:00", closes)


def test_long_breakout_fires_only_on_h4_boundary_m15_close():
    h4 = _trending_h4()
    h4_close_time = h4["time"].iloc[-1] + pd.Timedelta(hours=4)
    final_close = float(h4["close"].iloc[-1]) + 0.0030  # clear channel break

    ctx = compute_context("EURUSD", h4, _m15_ending_at(h4_close_time, 8, final_close), PARAMS)
    assert ctx.regime == "LONG"
    assert ctx.pullback_active is True

    on_boundary = evaluate("EURUSD", h4, _m15_ending_at(h4_close_time, 8, final_close), PARAMS)
    assert on_boundary is not None
    assert on_boundary.side == "LONG"
    assert on_boundary.sl < on_boundary.entry < on_boundary.tp
    # tp = disaster cap at 8R
    risk = on_boundary.entry - on_boundary.sl
    assert on_boundary.tp == pytest.approx(on_boundary.entry + 8 * risk)

    off_boundary = evaluate(
        "EURUSD", h4, _m15_ending_at(h4_close_time + pd.Timedelta(minutes=15), 8, final_close), PARAMS
    )
    assert off_boundary is None


def test_slope_filter_blocks_weak_trend():
    h4 = _trending_h4()
    h4_close_time = h4["time"].iloc[-1] + pd.Timedelta(hours=4)
    final_close = float(h4["close"].iloc[-1]) + 0.0030
    params = SimpleNamespace(**vars(PARAMS), h4_min_slope_atr_frac=999.0, h4_min_atr_percentile=0.0)
    m15 = _m15_ending_at(h4_close_time, 8, final_close)
    assert evaluate("EURUSD", h4, m15, PARAMS) is not None
    assert evaluate("EURUSD", h4, m15, params) is None


def test_atr_percentile_filter_blocks_low_vol():
    # Flat range -> low ATR percentile vs history with a volatility spike earlier.
    flat = [1.1000] * 30
    spike = [1.1000 + (0.05 if i % 2 == 0 else -0.05) for i in range(30)]
    closes = spike + flat
    h4 = _h4_bars("2024-01-01 00:00", closes, spread_range=0.0002)
    h4_close_time = h4["time"].iloc[-1] + pd.Timedelta(hours=4)
    # Force a marginal breakout on the last bar
    last_close = float(h4["close"].iloc[-1]) + 0.0010
    params = SimpleNamespace(
        **vars(PARAMS),
        h4_min_slope_atr_frac=0.0,
        h4_min_atr_percentile=50.0,
        h4_atr_percentile_lookback=60,
    )
    m15 = _m15_ending_at(h4_close_time, 8, last_close)
    # With high percentile threshold, flat-tail ATR should be rejected even if regime fires.
    sig = evaluate("EURUSD", h4, m15, params)
    assert sig is None


def test_no_signal_without_regime():
    closes = [1.1000 + 0.0003 * ((i % 8) - 4) for i in range(40)]  # oscillating, flat
    h4 = _h4_bars("2024-01-01 00:00", closes)
    end = h4["time"].iloc[-1] + pd.Timedelta(hours=4)
    assert evaluate("EURUSD", h4, _m15_ending_at(end, 8, closes[-1]), PARAMS) is None


def test_update_stop_proposes_tighter_only():
    h4 = _trending_h4()
    m15 = _m15_ending_at(h4["time"].iloc[-1] + pd.Timedelta(hours=4), 8, float(h4["close"].iloc[-1]))
    entry_time = h4["time"].iloc[5]
    entry = float(h4["close"].iloc[5])

    wide_sl = entry - 0.0500  # far below: trail should tighten
    proposal = update_stop("EURUSD", "LONG", entry, entry_time, wide_sl, h4, m15, PARAMS)
    assert proposal is not None
    assert proposal > wide_sl

    # If current SL is already above the chandelier level, no proposal.
    tight_sl = float(h4["close"].iloc[-1])  # unrealistically tight
    assert update_stop("EURUSD", "LONG", entry, entry_time, tight_sl, h4, m15, PARAMS) is None


def test_update_stop_breakeven_locks_after_move():
    # Flat market, then ONE spike bar >= 2 ATR in favor: the chandelier is
    # still below entry, so the breakeven rule must win and propose
    # entry + spread buffer.
    params = SimpleNamespace(**vars(PARAMS), h4_breakeven_after_atr=2.0, spread_buffer_pips=1.0)

    flat = [1.1000] * 20
    times = pd.date_range("2024-01-01 00:00", periods=21, freq="4h", tz="UTC")
    rows = [
        {"time": times[i], "open": 1.1000, "high": 1.1010, "low": 1.0990, "close": flat[i]}
        for i in range(20)
    ]
    rows.append({"time": times[20], "open": 1.1000, "high": 1.1065, "low": 1.0999, "close": 1.1060})
    h4 = pd.DataFrame(rows)
    m15 = _m15_ending_at(times[20] + pd.Timedelta(hours=4), 8, 1.1060)

    entry_time = times[19]
    entry = 1.1000

    from src.indicators.ta import atr_wilder

    atr_now = float(atr_wilder(h4, params.h4_atr_period).iloc[-1])
    anchor = 1.1060
    assert anchor - entry >= 2.0 * atr_now          # breakeven trigger reached
    assert anchor - 3.0 * atr_now < entry           # chandelier alone still below entry

    wide_sl = entry - 0.0500
    proposal = update_stop("EURUSD", "LONG", entry, entry_time, wide_sl, h4, m15, params)
    assert proposal is not None
    assert proposal == pytest.approx(entry + 1.0 * 0.0001)  # entry + spread buffer


def test_update_stop_short_direction_mirrors():
    closes = list(np.linspace(1.20, 1.10, 40))  # downtrend
    h4 = _h4_bars("2024-01-01 00:00", closes)
    m15 = _m15_ending_at(h4["time"].iloc[-1] + pd.Timedelta(hours=4), 8, closes[-1])
    entry_time = h4["time"].iloc[5]
    entry = float(closes[5])

    wide_sl = entry + 0.0500
    proposal = update_stop("EURUSD", "SHORT", entry, entry_time, wide_sl, h4, m15, PARAMS)
    assert proposal is not None
    assert proposal < wide_sl
