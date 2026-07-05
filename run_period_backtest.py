"""Compounding period backtest with daily / weekly / monthly rollups.

Simulates starting the bot on ``--start`` with ``--equity`` (default $4,000),
trading through ``--end``, carrying end-of-day balance into the next day.

Default window: 2026-01-01 → 2026-06-30 (last six months). Indicator warmup
uses ``--warmup-days`` of history before ``--start`` (not counted in PnL).

Usage::

    KILL_SWITCH_ENABLED=false .venv/bin/python run_period_backtest.py \\
        --db data/forex_bot.db --no-telegram
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.backtest.engine import BacktestEngine, _SymbolData
from src.backtest.period_report import compute_period_rollups, print_period_report
from src.backtest.report import load_reject_reason_counts
from src.config import load_settings
from src.data.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_period_backtest")

WARMUP_DAYS = 60
DEFAULT_START = "2026-01-01"
DEFAULT_END = "2026-06-30"


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def end_of_day(d: datetime) -> datetime:
    return d.replace(hour=23, minute=59, second=59)


def load_data(
    store: Store,
    pairs: list[str],
    htf: str,
    load_start: datetime,
    load_end: datetime,
) -> dict[str, _SymbolData]:
    from src.strategy.h4_trend import resample_h4

    data: dict[str, _SymbolData] = {}
    for symbol in pairs:
        higher = store.read_candles(symbol, htf, start=load_start, end=load_end)
        if higher.empty and htf == "H4":
            h1_full = store.read_candles(symbol, "H1")
            if not h1_full.empty:
                h4_full = resample_h4(h1_full)
                store.upsert_candles(symbol, "H4", h4_full)
                higher = store.read_candles(symbol, htf, start=load_start, end=load_end)
        m15 = store.read_candles(symbol, "M15", start=load_start, end=load_end)
        if higher.empty or m15.empty:
            raise SystemExit(
                f"No {htf}/M15 candles for {symbol} — run pull_data.py first."
            )
        data[symbol] = _SymbolData(h1=higher, m15=m15)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="forex_bot.db")
    parser.add_argument("--log-dir", default="logs/backtest/period")
    parser.add_argument("--start", type=parse_date, default=parse_date(DEFAULT_START))
    parser.add_argument("--end", type=parse_date, default=parse_date(DEFAULT_END))
    parser.add_argument("--warmup-days", type=int, default=WARMUP_DAYS)
    parser.add_argument("--equity", type=float, default=None)
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    logger.info("kill_switch_enabled=%s", settings.kill_switch_enabled)

    period_start = args.start
    period_end = end_of_day(args.end)
    load_start = period_start - timedelta(days=args.warmup_days)
    start_equity = args.equity if args.equity is not None else settings.backtest_start_equity

    ts_start = pd.Timestamp(period_start)
    ts_end = pd.Timestamp(period_end)

    log_dir = Path(args.log_dir)
    if log_dir.exists():
        for pattern in ("decisions-*.jsonl", "trades-*.jsonl", "equity-*.jsonl"):
            for path in log_dir.glob(pattern):
                path.unlink()

    store = Store(args.db)
    from src.strategy import load_strategy

    htf = getattr(load_strategy(settings.strategy), "HTF", "H1")
    logger.info(
        "Period %s → %s | warmup from %s | HTF %s",
        period_start.date(),
        period_end.date(),
        load_start.date(),
        htf,
    )

    data = load_data(store, settings.pairs, htf, load_start, period_end)
    store.close()

    engine = BacktestEngine(
        data=data,
        params=settings,
        log_dir=str(log_dir),
        start_equity=start_equity,
        timeline_start=ts_start,
        timeline_end=ts_end,
    )
    engine.run()

    trades = pd.DataFrame(engine.closed_trades)
    equity_curve = pd.DataFrame(engine.equity_history)

    rollups = compute_period_rollups(
        equity_curve, trades, start_equity, period_start, period_end
    )
    print_period_report(rollups, trades, equity_curve, str(log_dir))

    reject_counts = load_reject_reason_counts(log_dir, period_start, period_end)
    if reject_counts:
        print("\nRejections by reason:")
        for reason, count in sorted(reject_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {reason}: {count}")

    summary_path = log_dir / "period_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "start_equity_cents": start_equity,
        "rollups": {
            k: v
            for k, v in rollups.items()
            if k not in ("trade_stats", "best_day", "worst_day")
        },
        "best_day": {"date": rollups["best_day"][0], "pnl": rollups["best_day"][1]["pnl"]}
        if rollups.get("best_day")
        else None,
        "worst_day": {"date": rollups["worst_day"][0], "pnl": rollups["worst_day"][1]["pnl"]}
        if rollups.get("worst_day")
        else None,
        "trade_stats": rollups.get("trade_stats"),
        "reject_counts": reject_counts,
    }
    summary_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("Saved %s", summary_path)


if __name__ == "__main__":
    main()
