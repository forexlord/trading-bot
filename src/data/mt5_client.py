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
import rpyc.utils.classic
from mt5linux import MetaTrader5

logger = logging.getLogger(__name__)

TIMEFRAMES = ("M15", "H1")

# Local MT5 timeframe constants (avoid RPyC netrefs in remote eval strings).
# https://www.mql5.com/en/docs/constants/chartconstants/enum_timeframes
_TF_CONST = {"M15": 15, "H1": 16385}
_TF_MINUTES = {"M15": 15, "H1": 60}
_CHUNK = 3000


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

    def _conn(self) -> Any:
        return getattr(self.raw, "_MetaTrader5__conn")

    def _eval(self, code: str) -> Any:
        return rpyc.utils.classic.obtain(self._conn().eval(code))

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
        symbol = self.ensure_symbol(symbol)
        info = self.raw.symbol_info(symbol)
        if info is None:
            raise MT5ConnectionError(f"symbol_info({symbol}) failed: {self.raw.last_error()}")
        return info._asdict()

    def symbol_info_tick(self, symbol: str) -> dict:
        symbol = self.ensure_symbol(symbol)
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
        # Exness Standard Cent uses EURUSDm / GBPUSDm.
        if symbol[-1:] not in "mci" and "." not in symbol and "_" not in symbol:
            candidates.extend(
                [f"{symbol}m", f"{symbol}c", f"{symbol}.a", f"{symbol}.m", f"{symbol}_i"]
            )
        for name in candidates:
            ok = self._eval(f"mt5.symbol_select({name!r}, True)")
            if ok:
                if name != symbol:
                    logger.info("Using broker symbol %s (configured as %s)", name, symbol)
                return name
        raise MT5ConnectionError(
            f"symbol_select({symbol}) failed: {self.raw.last_error()}. "
            "Open the symbol in Market Watch (right-click → Show All) or set the "
            "exact broker name in config/settings.yaml pairs (e.g. EURUSDm)."
        )

    def _copy_from_pos(self, symbol: str, timeframe: str, start_pos: int, count: int) -> Any:
        tf = _TF_CONST[timeframe]
        return self._eval(
            f"mt5.copy_rates_from_pos({symbol!r}, {tf}, {int(start_pos)}, {int(count)})"
        )

    def copy_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        symbol = self.ensure_symbol(symbol)
        rates = self._copy_from_pos(symbol, timeframe, 0, count)
        return _rates_to_df(rates)

    def copy_rates_range(self, symbol: str, timeframe: str, date_from: datetime, date_to: datetime) -> pd.DataFrame:
        """Fetch bars in [date_from, date_to].

        mt5linux's datetime-based APIs break over RPyC eval. We pull history in
        integer chunks via ``copy_rates_from_pos`` and filter to the range.
        """
        if timeframe not in _TF_CONST:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        symbol = self.ensure_symbol(symbol)
        minutes = _TF_MINUTES[timeframe]
        span_sec = max((date_to - date_from).total_seconds(), 0.0)
        total = int(span_sec / (minutes * 60) * 1.5) + 500
        total = min(max(total, 100), 99_999)

        frames: list[pd.DataFrame] = []
        pos = 0
        while pos < total:
            chunk = min(_CHUNK, total - pos)
            rates = self._copy_from_pos(symbol, timeframe, pos, chunk)
            part = _rates_to_df(rates)
            if part.empty:
                break
            frames.append(part)
            got = len(part)
            pos += got
            if got < chunk:
                break

        if not frames:
            raise MT5ConnectionError(
                f"No rates for {symbol} {timeframe}: {self.raw.last_error()}. "
                "In MT5 VNC: open an M15 chart for this symbol and scroll left "
                "so history downloads, then retry."
            )

        df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["time"]).sort_values("time")
        df = df.reset_index(drop=True)

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


def _rates_to_df(rates: Any) -> pd.DataFrame:
    columns = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df
