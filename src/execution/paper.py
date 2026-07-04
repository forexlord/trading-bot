"""Paper executor: same interface as LiveBroker, but simulates fills against
the real-time tick feed (spread + 0.5 pip slippage) instead of sending real
orders. Lets the bot forward-test on the live/demo feed without risking
money, using the identical strategy/risk code path as live and backtest.
"""
from __future__ import annotations

from src.data.mt5_client import MT5Client
from src.execution.types import FillResult
from src.risk.risk_manager import Approved
from src.strategy.common import Signal, pip_size

SLIPPAGE_PIPS = 0.5


class PaperBroker:
    def __init__(self, client: MT5Client):
        self._client = client
        self._next_order_id = 1

    def open_position(self, signal: Signal, approved: Approved) -> FillResult:
        tick = self._client.symbol_info_tick(signal.symbol)
        slip = SLIPPAGE_PIPS * pip_size(signal.symbol)
        price = (tick["ask"] + slip) if signal.side == "LONG" else (tick["bid"] - slip)
        return FillResult(success=True, order_id=self._next_id(), price=price, retcode=0, comment="paper fill")

    def close_position(self, position: dict) -> FillResult:
        symbol = position["symbol"]
        tick = self._client.symbol_info_tick(symbol)
        slip = SLIPPAGE_PIPS * pip_size(symbol)
        is_long = position.get("side", "LONG") == "LONG"
        price = (tick["bid"] - slip) if is_long else (tick["ask"] + slip)
        return FillResult(success=True, order_id=self._next_id(), price=price, retcode=0, comment="paper close")

    def _next_id(self) -> int:
        order_id = self._next_order_id
        self._next_order_id += 1
        return order_id
