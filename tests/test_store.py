import pandas as pd
import pytest

from src.data.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _candles(times, base=1.1000):
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times, utc=True),
            "open": [base] * len(times),
            "high": [base + 0.0005] * len(times),
            "low": [base - 0.0005] * len(times),
            "close": [base + 0.0001] * len(times),
            "tick_volume": [100] * len(times),
            "spread": [10] * len(times),
            "real_volume": [0] * len(times),
        }
    )


def test_upsert_and_read_candles_roundtrip(store):
    times = ["2024-01-02 00:00", "2024-01-02 00:15", "2024-01-02 00:30"]
    df = _candles(times)
    n = store.upsert_candles("EURUSD", "M15", df)
    assert n == 3

    result = store.read_candles("EURUSD", "M15")
    assert len(result) == 3
    assert result["close"].iloc[0] == pytest.approx(1.1001)


def test_upsert_is_idempotent_on_conflict(store):
    times = ["2024-01-02 00:00", "2024-01-02 00:15"]
    store.upsert_candles("EURUSD", "M15", _candles(times, base=1.1000))
    store.upsert_candles("EURUSD", "M15", _candles(times, base=1.2000))  # overwrite

    result = store.read_candles("EURUSD", "M15")
    assert len(result) == 2
    assert result["close"].iloc[0] == pytest.approx(1.2001)


def test_find_gaps_ignores_normal_weekend_close(store):
    # Friday 23:45 -> Sunday 22:00 is a normal weekend close, should NOT be flagged.
    times = ["2024-01-05 23:30", "2024-01-05 23:45", "2024-01-07 22:00", "2024-01-07 22:15"]
    store.upsert_candles("EURUSD", "M15", _candles(times))

    assert store.find_gaps("EURUSD", "M15", expected_interval_minutes=15) == []


def test_find_gaps_flags_intraweek_gap(store):
    # Wednesday 00:00 -> Wednesday 04:00 with M15 candles missing in between IS a real gap.
    times = ["2024-01-10 00:00", "2024-01-10 00:15", "2024-01-10 04:00", "2024-01-10 04:15"]
    store.upsert_candles("EURUSD", "M15", _candles(times))

    gaps = store.find_gaps("EURUSD", "M15", expected_interval_minutes=15)
    assert len(gaps) == 1
    assert gaps[0][0] == pd.Timestamp("2024-01-10 00:15", tz="UTC")
    assert gaps[0][1] == pd.Timestamp("2024-01-10 04:00", tz="UTC")


def test_trade_entry_and_exit_roundtrip(store):
    store.record_trade_entry(
        {
            "trade_id": "t1",
            "symbol": "EURUSD",
            "side": "LONG",
            "lots": 0.02,
            "entry": 1.1050,
            "sl": 1.1030,
            "tp": 1.1090,
            "sl_pips": 20.0,
            "risk_amount": 40.0,
            "entry_time": 1700000000,
            "entry_context": {"rsi": 55.0},
        }
    )
    store.record_trade_exit(
        "t1",
        exit_time=1700001000,
        exit_price=1.1090,
        outcome="TP",
        r_result=2.0,
        pnl=80.0,
        hold_minutes=16.6,
        mae_pips=3.0,
        mfe_pips=40.0,
    )

    df = store.all_trades()
    assert len(df) == 1
    assert df["outcome"].iloc[0] == "TP"
    assert df["pnl"].iloc[0] == pytest.approx(80.0)
    assert store.open_trades() == []


def test_open_trades_excludes_closed(store):
    store.record_trade_entry(
        {
            "trade_id": "open1",
            "symbol": "GBPUSD",
            "side": "SHORT",
            "lots": 0.01,
            "entry": 1.25,
            "sl": 1.26,
            "tp": 1.23,
            "sl_pips": 100.0,
            "risk_amount": 40.0,
            "entry_time": 1700000000,
            "entry_context": {},
        }
    )
    open_trades = store.open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["symbol"] == "GBPUSD"


def test_equity_curve_roundtrip(store):
    store.record_equity(1700000000, 4000.0, 4000.0)
    store.record_equity(1700003600, 4010.0, 4000.0)
    curve = store.equity_curve()
    assert len(curve) == 2
    assert curve["equity"].iloc[1] == pytest.approx(4010.0)
