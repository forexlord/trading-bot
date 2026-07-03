from datetime import datetime, timezone

from src.state import BotState, StateStore, TradeState


def test_load_with_no_file_returns_fresh_state(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = store.load(default_equity=4000.0)
    assert state.day_start_equity == 4000.0
    assert state.hwm == 4000.0
    assert state.kill_switch_triggered is False
    assert state.open_trades == {}


def test_save_and_load_roundtrip(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = BotState(day_start_equity=4000.0, hwm=4200.0, kill_switch_triggered=False, current_day="2024-01-02")
    state.record_trade_open(
        TradeState(
            trade_id="t1", symbol="EURUSD", side="LONG", lots=0.02, entry=1.1050, sl=1.1030,
            tp=1.1090, sl_pips=20.0, risk_amount=40.0, entry_time="2024-01-02T10:00:00+00:00",
            entry_context={"rsi": 55.0},
        )
    )
    store.save(state)

    reloaded = store.load(default_equity=0.0)
    assert reloaded.hwm == 4200.0
    assert reloaded.current_day == "2024-01-02"
    assert "EURUSD" in reloaded.open_trades
    assert reloaded.open_trades["EURUSD"].entry == 1.1050
    assert reloaded.last_entry_time_by_symbol["EURUSD"] == "2024-01-02T10:00:00+00:00"


def test_kill_switch_latch_persists_across_reload(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = store.load(default_equity=4000.0)
    state.hwm = 4000.0
    tripped = state.maybe_trip_kill_switch(equity=3500.0, max_drawdown_kill=0.12)
    assert tripped is True
    store.save(state)

    # equity recovers above threshold, but a fresh load must still be latched
    reloaded = store.load(default_equity=4000.0)
    assert reloaded.kill_switch_triggered is True
    still_tripped = reloaded.maybe_trip_kill_switch(equity=5000.0, max_drawdown_kill=0.12)
    assert still_tripped is True


def test_roll_day_resets_day_start_equity_once_per_day():
    state = BotState(day_start_equity=4000.0, hwm=4000.0)
    t1 = datetime(2024, 1, 2, 8, 0, tzinfo=timezone.utc)
    assert state.roll_day_if_needed(t1, current_equity=4050.0) is True
    assert state.day_start_equity == 4050.0

    t2 = datetime(2024, 1, 2, 14, 0, tzinfo=timezone.utc)
    assert state.roll_day_if_needed(t2, current_equity=4100.0) is False
    assert state.day_start_equity == 4050.0  # unchanged within the same day

    t3 = datetime(2024, 1, 3, 1, 0, tzinfo=timezone.utc)
    assert state.roll_day_if_needed(t3, current_equity=4100.0) is True
    assert state.day_start_equity == 4100.0


def test_record_trade_close_moves_out_of_open_trades():
    state = BotState(day_start_equity=4000.0, hwm=4000.0)
    state.record_trade_open(
        TradeState(
            trade_id="t1", symbol="EURUSD", side="LONG", lots=0.02, entry=1.1050, sl=1.1030,
            tp=1.1090, sl_pips=20.0, risk_amount=40.0, entry_time="2024-01-02T10:00:00+00:00",
            entry_context={},
        )
    )
    closed_at = datetime(2024, 1, 2, 10, 30, tzinfo=timezone.utc)
    state.record_trade_close("EURUSD", closed_at, was_loss=True)

    assert "EURUSD" not in state.open_trades
    assert state.last_trade_by_symbol["EURUSD"]["was_loss"] is True

    last_trade_info = state.to_risk_last_trade_by_symbol()["EURUSD"]
    assert last_trade_info.was_loss is True
    assert last_trade_info.closed_at == closed_at
