"""Fetch 2 years of M15+H1 candles for the configured pairs into SQLite,
then report any gaps found. Run this once before backtesting, and re-run
periodically to top up recent history.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone

from src.config import load_secrets, load_settings
from src.data.mt5_client import MT5Client
from src.data.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pull_data")

TIMEFRAMES = ("M15", "H1")
EXPECTED_INTERVAL_MINUTES = {"M15": 15, "H1": 60}
DEFAULT_HISTORY_DAYS = 730


def pull(symbols: list[str], days: int, db_path: str) -> None:
    secrets = load_secrets()

    client = MT5Client(
        host=secrets.mt5_host,
        port=secrets.mt5_port,
        login=secrets.mt5_login,
        password=secrets.mt5_password,
        server=secrets.mt5_server,
    )
    client.connect()

    store = Store(db_path)
    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=days)

    try:
        for symbol in symbols:
            for timeframe in TIMEFRAMES:
                logger.info("Fetching %s %s from %s to %s", symbol, timeframe, date_from, date_to)
                df = client.copy_rates_range(symbol, timeframe, date_from, date_to)
                n = store.upsert_candles(symbol, timeframe, df)
                logger.info("Stored %d %s %s candles", n, symbol, timeframe)

                gaps = store.find_gaps(symbol, timeframe, EXPECTED_INTERVAL_MINUTES[timeframe])
                if gaps:
                    logger.warning("%s %s has %d gap(s):", symbol, timeframe, len(gaps))
                    for prev_time, next_time in gaps:
                        logger.warning("  gap from %s to %s", prev_time, next_time)
                else:
                    logger.info("%s %s has no unexpected gaps", symbol, timeframe)
    finally:
        store.close()
        client.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS)
    parser.add_argument("--db", type=str, default="forex_bot.db")
    args = parser.parse_args()

    settings = load_settings()
    pull(settings.pairs, args.days, args.db)


if __name__ == "__main__":
    main()
