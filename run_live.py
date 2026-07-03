"""Live (or --paper) entrypoint. Connects to MT5 via mt5linux, verifies the
terminal is connected and the account matches what's configured
(MT5Client.connect()), reconciles local state against MT5's own open
positions (never assume local state is truth after a restart), then runs
the M15 evaluation loop forever. Ctrl-C to stop.
"""
from __future__ import annotations

import argparse

from src.bot import Bot
from src.data.mt5_client import MT5ConnectionError
from src.logger import console_logger

logger = console_logger("run_live")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper", action="store_true", help="simulate fills instead of sending real orders")
    parser.add_argument("--db", default="forex_bot.db")
    parser.add_argument("--state", default="state/state.json")
    parser.add_argument("--log-dir", default="logs")
    args = parser.parse_args()

    bot = Bot(paper=args.paper, db_path=args.db, state_path=args.state, log_dir=args.log_dir)
    logger.info("Starting bot in %s mode", "PAPER" if args.paper else "LIVE")

    try:
        bot.run_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    except MT5ConnectionError:
        logger.exception("Could not establish/verify MT5 connection at startup")
        raise
    finally:
        if bot.client is not None:
            bot.client.shutdown()


if __name__ == "__main__":
    main()
