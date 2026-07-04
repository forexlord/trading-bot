"""Persisted bot state: open trades, day-start equity, high-water mark,
per-symbol cooldown bookkeeping, and the kill-switch latch.

The kill switch is a one-way latch: once tripped it is written to disk and
stays tripped across restarts. The ONLY way to clear it is to manually edit
(or delete) the state file — there is no in-app command or config flag for
this, by design.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.risk.risk_manager import LastTradeInfo, OpenTrade

DEFAULT_STATE_PATH = Path("state/state.json")


@dataclass
class TradeState:
    trade_id: str
    symbol: str
    side: str
    lots: float
    entry: float
    sl: float
    tp: float
    sl_pips: float
    risk_amount: float
    entry_time: str  # ISO 8601
    entry_context: dict
    initial_sl: float = 0.0
    mae_pips: float = 0.0
    mfe_pips: float = 0.0


@dataclass
class BotState:
    day_start_equity: float
    hwm: float
    kill_switch_triggered: bool = False
    current_day: Optional[str] = None  # ISO date, broker/UTC day
    open_trades: dict[str, TradeState] = field(default_factory=dict)  # keyed by symbol
    last_trade_by_symbol: dict[str, dict] = field(default_factory=dict)  # {closed_at, was_loss}
    last_entry_time_by_symbol: dict[str, str] = field(default_factory=dict)

    def roll_day_if_needed(self, now_utc: datetime, current_equity: float) -> bool:
        day_str = now_utc.date().isoformat()
        if self.current_day == day_str:
            return False
        self.current_day = day_str
        self.day_start_equity = current_equity
        return True

    def update_hwm(self, equity: float) -> None:
        self.hwm = max(self.hwm, equity)

    def maybe_trip_kill_switch(self, equity: float, max_drawdown_kill: float) -> bool:
        if not self.kill_switch_triggered and equity <= self.hwm * (1 - max_drawdown_kill):
            self.kill_switch_triggered = True
        return self.kill_switch_triggered

    def record_trade_close(self, symbol: str, closed_at: datetime, was_loss: bool) -> None:
        self.last_trade_by_symbol[symbol] = {"closed_at": closed_at.isoformat(), "was_loss": was_loss}
        self.open_trades.pop(symbol, None)

    def record_trade_open(self, trade: TradeState) -> None:
        self.open_trades[trade.symbol] = trade
        self.last_entry_time_by_symbol[trade.symbol] = trade.entry_time

    def to_risk_open_trades(self) -> list[OpenTrade]:
        return [OpenTrade(symbol=t.symbol, side=t.side) for t in self.open_trades.values()]

    def to_risk_last_trade_by_symbol(self) -> dict[str, LastTradeInfo]:
        return {
            symbol: LastTradeInfo(closed_at=datetime.fromisoformat(v["closed_at"]), was_loss=v["was_loss"])
            for symbol, v in self.last_trade_by_symbol.items()
        }

    def to_risk_last_entry_time_by_symbol(self) -> dict[str, datetime]:
        return {symbol: datetime.fromisoformat(ts) for symbol, ts in self.last_entry_time_by_symbol.items()}


class StateStore:
    def __init__(self, path: str | Path = DEFAULT_STATE_PATH):
        self._path = Path(path)

    def load(self, default_equity: float) -> BotState:
        if not self._path.exists():
            return BotState(day_start_equity=default_equity, hwm=default_equity)

        raw = json.loads(self._path.read_text(encoding="utf-8"))
        open_trades = {symbol: TradeState(**t) for symbol, t in raw.get("open_trades", {}).items()}
        return BotState(
            day_start_equity=raw["day_start_equity"],
            hwm=raw["hwm"],
            kill_switch_triggered=raw.get("kill_switch_triggered", False),
            current_day=raw.get("current_day"),
            open_trades=open_trades,
            last_trade_by_symbol=raw.get("last_trade_by_symbol", {}),
            last_entry_time_by_symbol=raw.get("last_entry_time_by_symbol", {}),
        )

    def save(self, state: BotState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "day_start_equity": state.day_start_equity,
            "hwm": state.hwm,
            "kill_switch_triggered": state.kill_switch_triggered,
            "current_day": state.current_day,
            "open_trades": {symbol: asdict(t) for symbol, t in state.open_trades.items()},
            "last_trade_by_symbol": state.last_trade_by_symbol,
            "last_entry_time_by_symbol": state.last_entry_time_by_symbol,
        }
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self._path)  # atomic swap, avoids truncated state on crash mid-write
