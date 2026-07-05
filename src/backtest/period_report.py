"""Rolling daily / weekly / monthly PnL for a compounding backtest window.

Equity at the close of each day becomes the opening balance for the next —
matching how the live bot treats day-start equity for risk sizing.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

import pandas as pd
from tabulate import tabulate

from src.backtest.report import compute_stats


def _as_utc_ts(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _filter_trades_in_period(
    trades: pd.DataFrame, period_start: pd.Timestamp, period_end: pd.Timestamp
) -> pd.DataFrame:
    if trades.empty or "exit_time" not in trades.columns:
        return trades.iloc[0:0]
    closed = trades[trades["exit_time"].notna()].copy()
    exit_ts = pd.to_datetime(closed["exit_time"], unit="s", utc=True)
    return closed[(exit_ts >= period_start) & (exit_ts <= period_end)]


def compute_period_rollups(
    equity_curve: pd.DataFrame,
    trades: pd.DataFrame,
    start_equity: float,
    period_start: datetime | pd.Timestamp,
    period_end: datetime | pd.Timestamp,
    deposits: dict[Any, float] | None = None,
) -> dict[str, Any]:
    """Build compounding daily → weekly → monthly PnL from end-of-day equity.

    ``deposits`` maps a ``date`` to cents added at the START of that day (a
    recurring contribution). Deposits are excluded from trading PnL: a day's
    reported PnL is close - (prior close + that day's deposit), so the daily /
    weekly / monthly / net figures reflect only what trading did, not money
    you paid in.
    """
    deposits = deposits or {}
    total_deposited = float(sum(deposits.values()))
    p0 = _as_utc_ts(period_start)
    p1 = _as_utc_ts(period_end)

    result: dict[str, Any] = {
        "period_start": p0.isoformat(),
        "period_end": p1.isoformat(),
        "start_equity": start_equity,
        "total_deposited": total_deposited,
        "total_invested": float(start_equity) + total_deposited,
        "daily": {},
        "weekly": {},
        "monthly": {},
    }

    if equity_curve.empty:
        result.update(
            end_equity=start_equity, net_pnl=0.0, trading_days=0,
        )
        return result

    eq = equity_curve.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    eq = eq[(eq["ts"] >= p0) & (eq["ts"] <= p1)].sort_values("ts")
    if eq.empty:
        result.update(
            end_equity=start_equity, net_pnl=0.0, trading_days=0,
        )
        return result

    eod = eq.groupby(eq["ts"].dt.date, sort=True)["equity"].last()
    daily: dict[str, dict[str, float]] = {}
    balance = float(start_equity)
    for d, close in eod.items():
        deposit = float(deposits.get(d, 0.0))
        day_open = balance + deposit  # contribution lands before trading
        close_f = float(close)
        daily[str(d)] = {
            "start_equity": day_open,
            "deposit": deposit,
            "end_equity": close_f,
            "pnl": close_f - day_open,  # trading only, excludes the deposit
        }
        balance = close_f

    weekly: dict[str, float] = {}
    monthly: dict[str, float] = {}
    for day_str, row in daily.items():
        d = date.fromisoformat(day_str)
        iso = d.isocalendar()
        week_key = f"{iso.year}-W{iso.week:02d}"
        month_key = day_str[:7]
        weekly[week_key] = weekly.get(week_key, 0.0) + row["pnl"]
        monthly[month_key] = monthly.get(month_key, 0.0) + row["pnl"]

    pnls = [row["pnl"] for row in daily.values()]
    result["daily"] = daily
    result["weekly"] = dict(sorted(weekly.items()))
    result["monthly"] = dict(sorted(monthly.items()))
    result["end_equity"] = balance
    # Trading PnL only: growth minus the money paid in.
    result["net_pnl"] = balance - start_equity - total_deposited
    result["trading_days"] = len(daily)
    result["avg_daily_pnl"] = result["net_pnl"] / len(daily) if daily else 0.0
    result["avg_weekly_pnl"] = result["net_pnl"] / len(weekly) if weekly else 0.0
    result["best_day"] = max(daily.items(), key=lambda kv: kv[1]["pnl"]) if daily else None
    result["worst_day"] = min(daily.items(), key=lambda kv: kv[1]["pnl"]) if daily else None

    period_trades = _filter_trades_in_period(trades, p0, p1)
    period_eq = eq  # drawdown within window only
    result["trade_stats"] = compute_stats(period_trades, period_eq)

    return result


def _usd(cents: float) -> str:
    return f"${cents / 100:,.2f}"


def print_period_report(
    rollups: dict[str, Any],
    trades: pd.DataFrame,
    equity_curve: pd.DataFrame,
    log_dir: str | None = None,
) -> None:
    start = rollups["start_equity"]
    end = rollups["end_equity"]
    net = rollups["net_pnl"]
    deposited = rollups.get("total_deposited", 0.0)
    invested = rollups.get("total_invested", start)

    print("=== Period Backtest (compounding daily balance) ===")
    print(f"Window:     {rollups['period_start'][:10]} → {rollups['period_end'][:10]}")
    print(f"Start:      {_usd(start)}")
    if deposited:
        print(f"Deposited:  {_usd(deposited)} (recurring contributions)")
        print(f"Invested:   {_usd(invested)} (start + deposits)")
    print(f"End:        {_usd(end)}")
    denom = invested if deposited else start
    print(f"Trading PnL: {_usd(net)} ({net / denom:+.1%} on invested capital)")
    if deposited:
        print(f"  (End = Invested {_usd(invested)} + Trading PnL {_usd(net)})")
    print(f"Trading days: {rollups['trading_days']}")
    print(f"Avg daily:  {_usd(rollups['avg_daily_pnl'])}")
    print(f"Avg weekly: {_usd(rollups['avg_weekly_pnl'])}")

    ts = rollups.get("trade_stats", {})
    if ts.get("trade_count", 0):
        print(
            f"Trades: {ts['trade_count']}  WR: {ts['win_rate']:.1%}  "
            f"PF: {ts['profit_factor']:.2f}  Max DD: {ts.get('max_drawdown_pct', 0):.1%}"
        )

    print("\n--- Monthly PnL ---")
    for month, pnl in rollups["monthly"].items():
        print(f"  {month}: {_usd(pnl)}")

    print("\n--- Weekly PnL ---")
    for week, pnl in rollups["weekly"].items():
        print(f"  {week}: {_usd(pnl)}")

    print("\n--- Daily PnL (each day opens at prior close) ---")
    has_deposits = rollups.get("total_deposited", 0.0) > 0
    rows = []
    for day, row in rollups["daily"].items():
        cols = [day, _usd(row["start_equity"])]
        if has_deposits:
            dep = row.get("deposit", 0.0)
            cols.append(_usd(dep) if dep else "")
        cols += [_usd(row["pnl"]), _usd(row["end_equity"])]
        rows.append(cols)
    if rows:
        headers = ["Date", "Day open"] + (["Deposit"] if has_deposits else []) + ["Day PnL", "Day close"]
        print(tabulate(rows, headers=headers, tablefmt="simple"))
    else:
        print("  (no equity snapshots in window)")

    if rollups.get("best_day"):
        d, r = rollups["best_day"]
        print(f"\nBest day:  {d}  {_usd(r['pnl'])}")
    if rollups.get("worst_day"):
        d, r = rollups["worst_day"]
        print(f"Worst day: {d}  {_usd(r['pnl'])}")
