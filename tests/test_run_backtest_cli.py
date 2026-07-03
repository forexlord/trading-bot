import sys

import pandas as pd
import pytest

import run_backtest
from src.config import Settings
from src.data.store import Store
from tests.test_strategy import _m15_pullback_sequence, _uptrend_h1


def _build_symbol_candles():
    m15 = _m15_pullback_sequence([0.0012, 0.0012, 0.0010, 0.0008])
    m15["time"] = pd.date_range("2024-01-02 00:00", periods=len(m15), freq="15min", tz="UTC")

    extra_rows = []
    price = m15["close"].iloc[-1]
    for _ in range(3):
        o, c = price, price + 0.0100
        extra_rows.append({"open": o, "high": c + 0.0005, "low": o - 0.0005, "close": c})
        price = c
    extra = pd.DataFrame(extra_rows)
    extra["time"] = pd.date_range(m15["time"].iloc[-1] + pd.Timedelta(minutes=15), periods=3, freq="15min", tz="UTC")
    m15 = pd.concat([m15, extra], ignore_index=True)

    h1 = _uptrend_h1()
    h1["time"] = pd.date_range("2023-12-30 00:00", periods=len(h1), freq="1h", tz="UTC")
    return h1, m15


def _fake_settings() -> Settings:
    return Settings(
        pairs=["EURUSD", "GBPUSD"],
        risk_per_trade=0.5,  # generous so lot sizing clears volume_min in this small fixture
        max_open_trades=2,
        max_per_symbol=1,
        daily_loss_limit=0.04,
        max_drawdown_kill=0.12,
        cooldown_after_loss_min=15,
        session_utc=["00:00", "23:59"],
        max_spread_pips={"EURUSD": 5.0, "GBPUSD": 5.0},
        trend_ema=10,
        pullback_ema=5,
        rsi_period=5,
        atr_period=5,
        atr_sl_mult=1.5,
        tp_r_multiple=2.0,
        spread_buffer_pips=1.0,
        pullback_lookback=8,
        pullback_expiry=6,
        swing_lookback=4,
        h1_slope_lookback=3,
        backtest_start_equity=4000.0,
    )


def test_run_backtest_cli_end_to_end(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "test.db"
    log_dir = tmp_path / "logs"

    h1, m15 = _build_symbol_candles()
    store = Store(db_path)
    for symbol in ("EURUSD", "GBPUSD"):
        store.upsert_candles(symbol, "H1", h1)
        store.upsert_candles(symbol, "M15", m15)
    store.close()

    monkeypatch.setattr(run_backtest, "load_settings", _fake_settings)
    monkeypatch.setattr(sys, "argv", ["run_backtest.py", "--db", str(db_path), "--log-dir", str(log_dir)])

    run_backtest.main()

    captured = capsys.readouterr().out
    assert "=== Backtest Report ===" in captured
    assert "Trades" in captured
    assert (log_dir).glob("decisions-*.jsonl") is not None
