"""Event-driven backtester. Iterates closed candles in chronological order
across all configured symbols, calling the exact same strategy/risk_manager
code the live bot uses, and writes the same JSONL log schema (logger.py) so
the two can be diagnosed identically.

Fills are simulated at candle close + configured spread + 0.5 pip slippage.
If both SL and TP are touched within the same candle, SL is assumed to have
been hit first (pessimistic).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.logger import DecisionLogger, EquityLogger, TradeLogger
from src.risk import risk_manager as rm
from src.strategy import trend_pullback as strat

SLIPPAGE_PIPS = 0.5
CONTRACT_SIZE = 100_000
CENTS_PER_UNIT = 100  # account currency is USD-cents
WARMUP_H1_BARS = 300
WARMUP_M15_BARS = 300


def assumed_pip_value_per_lot(symbol: str) -> float:
    """Backtest-only stand-in for symbol_info.trade_tick_value: EURUSD/GBPUSD
    are quoted directly in USD, so 1 pip on 1.0 lot is always
    pip_size * contract_size, converted to account-currency cents. The live
    path (mt5_client.pip_value_per_lot) must never use this — it reads the
    broker's real tick value/size instead.
    """
    pip = strat.pip_size(symbol)
    return pip * CONTRACT_SIZE * CENTS_PER_UNIT


@dataclass
class OpenPosition:
    trade_id: str
    symbol: str
    side: str
    lots: float
    entry: float
    sl: float
    tp: float
    sl_pips: float
    risk_amount: float
    entry_time: pd.Timestamp
    entry_context: dict
    mae_pips: float = 0.0
    mfe_pips: float = 0.0

    def update_excursion(self, high: float, low: float) -> None:
        pip = strat.pip_size(self.symbol)
        if self.side == "LONG":
            adverse = (self.entry - low) / pip
            favorable = (high - self.entry) / pip
        else:
            adverse = (high - self.entry) / pip
            favorable = (self.entry - low) / pip
        self.mae_pips = max(self.mae_pips, adverse)
        self.mfe_pips = max(self.mfe_pips, favorable)


@dataclass
class _SymbolData:
    h1: pd.DataFrame
    m15: pd.DataFrame


class BacktestEngine:
    def __init__(
        self,
        data: dict[str, _SymbolData],
        params: Any,
        log_dir: str,
        start_equity: float,
    ):
        self.data = data
        self.params = params
        self.equity = start_equity
        self.balance = start_equity
        self.hwm = start_equity
        self.day_start_equity = start_equity
        self.current_day: Any = None
        self.kill_switch_triggered = False

        self.open_trades: dict[str, OpenPosition] = {}
        self.last_trade_by_symbol: dict[str, rm.LastTradeInfo] = {}
        self.last_entry_time_by_symbol: dict[str, pd.Timestamp] = {}
        self.closed_trades: list[dict] = []
        self.equity_history: list[dict] = []
        self._trade_seq = 0

        self.decision_log = DecisionLogger(log_dir)
        self.trade_log = TradeLogger(log_dir)
        self.equity_log = EquityLogger(log_dir)
        self._last_equity_log_hour: Any = None

    # -- main loop ---------------------------------------------------------

    def run(self) -> None:
        timeline = self._build_timeline()
        for ts in timeline:
            self._roll_day_if_needed(ts)
            for symbol, idx in self._symbols_at(ts).items():
                self._process_exits(symbol, idx, ts)
                self._process_eval(symbol, idx, ts)
            self._maybe_log_equity(ts)
        self._finalize_open_positions_marker()

    def _build_timeline(self) -> list[pd.Timestamp]:
        all_times: set[pd.Timestamp] = set()
        for sd in self.data.values():
            all_times.update(sd.m15["time"].tolist())
        return sorted(all_times)

    def _symbols_at(self, ts: pd.Timestamp) -> dict[str, int]:
        result = {}
        for symbol, sd in self.data.items():
            matches = sd.m15.index[sd.m15["time"] == ts]
            if len(matches) > 0:
                result[symbol] = int(matches[0])
        return result

    def _roll_day_if_needed(self, ts: pd.Timestamp) -> None:
        day = ts.date()
        if self.current_day is None:
            self.current_day = day
            self.day_start_equity = self.equity
        elif day != self.current_day:
            self.current_day = day
            self.day_start_equity = self.equity

    # -- exits ---------------------------------------------------------------

    def _process_exits(self, symbol: str, idx: int, ts: pd.Timestamp) -> None:
        position = self.open_trades.get(symbol)
        if position is None:
            return

        candle = self.data[symbol].m15.iloc[idx]
        high, low = float(candle["high"]), float(candle["low"])
        position.update_excursion(high, low)

        hit_sl = low <= position.sl if position.side == "LONG" else high >= position.sl
        hit_tp = high >= position.tp if position.side == "LONG" else low <= position.tp

        if not hit_sl and not hit_tp:
            return

        outcome = "SL" if hit_sl else "TP"  # both-touched -> pessimistic SL-first
        exit_price = position.sl if outcome == "SL" else position.tp
        self._close_position(position, ts, exit_price, outcome)

    def _close_position(
        self, position: OpenPosition, ts: pd.Timestamp, exit_price: float, outcome: str
    ) -> None:
        pip = strat.pip_size(position.symbol)
        pip_value = assumed_pip_value_per_lot(position.symbol)
        price_diff = (exit_price - position.entry) if position.side == "LONG" else (position.entry - exit_price)
        pnl = (price_diff / pip) * pip_value * position.lots
        r_result = pnl / position.risk_amount if position.risk_amount > 0 else 0.0
        hold_minutes = (ts - position.entry_time).total_seconds() / 60

        self.balance += pnl
        self.equity = self.balance
        self.hwm = max(self.hwm, self.equity)

        self.last_trade_by_symbol[position.symbol] = rm.LastTradeInfo(closed_at=ts, was_loss=pnl < 0)
        del self.open_trades[position.symbol]

        record = {
            "ts": ts,
            "event": "exit",
            "trade_id": position.trade_id,
            "symbol": position.symbol,
            "side": position.side,
            "lots": position.lots,
            "entry": position.entry,
            "entry_time": int(position.entry_time.timestamp()),
            "sl": position.sl,
            "tp": position.tp,
            "sl_pips": position.sl_pips,
            "risk_amount": position.risk_amount,
            "exit": exit_price,
            "exit_time": int(ts.timestamp()),
            "outcome": outcome,
            "r_result": r_result,
            "pnl": pnl,
            "hold_minutes": hold_minutes,
            "mae_pips": position.mae_pips,
            "mfe_pips": position.mfe_pips,
            "entry_context": position.entry_context,
        }
        self.trade_log.write(record, ts=ts.to_pydatetime())
        self.closed_trades.append(record)
        self._log_equity_snapshot(ts)

    def _open_risk(self) -> float:
        return sum(p.risk_amount for p in self.open_trades.values())

    # -- evaluation / entries -------------------------------------------------

    def _process_eval(self, symbol: str, idx: int, ts: pd.Timestamp) -> None:
        sd = self.data[symbol]
        h1_slice = sd.h1[sd.h1["time"] <= ts].tail(WARMUP_H1_BARS)
        m15_slice = sd.m15.iloc[max(0, idx - WARMUP_M15_BARS + 1) : idx + 1]

        if h1_slice.empty or len(m15_slice) < 2:
            self._log_eval(symbol, ts, strat.Context("NONE", float("nan"), float("nan"), float("nan"),
                                                       float("nan"), float("nan"), float("nan"), float("nan"),
                                                       False, None), None, None, None)
            return

        ctx = strat.compute_context(symbol, h1_slice, m15_slice, self.params)
        signal = strat.evaluate(symbol, h1_slice, m15_slice, self.params)

        verdict = None
        reject_reason = None
        lots = None
        if signal is not None:
            account = self._account_state(symbol, ts)
            verdict = rm.evaluate(signal, account, self.params)
            if isinstance(verdict, rm.Rejected):
                reject_reason = verdict.reason
            else:
                lots = verdict.lots
                self._open_position(signal, verdict, ts)

        self._log_eval(symbol, ts, ctx, signal, verdict, reject_reason, lots)

    def _log_eval(self, symbol, ts, ctx, signal=None, verdict=None, reject_reason=None, lots=None) -> None:
        record = {
            "ts": ts,
            "symbol": symbol,
            "event": "eval",
            "regime": ctx.regime,
            "h1_close": ctx.h1_close,
            "h1_ema50": ctx.h1_ema50,
            "ema50_slope": ctx.ema50_slope,
            "m15_close": ctx.m15_close,
            "m15_ema20": ctx.m15_ema20,
            "rsi": ctx.rsi,
            "atr_pips": ctx.atr_pips,
            "spread_pips": self._assumed_spread_pips(symbol),
            "pullback_active": ctx.pullback_active,
            "pullback_age": ctx.pullback_age,
            "signal": signal.side if signal else None,
            "risk_verdict": "APPROVE" if isinstance(verdict, rm.Approved) else ("REJECT" if verdict else None),
            "reject_reason": reject_reason,
            "lots": lots,
            "open_trades": len(self.open_trades),
            "equity": self.equity,
            "day_pnl": self.equity - self.day_start_equity,
        }
        self.decision_log.write(record, ts=ts.to_pydatetime())

    def _assumed_spread_pips(self, symbol: str) -> float:
        return self.params.max_spread_pips.get(symbol, 0.0)

    def _account_state(self, symbol: str, ts: pd.Timestamp) -> rm.AccountState:
        kill_threshold = self.hwm * (1 - self.params.max_drawdown_kill)
        if not self.kill_switch_triggered and self.equity <= kill_threshold:
            self.kill_switch_triggered = True

        pip_value = assumed_pip_value_per_lot(symbol)
        symbol_info = rm.SymbolInfo(pip_value_per_lot=pip_value, volume_step=0.01, volume_min=0.01)

        return rm.AccountState(
            equity=self.equity,
            balance=self.balance,
            day_start_equity=self.day_start_equity,
            hwm=self.hwm,
            kill_switch_triggered=self.kill_switch_triggered,
            now_utc=ts.to_pydatetime(),
            spread_pips=self._assumed_spread_pips(symbol),
            symbol_info=symbol_info,
            open_trades=[rm.OpenTrade(symbol=p.symbol, side=p.side) for p in self.open_trades.values()],
            last_trade_by_symbol=self.last_trade_by_symbol,
            last_entry_time_by_symbol=self.last_entry_time_by_symbol,
        )

    def _open_position(self, signal: strat.Signal, verdict: rm.Approved, ts: pd.Timestamp) -> None:
        pip = strat.pip_size(signal.symbol)
        slip = (self._assumed_spread_pips(signal.symbol) + SLIPPAGE_PIPS) * pip
        fill = signal.entry + slip if signal.side == "LONG" else signal.entry - slip

        self._trade_seq += 1
        trade_id = f"{signal.symbol}-{ts.strftime('%Y%m%dT%H%M')}-{self._trade_seq}"

        position = OpenPosition(
            trade_id=trade_id,
            symbol=signal.symbol,
            side=signal.side,
            lots=verdict.lots,
            entry=fill,
            sl=signal.sl,
            tp=signal.tp,
            sl_pips=signal.sl_pips,
            risk_amount=verdict.risk_amount,
            entry_time=ts,
            entry_context=vars(signal.context) if signal.context else {},
        )
        self.open_trades[signal.symbol] = position
        self.last_entry_time_by_symbol[signal.symbol] = ts

        self.trade_log.write(
            {
                "ts": ts,
                "event": "entry",
                "trade_id": trade_id,
                "symbol": signal.symbol,
                "side": signal.side,
                "lots": verdict.lots,
                "entry": fill,
                "sl": signal.sl,
                "tp": signal.tp,
                "sl_pips": signal.sl_pips,
                "risk_amount": verdict.risk_amount,
                "entry_context": position.entry_context,
            },
            ts=ts.to_pydatetime(),
        )

    def _maybe_log_equity(self, ts: pd.Timestamp) -> None:
        hour_key = (ts.date(), ts.hour)
        if hour_key == self._last_equity_log_hour:
            return
        self._last_equity_log_hour = hour_key
        self._log_equity_snapshot(ts)

    def _log_equity_snapshot(self, ts: pd.Timestamp) -> None:
        snapshot = {
            "ts": ts,
            "equity": self.equity,
            "balance": self.balance,
            "open_risk": self._open_risk(),
            "dist_to_daily_cap": self.equity - self.day_start_equity * (1 - self.params.daily_loss_limit),
            "dist_to_kill_switch": self.equity - self.hwm * (1 - self.params.max_drawdown_kill),
            "hwm": self.hwm,
        }
        self.equity_log.write(snapshot, ts=ts.to_pydatetime())
        self.equity_history.append(snapshot)

    def _finalize_open_positions_marker(self) -> None:
        # Positions still open at the end of the backtest window are left
        # open in self.open_trades (not force-closed) so callers can inspect
        # them; report.py only summarizes self.closed_trades.
        pass
