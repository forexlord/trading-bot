from types import SimpleNamespace

import pandas as pd
import pytest

from src.backtest.engine import BacktestEngine, OpenPosition, _SymbolData


def _params(**overrides):
    base = dict(
        strategy="trend_pullback",  # engine loads it; tests below patch strat directly
        daily_loss_limit=0.04,
        max_drawdown_kill=0.12,
        max_spread_pips={"EURUSD": 5.0},
        spread_buffer_pips=1.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _m15(times_and_ohlc: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"time": pd.Timestamp(t, tz="UTC"), "open": o, "high": h, "low": l, "close": c}
            for t, o, h, l, c in times_and_ohlc
        ]
    )


class _TrailingStrat:
    """Minimal strategy double: no entries, fixed trailing proposal per call."""

    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.calls = 0

    def update_stop(self, symbol, side, entry, entry_time, current_sl, h1_df, m15_df, params):
        proposal = self.proposals[min(self.calls, len(self.proposals) - 1)]
        self.calls += 1
        return proposal

    @staticmethod
    def pip_size(symbol):
        return 0.0001

    @staticmethod
    def compute_context(*a, **k):
        raise AssertionError("not used")

    @staticmethod
    def evaluate(*a, **k):
        return None


def _engine_with_position(tmp_path, m15: pd.DataFrame, strat) -> BacktestEngine:
    h1 = pd.DataFrame(
        {
            "time": pd.date_range("2024-01-01 00:00", periods=50, freq="1h", tz="UTC"),
            "open": 1.10, "high": 1.11, "low": 1.09, "close": 1.10,
        }
    )
    engine = BacktestEngine(
        data={"EURUSD": _SymbolData(h1=h1, m15=m15)},
        params=_params(),
        log_dir=str(tmp_path),
        start_equity=400000.0,
    )
    engine.strat = strat
    engine.open_trades["EURUSD"] = OpenPosition(
        trade_id="t1", symbol="EURUSD", side="LONG", lots=0.1,
        entry=1.1000, sl=1.0950, tp=1.2000, sl_pips=50.0, risk_amount=5000.0,
        entry_time=m15["time"].iloc[0], entry_context={},
    )
    return engine


def test_trailing_ratchets_up_and_exit_hits_trailed_stop(tmp_path):
    # Bar 1 (:45, H1 boundary): price up, trail proposes 1.1020 (above entry).
    # Bar 2: dips to 1.1015 -> must exit at the TRAILED stop 1.1020 in profit.
    m15 = _m15(
        [
            ("2024-01-02 10:45", 1.1000, 1.1100, 1.0995, 1.1090),
            ("2024-01-02 11:00", 1.1090, 1.1095, 1.1015, 1.1030),
        ]
    )
    strat = _TrailingStrat(proposals=[1.1020])
    engine = _engine_with_position(tmp_path, m15, strat)

    ts1 = m15["time"].iloc[0]
    engine._process_exits("EURUSD", 0, ts1)          # no exit: sl still 1.0950
    engine._update_trailing("EURUSD", 0, ts1)        # :45 -> trail applies
    assert engine.open_trades["EURUSD"].sl == pytest.approx(1.1020)

    ts2 = m15["time"].iloc[1]
    engine._process_exits("EURUSD", 1, ts2)
    assert "EURUSD" not in engine.open_trades
    trade = engine.closed_trades[0]
    assert trade["outcome"] == "SL"                  # stopped by the trail...
    assert trade["pnl"] > 0                          # ...in profit


def test_trailing_never_loosens(tmp_path):
    m15 = _m15([("2024-01-02 10:45", 1.1000, 1.1010, 1.0990, 1.1005)])
    strat = _TrailingStrat(proposals=[1.0900])       # looser than current 1.0950
    engine = _engine_with_position(tmp_path, m15, strat)

    engine._update_trailing("EURUSD", 0, m15["time"].iloc[0])
    assert engine.open_trades["EURUSD"].sl == pytest.approx(1.0950)


def test_trailing_only_runs_on_h1_boundary(tmp_path):
    m15 = _m15([("2024-01-02 10:30", 1.1000, 1.1010, 1.0990, 1.1005)])  # :30, not :45
    strat = _TrailingStrat(proposals=[1.1000])
    engine = _engine_with_position(tmp_path, m15, strat)

    engine._update_trailing("EURUSD", 0, m15["time"].iloc[0])
    assert strat.calls == 0
    assert engine.open_trades["EURUSD"].sl == pytest.approx(1.0950)


def test_h1_slice_excludes_bars_not_yet_closed(tmp_path):
    # M15 bar stamped 10:00 closes at 10:15. The H1 bar stamped 10:00 closes
    # at 11:00 — it must NOT be visible. The newest visible H1 bar is 09:00.
    h1 = pd.DataFrame(
        {
            "time": pd.date_range("2024-01-02 00:00", periods=11, freq="1h", tz="UTC"),  # 00:00..10:00
            "open": 1.10, "high": 1.11, "low": 1.09, "close": 1.10,
        }
    )
    m15 = _m15([("2024-01-02 10:00", 1.1000, 1.1010, 1.0990, 1.1005)])
    engine = BacktestEngine(
        data={"EURUSD": _SymbolData(h1=h1, m15=m15)},
        params=_params(),
        log_dir=str(tmp_path),
        start_equity=400000.0,
    )

    h1_slice, _ = engine._slices("EURUSD", 0, m15["time"].iloc[0])
    assert h1_slice["time"].max() == pd.Timestamp("2024-01-02 09:00", tz="UTC")

    # At 10:45 (closes 11:00) the 10:00 H1 bar IS complete and visible.
    m15_b = _m15([("2024-01-02 10:45", 1.1000, 1.1010, 1.0990, 1.1005)])
    engine2 = BacktestEngine(
        data={"EURUSD": _SymbolData(h1=h1, m15=m15_b)},
        params=_params(),
        log_dir=str(tmp_path / "b"),
        start_equity=400000.0,
    )
    h1_slice_b, _ = engine2._slices("EURUSD", 0, m15_b["time"].iloc[0])
    assert h1_slice_b["time"].max() == pd.Timestamp("2024-01-02 10:00", tz="UTC")


def test_h4_slice_excludes_bars_not_yet_closed(tmp_path):
    # With strategy=h4_trend the higher-timeframe slot carries H4 bars.
    # The 08:00 H4 bar closes at 12:00: invisible at the 11:30 M15 close,
    # visible at the 11:45 M15 close (which lands exactly on 12:00).
    h4 = pd.DataFrame(
        {
            "time": pd.date_range("2024-01-02 00:00", periods=3, freq="4h", tz="UTC"),  # 00,04,08
            "open": 1.10, "high": 1.11, "low": 1.09, "close": 1.10,
        }
    )
    m15_early = _m15([("2024-01-02 11:15", 1.1000, 1.1010, 1.0990, 1.1005)])  # closes 11:30
    engine = BacktestEngine(
        data={"EURUSD": _SymbolData(h1=h4, m15=m15_early)},
        params=_params(strategy="h4_trend"),
        log_dir=str(tmp_path),
        start_equity=400000.0,
    )
    assert engine.htf == "H4"
    slice_early, _ = engine._slices("EURUSD", 0, m15_early["time"].iloc[0])
    assert slice_early["time"].max() == pd.Timestamp("2024-01-02 04:00", tz="UTC")

    m15_late = _m15([("2024-01-02 11:45", 1.1000, 1.1010, 1.0990, 1.1005)])  # closes 12:00
    engine2 = BacktestEngine(
        data={"EURUSD": _SymbolData(h1=h4, m15=m15_late)},
        params=_params(strategy="h4_trend"),
        log_dir=str(tmp_path / "b"),
        start_equity=400000.0,
    )
    slice_late, _ = engine2._slices("EURUSD", 0, m15_late["time"].iloc[0])
    assert slice_late["time"].max() == pd.Timestamp("2024-01-02 08:00", tz="UTC")
