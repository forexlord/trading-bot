import numpy as np
import pandas as pd
import pytest

from src.indicators.ta import atr_wilder, ema, rsi_wilder, swing_high, swing_low


def test_ema_matches_hand_computed():
    series = pd.Series([1, 2, 3, 4, 5, 6, 7], dtype=float)
    result = ema(series, period=3)

    assert result.iloc[:2].isna().all()
    expected = [2.0, 3.0, 4.0, 5.0, 6.0]
    for i, exp in zip(range(2, 7), expected):
        assert result.iloc[i] == pytest.approx(exp)


def test_ema_insufficient_data_is_all_nan():
    series = pd.Series([1, 2], dtype=float)
    result = ema(series, period=3)
    assert result.isna().all()


def test_rsi_wilder_matches_hand_computed():
    series = pd.Series([10, 12, 11, 13, 12], dtype=float)
    result = rsi_wilder(series, period=2)

    assert result.iloc[:2].isna().all()
    assert result.iloc[2] == pytest.approx(66.6667, abs=1e-3)
    assert result.iloc[3] == pytest.approx(85.7143, abs=1e-3)
    assert result.iloc[4] == pytest.approx(54.5455, abs=1e-3)


def test_rsi_wilder_flat_series_is_neutral():
    series = pd.Series([10, 10, 10, 10, 10], dtype=float)
    result = rsi_wilder(series, period=2)
    assert result.iloc[2] == pytest.approx(50.0)


def test_rsi_wilder_all_gains_is_100():
    series = pd.Series([10, 11, 12, 13, 14], dtype=float)
    result = rsi_wilder(series, period=2)
    assert result.iloc[2] == pytest.approx(100.0)


def test_atr_wilder_matches_hand_computed():
    df = pd.DataFrame(
        {
            "high": [10, 11, 12, 9],
            "low": [8, 9, 10, 7],
            "close": [9, 10, 11, 8],
        },
        dtype=float,
    )
    result = atr_wilder(df, period=2)

    assert np.isnan(result.iloc[0])
    assert result.iloc[1] == pytest.approx(2.0)
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[3] == pytest.approx(3.0)


def test_swing_high_low():
    df = pd.DataFrame(
        {
            "high": [5, 7, 6, 9, 8],
            "low": [3, 4, 2, 5, 6],
        },
        dtype=float,
    )
    highs = swing_high(df, lookback=3)
    lows = swing_low(df, lookback=3)

    assert highs.iloc[:2].isna().all()
    assert highs.iloc[2] == pytest.approx(7.0)
    assert highs.iloc[3] == pytest.approx(9.0)
    assert highs.iloc[4] == pytest.approx(9.0)

    assert lows.iloc[:2].isna().all()
    assert lows.iloc[2] == pytest.approx(2.0)
    assert lows.iloc[3] == pytest.approx(2.0)
    assert lows.iloc[4] == pytest.approx(2.0)
