"""CLI entrypoint for the event-driven backtester. Reads candles from the
SQLite store (populated by pull_data.py), runs the backtest, and prints a
performance report. Restrict --start/--end to evaluate two halves of
history independently.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pandas as pd

from src.backtest.engine import BacktestEngine, _SymbolData
from src.backtest.report import print_report_from_frames
from src.config import load_settings
from src.data.store import Store


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="forex_bot.db", help="SQLite database populated by pull_data.py")
    parser.add_argument("--log-dir", default="logs/backtest")
    parser.add_argument("--start", type=parse_date, default=None)
    parser.add_argument("--end", type=parse_date, default=None)
    parser.add_argument(
        "--equity", type=float, default=None, help="override backtest_start_equity from settings.yaml"
    )
    args = parser.parse_args()

    settings = load_settings()
    store = Store(args.db)

    data: dict[str, _SymbolData] = {}
    for symbol in settings.pairs:
        h1 = store.read_candles(symbol, "H1", start=args.start, end=args.end)
        m15 = store.read_candles(symbol, "M15", start=args.start, end=args.end)
        if h1.empty or m15.empty:
            store.close()
            raise SystemExit(f"No candles found for {symbol} in range — run pull_data.py first.")
        data[symbol] = _SymbolData(h1=h1, m15=m15)
    store.close()

    start_equity = args.equity if args.equity is not None else settings.backtest_start_equity
    engine = BacktestEngine(data=data, params=settings, log_dir=args.log_dir, start_equity=start_equity)
    engine.run()

    trades = pd.DataFrame(engine.closed_trades)
    equity_curve = pd.DataFrame(engine.equity_history)

    print_report_from_frames(trades, equity_curve, args.log_dir, args.start, args.end)


if __name__ == "__main__":
    main()
