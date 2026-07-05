from datetime import datetime, timezone

import pandas as pd
import pytest

from src.backtest.period_report import compute_period_rollups


def test_daily_rollups_compound_balance():
    # Three days: +100, -50, +200 cents on 400000 start
    eq = pd.DataFrame(
        {
            "ts": pd.to_datetime(
                [
                    "2026-01-02 12:00:00+00:00",
                    "2026-01-02 23:00:00+00:00",
                    "2026-01-03 23:00:00+00:00",
                    "2026-01-04 23:00:00+00:00",
                ]
            ),
            "equity": [400000.0, 400100.0, 400050.0, 400250.0],
            "balance": [400000.0, 400100.0, 400050.0, 400250.0],
        }
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc)

    r = compute_period_rollups(eq, pd.DataFrame(), 400000.0, start, end)

    assert r["net_pnl"] == 250.0
    assert r["end_equity"] == 400250.0
    assert r["daily"]["2026-01-02"]["pnl"] == 100.0
    assert r["daily"]["2026-01-03"]["start_equity"] == 400100.0
    assert r["daily"]["2026-01-03"]["pnl"] == -50.0
    assert r["daily"]["2026-01-04"]["pnl"] == 200.0
    assert r["monthly"]["2026-01"] == 250.0


def test_weekly_sums_daily():
    eq = pd.DataFrame(
        {
            "ts": pd.to_datetime(
                [f"2026-01-{d:02d} 20:00:00+00:00" for d in range(5, 12)]
            ),
            "equity": [400000.0 + 100 * i for i in range(7)],
            "balance": [400000.0 + 100 * i for i in range(7)],
        }
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 31, tzinfo=timezone.utc)
    r = compute_period_rollups(eq, pd.DataFrame(), 400000.0, start, end)
    assert sum(r["weekly"].values()) == pytest.approx(r["net_pnl"])
