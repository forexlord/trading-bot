"""Minimal Telegram Bot API client: plain requests POST, no SDK dependency."""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 10


class TelegramAlerts:
    def __init__(self, bot_token: str, chat_id: str):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)

    def send(self, text: str) -> None:
        if not self._enabled:
            logger.debug("Telegram alerts disabled (no token/chat_id configured); message: %s", text)
            return
        url = API_URL.format(token=self._bot_token)
        try:
            resp = requests.post(url, json={"chat_id": self._chat_id, "text": text}, timeout=TIMEOUT_SECONDS)
            if resp.status_code != 200:
                logger.error("Telegram send failed (%s): %s", resp.status_code, resp.text)
        except requests.RequestException:
            logger.exception("Telegram send raised an exception")

    def trade_opened(self, symbol: str, side: str, lots: float, entry: float, sl: float, tp: float) -> None:
        self.send(f"Opened {side} {symbol} {lots} lots @ {entry:.5f} (SL {sl:.5f} / TP {tp:.5f})")

    def trade_closed(self, symbol: str, side: str, outcome: str, r_result: float, pnl: float) -> None:
        self.send(f"Closed {side} {symbol}: {outcome}, R={r_result:.2f}, PnL={pnl:.2f}")

    def daily_cap_hit(self, equity: float, day_start_equity: float) -> None:
        self.send(f"Daily loss cap hit: equity {equity:.2f} vs day-start {day_start_equity:.2f}. No new entries today.")

    def kill_switch_triggered(self, equity: float, hwm: float) -> None:
        self.send(
            f"KILL SWITCH TRIGGERED: equity {equity:.2f} vs HWM {hwm:.2f}. "
            "Trading halted. Manual edit of state/state.json is required to resume."
        )

    def terminal_disconnected(self) -> None:
        self.send("MT5 terminal disconnected. New entries halted until reconnected.")

    def heartbeat(self, equity: float, open_trades: int) -> None:
        self.send(f"Heartbeat OK: equity {equity:.2f}, {open_trades} open trade(s)")
