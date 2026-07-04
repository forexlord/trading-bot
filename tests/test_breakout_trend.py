from types import SimpleNamespace

import pandas as pd

from src.strategy.breakout_trend import compute_context, evaluate
from src.strategy import load_strategy


PARAMS = SimpleNamespace(
    trend_ema=10,
    pullback_ema=5,
    atr_period=5,
    rsi_period=5,
    h1_slope_lookback=3,
    atr_sl_mult=1.2,
    tp_r_multiple=1.5,
    spread_buffer_pips=1.0,
    breakout_lookback=5,
    min_trend_atr_frac=0.05,
    require_impulse_candle=True,
    breakout_rsi_long_max=90.0,
    breakout_rsi_short_min=20.0,
)


def _uptrend_h1(n: int = 30) -> pd.DataFrame:
    closes = [1.1000 + 0.0015 * i for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.0003 for c in closes],
            "high": [c + 0.0008 for c in closes],
            "low": [c - 0.0008 for c in closes],
            "close": closes,
        }
    )


def _breakout_m15(n: int = 20) -> pd.DataFrame:
    # Quiet range then a bullish breakout bar.
    rows = []
    for i in range(n - 1):
        c = 1.1200 + 0.0001 * (i % 3)
        rows.append({"open": c - 0.0001, "high": c + 0.0002, "low": c - 0.0002, "close": c})
    # Break above prior highs (~1.1204)
    rows.append({"open": 1.1202, "high": 1.1220, "low": 1.1200, "close": 1.1215})
    return pd.DataFrame(rows)


def test_load_strategy_breakout():
    mod = load_strategy("breakout_trend")
    assert hasattr(mod, "evaluate") and hasattr(mod, "compute_context")


def test_breakout_long_signal():
    h1 = _uptrend_h1()
    m15 = _breakout_m15()
    ctx = compute_context("EURUSDm", h1, m15, PARAMS)
    assert ctx.regime == "LONG"
    signal = evaluate("EURUSDm", h1, m15, PARAMS)
    assert signal is not None
    assert signal.side == "LONG"
    assert signal.tp > signal.entry > signal.sl


def test_no_signal_without_breakout():
    h1 = _uptrend_h1()
    m15 = _breakout_m15()
    # Flatten last bar so it does not break the channel.
    m15.loc[m15.index[-1], ["open", "high", "low", "close"]] = [1.1200, 1.1202, 1.1198, 1.1201]
    signal = evaluate("EURUSDm", h1, m15, PARAMS)
    assert signal is None
