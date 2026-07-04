"""CLI entrypoint for the event-driven backtester. Reads candles from the
SQLite store (populated by pull_data.py), runs the backtest, and prints a
performance report. Restrict --start/--end to evaluate two halves of
history independently.

Results are written to ``<log-dir>/summary.json`` so you can re-read them
without re-running. Telegram is notified when credentials are configured.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.backtest.engine import BacktestEngine, _SymbolData
from src.backtest.report import compute_stats, load_reject_reason_counts, print_report_from_frames
from src.config import load_secrets, load_settings
from src.data.store import Store
from src.telegram import TelegramAlerts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_backtest")


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def save_summary(path: Path, stats: dict, reject_counts: dict, start_equity: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "start_equity_cents": start_equity,
        "start_equity_usd": start_equity / 100.0,
        "stats": _json_safe(stats),
        "reject_counts": reject_counts,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Saved summary to %s", path)


def notify_telegram(stats: dict, start_equity: float) -> None:
    secrets = load_secrets()
    alerts = TelegramAlerts(secrets.telegram_bot_token, secrets.telegram_chat_id)
    if stats.get("trade_count", 0) == 0:
        alerts.send("Backtest complete: no closed trades.")
        return

    wins = stats["wins"]
    losses = stats["losses"]
    gross_win = stats["gross_win"]
    gross_loss = stats["gross_loss"]
    net = stats["net_pnl"]
    end_equity = start_equity + net
    dd = stats.get("max_drawdown_pct")
    dd_s = f"{dd:.1%}" if dd is not None else "n/a"

    msg = "\n".join(
        [
            "Forex-bot backtest complete (Exness candles)",
            f"Start equity: ${start_equity / 100:.2f} ({start_equity:.0f} cents)",
            f"Trades: {stats['trade_count']}",
            f"Wins: {wins} ({stats['win_rate']:.1%})",
            f"Losses: {losses} ({1 - stats['win_rate']:.1%})",
            f"Total won: ${gross_win / 100:.2f}",
            f"Total lost: ${gross_loss / 100:.2f}",
            f"Net PnL: ${net / 100:.2f}",
            f"End equity: ${end_equity / 100:.2f}",
            f"Profit factor: {stats['profit_factor']:.2f}",
            f"Max drawdown: {dd_s}",
        ]
    )
    alerts.send(msg)
    logger.info("Telegram summary sent")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="forex_bot.db", help="SQLite database populated by pull_data.py")
    parser.add_argument("--log-dir", default="logs/backtest")
    parser.add_argument("--start", type=parse_date, default=None)
    parser.add_argument("--end", type=parse_date, default=None)
    parser.add_argument(
        "--equity", type=float, default=None, help="override backtest_start_equity from settings.yaml"
    )
    parser.add_argument(
        "--show-summary",
        action="store_true",
        help="print last saved summary.json and exit (no re-run)",
    )
    parser.add_argument("--no-telegram", action="store_true", help="skip Telegram notification")
    args = parser.parse_args()

    summary_path = Path(args.log_dir) / "summary.json"

    if args.show_summary:
        if not summary_path.exists():
            raise SystemExit(f"No cached summary at {summary_path} — run a backtest first.")
        print(summary_path.read_text(encoding="utf-8"))
        return

    settings = load_settings()
    logger.info(
        "kill_switch_enabled=%s (set KILL_SWITCH_ENABLED=false in .env to disable for research)",
        settings.kill_switch_enabled,
    )

    # Fresh decision/trade/equity logs each run so reject counts are not polluted
    # by previous backtests that appended to the same dated JSONL files.
    log_dir = Path(args.log_dir)
    if log_dir.exists():
        for pattern in ("decisions-*.jsonl", "trades-*.jsonl", "equity-*.jsonl"):
            for path in log_dir.glob(pattern):
                path.unlink()

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

    stats = compute_stats(trades, equity_curve)
    reject_counts = load_reject_reason_counts(args.log_dir, args.start, args.end)
    save_summary(summary_path, stats, reject_counts, start_equity)

    if not args.no_telegram:
        notify_telegram(stats, start_equity)


if __name__ == "__main__":
    main()
