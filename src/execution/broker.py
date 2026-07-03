"""Live execution against MT5. SL and TP are always attached in the SAME
order_send request — never sent naked and modified afterward.
"""
from __future__ import annotations

import logging

from src.data.mt5_client import MT5Client
from src.execution.types import FillResult
from src.risk.risk_manager import Approved
from src.strategy.trend_pullback import Signal

logger = logging.getLogger(__name__)

DEVIATION_POINTS = 20
MAGIC_NUMBER = 990001
COMMENT = "trend_pullback_v1"


class LiveBroker:
    def __init__(self, client: MT5Client):
        self._client = client

    def open_position(self, signal: Signal, approved: Approved) -> FillResult:
        mt5 = self._client.raw
        tick = self._client.symbol_info_tick(signal.symbol)
        price = tick["ask"] if signal.side == "LONG" else tick["bid"]
        order_type = mt5.ORDER_TYPE_BUY if signal.side == "LONG" else mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": signal.symbol,
            "volume": approved.lots,
            "type": order_type,
            "price": price,
            "sl": approved.sl,
            "tp": approved.tp,
            "deviation": DEVIATION_POINTS,
            "magic": MAGIC_NUMBER,
            "comment": COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        return self._send(request, mt5)

    def close_position(self, position: dict) -> FillResult:
        mt5 = self._client.raw
        symbol = position["symbol"]
        tick = self._client.symbol_info_tick(symbol)
        is_buy_position = position["type"] == mt5.ORDER_TYPE_BUY
        price = tick["bid"] if is_buy_position else tick["ask"]
        order_type = mt5.ORDER_TYPE_SELL if is_buy_position else mt5.ORDER_TYPE_BUY

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": position["volume"],
            "type": order_type,
            "position": position["ticket"],
            "price": price,
            "deviation": DEVIATION_POINTS,
            "magic": MAGIC_NUMBER,
            "comment": f"{COMMENT}_close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        return self._send(request, mt5)

    def _send(self, request: dict, mt5) -> FillResult:
        result = self._client.order_send(request)
        success = result.get("retcode") == mt5.TRADE_RETCODE_DONE
        if not success:
            logger.error("order_send failed: %s", result)
        return FillResult(
            success=success,
            order_id=result.get("order"),
            price=result.get("price"),
            retcode=result.get("retcode"),
            comment=str(result.get("comment", "")),
        )
