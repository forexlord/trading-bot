"""Thin wrapper around mt5linux.MetaTrader5 (RPyC bridge to the MT5 terminal
running in the Linux Docker container). This is the ONLY module (besides
execution/broker.py, which reuses it) allowed to import mt5linux/MetaTrader5 —
everything else in the bot works with plain dicts/DataFrames so it can be
exercised without a live terminal.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import pandas as pd
from mt5linux import MetaTrader5

logger = logging.getLogger(__name__)

TIMEFRAMES = ("M15", "H1")


class MT5ConnectionError(RuntimeError):
    pass


class MT5Client:
    def __init__(self, host: str, port: int, login: int, password: str, server: str):
        self._host = host
        self._port = port
        self._login = login
        self._password = password
        self._server = server
        self._mt5: Optional[MetaTrader5] = None

    def connect(self) -> None:
        self._mt5 = MetaTrader5(host=self._host, port=self._port)
        if not self._mt5.initialize(login=self._login, password=self._password, server=self._server):
            raise MT5ConnectionError(f"initialize() failed: {self._mt5.last_error()}")

        info = self._mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info() failed after initialize(): {self._mt5.last_error()}")
        if info.login != self._login:
            raise MT5ConnectionError(f"Connected to account {info.login}, expected {self._login}")
        logger.info("Connected to MT5 account %s on %s", info.login, self._server)

    def shutdown(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()

    def is_connected(self) -> bool:
        if self._mt5 is None:
            return False
        term = self._mt5.terminal_info()
        return term is not None and bool(term.connected)

    @property
    def raw(self) -> MetaTrader5:
        if self._mt5 is None:
            raise MT5ConnectionError("Not connected — call connect() first")
        return self._mt5

    def account_info(self) -> dict:
        info = self.raw.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info() failed: {self.raw.last_error()}")
        return info._asdict()

    def terminal_info(self) -> dict:
        info = self.raw.terminal_info()
        if info is None:
            raise MT5ConnectionError(f"terminal_info() failed: {self.raw.last_error()}")
        return info._asdict()

    def symbol_info(self, symbol: str) -> dict:
        info = self.raw.symbol_info(symbol)
        if info is None:
            raise MT5ConnectionError(f"symbol_info({symbol}) failed: {self.raw.last_error()}")
        return info._asdict()

    def symbol_info_tick(self, symbol: str) -> dict:
        tick = self.raw.symbol_info_tick(symbol)
        if tick is None:
            raise MT5ConnectionError(f"symbol_info_tick({symbol}) failed: {self.raw.last_error()}")
        return tick._asdict()

    def spread_pips(self, symbol: str, pip_size: float) -> float:
        info = self.symbol_info(symbol)
        return info["spread"] * info["point"] / pip_size

    def pip_value_per_lot(self, symbol: str, pip_size: float) -> float:
        """Account-currency value of a 1-pip move for 1 lot, derived from the
        broker's own tick value/size — never hardcoded.
        """
        info = self.symbol_info(symbol)
        return info["trade_tick_value"] * (pip_size / info["trade_tick_size"])

    def ensure_symbol(self, symbol: str) -> str:
        """Select symbol in Market Watch; try common Exness suffixes if needed."""
        candidates = [symbol]
        if not symbol[-1:].isdigit() and "." not in symbol:
            candidates.extend(
                [f"{symbol}m", f"{symbol}c", f"{symbol}.a", f"{symbol}.m", f"{symbol}_i"]
            )
        for name in candidates:
            if self.raw.symbol_select(name, True):
                if name != symbol:
                    logger.info("Using broker symbol %s (configured as %s)", name, symbol)
                return name
        raise MT5ConnectionError(
            f"symbol_select({symbol}) failed: {self.raw.last_error()}. "
            "Open the symbol in Market Watch (right-click → Show All) or set the "
            "exact broker name in config/settings.yaml pairs."
        )

    def copy_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        symbol = self.ensure_symbol(symbol)
        tf = _resolve_timeframe(self.raw, timeframe)
        rates = self.raw.copy_rates_from_pos(symbol, tf, 0, count)
        return _rates_to_df(rates)

    def copy_rates_range(self, symbol: str, timeframe: str, date_from: datetime, date_to: datetime) -> pd.DataFrame:
        """Fetch bars in [date_from, date_to].

        Avoids mt5linux's broken ``copy_rates_range`` datetime serialization by
        pulling recent bars with ``copy_rates_from_pos`` (ints only) and filtering.
        """
        symbol = self.ensure_symbol(symbol)
        tf = _resolve_timeframe(self.raw, timeframe)
        minutes = {"M15": 15, "H1": 60}[timeframe]
        span_sec = max((date_to - date_from).total_seconds(), 0.0)
        # Extra headroom for weekends/holidays.
        count = int(span_sec / (minutes * 60) * 1.5) + 500
        count = min(max(count, 100), 99_999)

        rates = self.raw.copy_rates_from_pos(symbol, tf, 0, count)
        df = _rates_to_df(rates)
        if df.empty:
            raise MT5ConnectionError(
                f"No rates for {symbol} {timeframe}: {self.raw.last_error()}. "
                "Open a chart for the symbol in MT5 once so history can download."
            )

        start = pd.Timestamp(date_from)
        end = pd.Timestamp(date_to)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        filtered = df[(df["time"] >= start) & (df["time"] <= end)].reset_index(drop=True)
        if filtered.empty:
            raise MT5ConnectionError(
                f"No {symbol} {timeframe} bars inside {start} .. {end} "
                f"(terminal returned {len(df)} bars from "
                f"{df['time'].iloc[0]} to {df['time'].iloc[-1]})."
            )
        return filtered

    def open_positions(self) -> list[dict]:
        positions = self.raw.positions_get()
        if positions is None:
            return []
        return [p._asdict() for p in positions]

    def order_send(self, request: dict) -> dict:
        result = self.raw.order_send(request)
        if result is None:
            raise MT5ConnectionError(f"order_send() failed: {self.raw.last_error()}")
        return result._asdict()


def _resolve_timeframe(mt5: Any, timeframe: str) -> int:
    mapping = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
    if timeframe not in mapping:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return mapping[timeframe]


def _rates_to_df(rates: Any) -> pd.DataFrame:
    columns = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df
