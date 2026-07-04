"""Backtest performance report: trades/equity from the Store, reject-reason
counts from the decision JSONL logs. Supports restricting to a date range so
two halves of history can be evaluated independently.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from tabulate import tabulate

from src.data.store import Store


def _as_utc_timestamp(value: Optional[datetime]) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def load_reject_reason_counts(
    log_dir: str | Path, start: Optional[datetime] = None, end: Optional[datetime] = None
) -> dict[str, int]:
    counts: dict[str, int] = {}
    start_ts = _as_utc_timestamp(start)
    end_ts = _as_utc_timestamp(end)

    for path in sorted(Path(log_dir).glob("decisions-*.jsonl")):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                reason = record.get("reject_reason")
                if reason is None:
                    continue
                ts = pd.Timestamp(record["ts"])
                if start_ts is not None and ts < start_ts:
                    continue
                if end_ts is not None and ts > end_ts:
                    continue
                counts[reason] = counts.get(reason, 0) + 1
    return counts


def compute_stats(trades: pd.DataFrame, equity_curve: pd.DataFrame) -> dict:
    if trades.empty or "exit_time" not in trades.columns:
        return {"trade_count": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0, "net_pnl": 0.0}

    closed = trades[trades["exit_time"].notna()].copy()
    stats: dict = {"trade_count": len(closed)}
    if closed.empty:
        stats.update(wins=0, losses=0, gross_win=0.0, gross_loss=0.0, net_pnl=0.0)
        return stats

    wins = closed[closed["pnl"] > 0]
    losses = closed[closed["pnl"] <= 0]
    stats["wins"] = int(len(wins))
    stats["losses"] = int(len(losses))
    stats["win_rate"] = len(wins) / len(closed)
    stats["avg_win"] = float(wins["pnl"].mean()) if not wins.empty else 0.0
    stats["avg_loss"] = float(losses["pnl"].mean()) if not losses.empty else 0.0

    gross_win = float(wins["pnl"].sum()) if not wins.empty else 0.0
    gross_loss = float(-losses["pnl"].sum()) if not losses.empty else 0.0
    stats["gross_win"] = gross_win
    stats["gross_loss"] = gross_loss
    stats["net_pnl"] = float(closed["pnl"].sum())
    stats["profit_factor"] = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    if not equity_curve.empty:
        running_max = equity_curve["equity"].cummax()
        drawdown = (equity_curve["equity"] - running_max) / running_max
        stats["max_drawdown_pct"] = float(drawdown.min())
    else:
        stats["max_drawdown_pct"] = None

    ordered = closed.sort_values("entry_time")
    longest = streak = 0
    for is_loss in (ordered["pnl"] <= 0):
        streak = streak + 1 if is_loss else 0
        longest = max(longest, streak)
    stats["longest_loss_streak"] = longest

    exit_month = pd.to_datetime(closed["exit_time"], unit="s", utc=True).dt.tz_localize(None).dt.to_period("M")
    stats["monthly_pnl"] = closed.groupby(exit_month)["pnl"].sum().to_dict()

    stats["mae_winners"] = _describe(wins["mae_pips"])
    stats["mae_losers"] = _describe(losses["mae_pips"])
    stats["mfe_winners"] = _describe(wins["mfe_pips"])
    stats["mfe_losers"] = _describe(losses["mfe_pips"])

    return stats


def _describe(series: pd.Series) -> dict:
    if series.empty:
        return {}
    return {k: float(v) for k, v in series.describe().to_dict().items()}


def print_report(
    store: Store,
    log_dir: str | Path,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> None:
    trades = store.all_trades()
    if not trades.empty and (start is not None or end is not None):
        entry_dt = pd.to_datetime(trades["entry_time"], unit="s", utc=True)
        start_ts, end_ts = _as_utc_timestamp(start), _as_utc_timestamp(end)
        if start_ts is not None:
            trades = trades[entry_dt >= start_ts]
        if end_ts is not None:
            trades = trades[entry_dt <= end_ts]

    print_report_from_frames(trades, store.equity_curve(), log_dir, start, end)


def print_report_from_frames(
    trades: pd.DataFrame,
    equity_curve: pd.DataFrame,
    log_dir: str | Path,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> None:
    """Same as print_report but takes trades/equity directly as DataFrames —
    used by run_backtest.py, which gets them from BacktestEngine rather than
    a Store.
    """
    stats = compute_stats(trades, equity_curve)
    reject_counts = load_reject_reason_counts(log_dir, start, end)

    print("=== Backtest Report ===")
    if stats.get("trade_count", 0) == 0:
        print("No closed trades in range.")
    else:
        dd = stats["max_drawdown_pct"]
        # Account units are Exness cents (1 unit = $0.01).
        print(
            tabulate(
                [
                    ["Trades", stats["trade_count"]],
                    ["Wins", f"{stats['wins']} ({stats['win_rate']:.1%})"],
                    ["Losses", f"{stats['losses']} ({1 - stats['win_rate']:.1%})"],
                    ["Total won", f"{stats['gross_win']:.2f} cents (${stats['gross_win'] / 100:.2f})"],
                    ["Total lost", f"{stats['gross_loss']:.2f} cents (${stats['gross_loss'] / 100:.2f})"],
                    ["Net PnL", f"{stats['net_pnl']:.2f} cents (${stats['net_pnl'] / 100:.2f})"],
                    ["Avg win", f"{stats['avg_win']:.2f} (${stats['avg_win'] / 100:.2f})"],
                    ["Avg loss", f"{stats['avg_loss']:.2f} (${stats['avg_loss'] / 100:.2f})"],
                    ["Profit factor", f"{stats['profit_factor']:.2f}"],
                    ["Max drawdown", f"{dd:.1%}" if dd is not None else "n/a"],
                    ["Longest loss streak", stats["longest_loss_streak"]],
                ],
                headers=["Metric", "Value"],
            )
        )

        print("\nMonthly PnL:")
        for month, pnl in stats["monthly_pnl"].items():
            print(f"  {month}: {pnl:.2f}")

        print("\nMAE (pips) -- winners vs losers:")
        print(f"  winners: {stats['mae_winners']}")
        print(f"  losers:  {stats['mae_losers']}")
        print("\nMFE (pips) -- winners vs losers:")
        print(f"  winners: {stats['mfe_winners']}")
        print(f"  losers:  {stats['mfe_losers']}")

    print("\nRejections by reason:")
    if reject_counts:
        print(tabulate(sorted(reject_counts.items(), key=lambda kv: -kv[1]), headers=["Reason", "Count"]))
    else:
        print("  none")
