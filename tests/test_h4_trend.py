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
    # 9 H1 bars starting at 00:00 -> two complete H4 bars (00:00, 04:00) and
    # one incomplete (08:00, only 1 of 4 bars) that must be dropped.
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


def _trending_h1(n_h4: int = 40, step: float = 0.0020, start_price: float = 1.1000) -> pd.DataFrame:
    """Steady uptrend: each H4 bar closes `step` above the previous one."""
    closes = []
    price = start_price
    for _ in range(n_h4):
        for k in range(4):
            closes.append(price + step * (k + 1) / 4)
        price += step
    return _h1_bars("2024-01-01 00:00", closes)


def test_long_breakout_fires_only_on_h4_boundary_m15_close():
    h1 = _trending_h1()
    last_h1_close_time = h1["time"].iloc[-1] + pd.Timedelta(hours=1)  # also an H4 boundary
    final_close = float(h1["close"].iloc[-1]) + 0.0030  # clear channel break

    ctx = compute_context("EURUSD", h1, _m15_ending_at(last_h1_close_time, 8, final_close), PARAMS)
    assert ctx.regime == "LONG"
    assert ctx.pullback_active is True

    on_boundary = evaluate("EURUSD", h1, _m15_ending_at(last_h1_close_time, 8, final_close), PARAMS)
    assert on_boundary is not None
    assert on_boundary.side == "LONG"
    assert on_boundary.sl < on_boundary.entry < on_boundary.tp
    # tp = disaster cap at 8R
    risk = on_boundary.entry - on_boundary.sl
    assert on_boundary.tp == pytest.approx(on_boundary.entry + 8 * risk)

    off_boundary = evaluate(
        "EURUSD", h1, _m15_ending_at(last_h1_close_time + pd.Timedelta(minutes=15), 8, final_close), PARAMS
    )
    assert off_boundary is None


def test_no_signal_without_regime():
    closes = [1.1000 + 0.0003 * ((i % 8) - 4) for i in range(160)]  # oscillating, flat
    h1 = _h1_bars("2024-01-01 00:00", closes)
    end = h1["time"].iloc[-1] + pd.Timedelta(hours=1)
    assert evaluate("EURUSD", h1, _m15_ending_at(end, 8, closes[-1]), PARAMS) is None


def test_update_stop_proposes_tighter_only():
    h1 = _trending_h1()
    m15 = _m15_ending_at(h1["time"].iloc[-1] + pd.Timedelta(hours=1), 8, float(h1["close"].iloc[-1]))
    entry_time = h1["time"].iloc[20]
    entry = float(h1["close"].iloc[20])

    wide_sl = entry - 0.0500  # far below: trail should tighten
    proposal = update_stop("EURUSD", "LONG", entry, entry_time, wide_sl, h1, m15, PARAMS)
    assert proposal is not None
    assert proposal > wide_sl

    # If current SL is already above the chandelier level, no proposal.
    tight_sl = float(h1["close"].iloc[-1])  # unrealistically tight
    assert update_stop("EURUSD", "LONG", entry, entry_time, tight_sl, h1, m15, PARAMS) is None


def test_update_stop_short_direction_mirrors():
    # Downtrend
    closes = list(np.linspace(1.20, 1.10, 160))
    h1 = _h1_bars("2024-01-01 00:00", closes)
    m15 = _m15_ending_at(h1["time"].iloc[-1] + pd.Timedelta(hours=1), 8, closes[-1])
    entry_time = h1["time"].iloc[20]
    entry = float(closes[20])

    wide_sl = entry + 0.0500
    proposal = update_stop("EURUSD", "SHORT", entry, entry_time, wide_sl, h1, m15, PARAMS)
    assert proposal is not None
    assert proposal < wide_sl
