from types import SimpleNamespace

import pandas as pd
import pytest

from src.strategy.trend_pullback import evaluate, compute_context

PARAMS = SimpleNamespace(
    trend_ema=10,
    pullback_ema=5,
    atr_period=5,
    rsi_period=5,
    pullback_lookback=8,
    pullback_expiry=6,
    swing_lookback=4,
    h1_slope_lookback=3,
    atr_sl_mult=1.5,
    tp_r_multiple=2.0,
    spread_buffer_pips=1.0,
)


def _uptrend_h1(n: int = 20) -> pd.DataFrame:
    closes = [1.1000 + 0.0010 * i for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.0002 for c in closes],
            "high": [c + 0.0005 for c in closes],
            "low": [c - 0.0005 for c in closes],
            "close": closes,
        }
    )


def _flat_h1(n: int = 20) -> pd.DataFrame:
    c = 1.1000
    return pd.DataFrame({"open": [c] * n, "high": [c] * n, "low": [c] * n, "close": [c] * n})


def _m15_pullback_sequence(down_bars: list[float]) -> pd.DataFrame:
    """Uptrend warm-up -> extension bar -> N down bars (pullback through EMA20,
    driving RSI below 50) -> one bullish reversal bar back above EMA20 with RSI
    crossing back above 50.
    """
    rows: list[dict] = []

    def bar(o: float, h: float, l: float, c: float) -> None:
        rows.append({"open": o, "high": h, "low": l, "close": c})

    price = 1.1000
    for _ in range(12):
        o, c = price, price + 0.0004
        bar(o, c + 0.0002, o - 0.00005, c)
        price = c

    o, c = price, price + 0.0035  # extension bar: stretches >= 1*ATR above EMA20
    bar(o, c + 0.0005, o - 0.00005, c)
    price = c

    for mv in down_bars:
        o, c = price, price - mv
        bar(o, o + 0.0002, c - 0.0002, c)
        price = c

    o, c = price, price + 0.0020  # reversal bar: bullish, closes back above EMA20
    bar(o, c + 0.0002, o - 0.0003, c)
    price = c

    return pd.DataFrame(rows)


def _straight_uptrend_m15(n: int = 25) -> pd.DataFrame:
    rows = []
    price = 1.1000
    for _ in range(n):
        o, c = price, price + 0.0004
        rows.append({"open": o, "high": c + 0.0002, "low": o - 0.00005, "close": c})
        price = c
    return pd.DataFrame(rows)


def test_long_entry_signals_within_pullback_window():
    h1 = _uptrend_h1()
    m15 = _m15_pullback_sequence([0.0012, 0.0012, 0.0010, 0.0008])  # reversal lands at age 5

    ctx = compute_context("EURUSD", h1, m15, PARAMS)
    assert ctx.regime == "LONG"
    assert ctx.pullback_active is True
    assert ctx.pullback_age == 5

    signal = evaluate("EURUSD", h1, m15, PARAMS)
    assert signal is not None
    assert signal.side == "LONG"
    assert signal.sl < signal.entry < signal.tp
    assert signal.tp - signal.entry == pytest.approx((signal.entry - signal.sl) * PARAMS.tp_r_multiple)


def test_no_signal_once_pullback_has_expired():
    h1 = _uptrend_h1()
    # six down bars instead of four -> the reversal bar lands after the
    # pullback's 6-candle expiry window has closed.
    m15 = _m15_pullback_sequence([0.0012, 0.0010, 0.0006, 0.0004, 0.0003, 0.0002])

    ctx = compute_context("EURUSD", h1, m15, PARAMS)
    assert ctx.pullback_active is False

    signal = evaluate("EURUSD", h1, m15, PARAMS)
    assert signal is None


def test_no_signal_without_regime():
    h1 = _flat_h1()
    m15 = _m15_pullback_sequence([0.0012, 0.0012, 0.0010, 0.0008])

    ctx = compute_context("EURUSD", h1, m15, PARAMS)
    assert ctx.regime == "NONE"
    assert evaluate("EURUSD", h1, m15, PARAMS) is None


def test_no_signal_without_pullback():
    h1 = _uptrend_h1()
    m15 = _straight_uptrend_m15()  # regime is LONG but price never pulls back

    ctx = compute_context("EURUSD", h1, m15, PARAMS)
    assert ctx.regime == "LONG"
    assert ctx.pullback_active is False
    assert evaluate("EURUSD", h1, m15, PARAMS) is None
