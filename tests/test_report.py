import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.backtest.report import compute_stats, load_reject_reason_counts


def _trade(entry_time, exit_time, pnl, mae=5.0, mfe=10.0):
    return {
        "entry_time": entry_time,
        "exit_time": exit_time,
        "pnl": pnl,
        "mae_pips": mae,
        "mfe_pips": mfe,
    }


def test_compute_stats_basic_metrics():
    trades = pd.DataFrame(
        [
            _trade(1, 1000, 100.0),  # win
            _trade(2, 2000, -50.0),  # loss
            _trade(3, 3000, -30.0),  # loss (2nd in a row)
            _trade(4, 4000, 80.0),  # win
            _trade(5, 5000, -40.0),  # loss
        ]
    )
    equity_curve = pd.DataFrame({"equity": [4000, 4100, 4050, 4180, 4140, 4110]})

    stats = compute_stats(trades, equity_curve)

    assert stats["trade_count"] == 5
    assert stats["win_rate"] == pytest.approx(0.4)
    assert stats["profit_factor"] == pytest.approx(180.0 / 120.0)
    assert stats["longest_loss_streak"] == 2
    assert stats["max_drawdown_pct"] == pytest.approx((4110 - 4180) / 4180)


def test_compute_stats_handles_no_closed_trades():
    trades = pd.DataFrame([{"entry_time": 1, "exit_time": None, "pnl": None, "mae_pips": None, "mfe_pips": None}])
    stats = compute_stats(trades, pd.DataFrame({"equity": [4000]}))
    assert stats["trade_count"] == 0


def test_compute_stats_all_wins_gives_infinite_profit_factor():
    trades = pd.DataFrame([_trade(1, 1000, 50.0), _trade(2, 2000, 30.0)])
    stats = compute_stats(trades, pd.DataFrame({"equity": [4000, 4080]}))
    assert stats["profit_factor"] == float("inf")


def test_load_reject_reason_counts(tmp_path):
    path = tmp_path / "decisions-2024-01-01.jsonl"
    lines = [
        {"ts": "2024-01-01T08:00:00+00:00", "reject_reason": "spread"},
        {"ts": "2024-01-01T09:00:00+00:00", "reject_reason": "spread"},
        {"ts": "2024-01-01T10:00:00+00:00", "reject_reason": "session"},
        {"ts": "2024-01-01T11:00:00+00:00", "reject_reason": None},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines))

    counts = load_reject_reason_counts(tmp_path)
    assert counts == {"spread": 2, "session": 1}


def test_load_reject_reason_counts_respects_date_range(tmp_path):
    path = tmp_path / "decisions-2024-01-01.jsonl"
    lines = [
        {"ts": "2024-01-01T08:00:00+00:00", "reject_reason": "spread"},
        {"ts": "2024-01-01T20:00:00+00:00", "reject_reason": "session"},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines))

    counts = load_reject_reason_counts(
        tmp_path,
        start=datetime(2024, 1, 1, 12, tzinfo=timezone.utc),
        end=datetime(2024, 1, 1, 23, tzinfo=timezone.utc),
    )
    assert counts == {"session": 1}
