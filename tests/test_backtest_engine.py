from types import SimpleNamespace

import pandas as pd
import pytest

from src.backtest.engine import BacktestEngine, OpenPosition, _SymbolData, assumed_pip_value_per_lot
from src.strategy.common import pip_size
from tests.test_strategy import PARAMS as STRAT_PARAMS
from tests.test_strategy import _m15_pullback_sequence, _uptrend_h1

RISK_PARAMS = SimpleNamespace(
    daily_loss_limit=0.04,
    max_drawdown_kill=0.12,
    max_spread_pips={"EURUSD": 1.5},
    spread_buffer_pips=1.0,
)


def make_engine(tmp_path, data=None):
    return BacktestEngine(data=data or {}, params=RISK_PARAMS, log_dir=str(tmp_path), start_equity=4000.0)


def test_pip_value_usd_quote_pairs():
    # 1 pip * 100k contract, in cents: 0.0001 * 100000 * 100 = 1000
    assert assumed_pip_value_per_lot("EURUSD", 1.10) == pytest.approx(1000.0)
    assert assumed_pip_value_per_lot("GBPUSDm", 1.30) == pytest.approx(1000.0)  # cent suffix


def test_pip_value_usd_base_pairs_divide_by_price():
    # USDJPY: pip 0.01, value = 0.01 * 100000 / 150 = 6.67 USD -> 666.7 cents
    assert assumed_pip_value_per_lot("USDJPY", 150.0) == pytest.approx(0.01 * 100000 / 150.0 * 100)
    assert assumed_pip_value_per_lot("USDCADm", 1.36) == pytest.approx(0.0001 * 100000 / 1.36 * 100)


def test_pip_value_rejects_crosses():
    with pytest.raises(ValueError):
        assumed_pip_value_per_lot("EURJPY", 160.0)


def test_pip_size_btc():
    assert pip_size("BTCUSDm") == 1.0


def test_pip_value_btc_usd():
    # $1 pip on 1 BTC lot ≈ $1 = 100 cents
    assert assumed_pip_value_per_lot("BTCUSDm", 97000.0) == pytest.approx(100.0)


def test_both_touched_in_one_candle_resolves_to_sl(tmp_path):
    engine = make_engine(tmp_path)
    pip_value = assumed_pip_value_per_lot("EURUSD", 1.1050)
    position = OpenPosition(
        trade_id="t1",
        symbol="EURUSD",
        side="LONG",
        lots=0.02,
        entry=1.1050,
        sl=1.1030,
        tp=1.1090,
        sl_pips=20.0,
        risk_amount=20.0 * pip_value * 0.02,
        entry_time=pd.Timestamp("2024-01-02 10:00", tz="UTC"),
        entry_context={},
    )
    engine.open_trades["EURUSD"] = position
    engine.data = {
        "EURUSD": SimpleNamespace(
            m15=pd.DataFrame(
                [
                    {
                        "time": pd.Timestamp("2024-01-02 10:15", tz="UTC"),
                        "open": 1.1050,
                        "high": 1.1095,  # touches TP (1.1090)
                        "low": 1.1020,  # also touches SL (1.1030)
                        "close": 1.1060,
                    }
                ]
            ),
            h1=pd.DataFrame(),
        )
    }

    engine._process_exits("EURUSD", 0, pd.Timestamp("2024-01-02 10:15", tz="UTC"))

    assert "EURUSD" not in engine.open_trades
    assert len(engine.closed_trades) == 1
    assert engine.closed_trades[0]["outcome"] == "SL"
    assert engine.closed_trades[0]["pnl"] < 0


def test_tp_only_touch_closes_as_tp_with_profit(tmp_path):
    engine = make_engine(tmp_path)
    pip_value = assumed_pip_value_per_lot("EURUSD", 1.1050)
    position = OpenPosition(
        trade_id="t2",
        symbol="EURUSD",
        side="LONG",
        lots=0.02,
        entry=1.1050,
        sl=1.1030,
        tp=1.1090,
        sl_pips=20.0,
        risk_amount=20.0 * pip_value * 0.02,
        entry_time=pd.Timestamp("2024-01-02 10:00", tz="UTC"),
        entry_context={},
    )
    engine.open_trades["EURUSD"] = position
    engine.data = {
        "EURUSD": SimpleNamespace(
            m15=pd.DataFrame(
                [
                    {
                        "time": pd.Timestamp("2024-01-02 10:15", tz="UTC"),
                        "open": 1.1050,
                        "high": 1.1095,
                        "low": 1.1045,  # never reaches SL
                        "close": 1.1080,
                    }
                ]
            ),
            h1=pd.DataFrame(),
        )
    }

    engine._process_exits("EURUSD", 0, pd.Timestamp("2024-01-02 10:15", tz="UTC"))

    assert engine.closed_trades[0]["outcome"] == "TP"
    assert engine.closed_trades[0]["pnl"] > 0


def test_full_run_opens_and_closes_a_trade(tmp_path):
    m15 = _m15_pullback_sequence([0.0012, 0.0012, 0.0010, 0.0008])  # ends right at the entry bar
    m15["time"] = pd.date_range("2024-01-02 00:00", periods=len(m15), freq="15min", tz="UTC")

    # a few strongly bullish bars after entry to guarantee TP is reached
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

    full_params = SimpleNamespace(
        **vars(STRAT_PARAMS),
        # generous risk_per_trade: this is a plumbing smoke test for the
        # engine wiring, not a realistic-economics test (a genuine $40
        # cent account often can't clear volume_min at 1% risk — see
        # test_risk_manager.py for the sizing-edge-case coverage).
        risk_per_trade=0.5,
        max_open_trades=2,
        max_per_symbol=1,
        daily_loss_limit=0.04,
        max_drawdown_kill=0.12,
        cooldown_after_loss_min=15,
        session_utc=["00:00", "23:59"],
        max_spread_pips={"EURUSD": 5.0},
    )

    engine = BacktestEngine(
        data={"EURUSD": _SymbolData(h1=h1, m15=m15)},
        params=full_params,
        log_dir=str(tmp_path),
        start_equity=4000.0,
    )
    engine.run()

    assert len(engine.closed_trades) == 1
    trade = engine.closed_trades[0]
    assert trade["outcome"] == "TP"
    assert trade["side"] == "LONG"
    assert engine.balance > 4000.0

    decisions_files = list((tmp_path).glob("decisions-*.jsonl"))
    trades_files = list((tmp_path).glob("trades-*.jsonl"))
    assert decisions_files and trades_files
