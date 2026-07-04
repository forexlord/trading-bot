"""Event-driven backtester. Iterates closed candles in chronological order
across all configured symbols, calling the exact same strategy/risk_manager
code the live bot uses, and writes the same JSONL log schema (logger.py) so
the two can be diagnosed identically.

Fills are simulated at candle close + configured spread + 0.5 pip slippage.
If both SL and TP are touched within the same candle, SL is assumed to have
been hit first (pessimistic).
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.logger import DecisionLogger, EquityLogger, TradeLogger
from src.risk import risk_manager as rm
from src.strategy import load_strategy
from src.strategy.common import Context, Signal, pip_size

logger = logging.getLogger(__name__)

SLIPPAGE_PIPS = 0.5
CONTRACT_SIZE = 100_000
CENTS_PER_UNIT = 100  # account currency is USD-cents
WARMUP_H1_BARS = 300
WARMUP_M15_BARS = 300


def assumed_pip_value_per_lot(symbol: str, price: float) -> float:
    """Backtest-only stand-in for symbol_info.trade_tick_value, in
    account-currency cents per pip per 1.0 lot.

    - USD-quote pairs (EURUSD, GBPUSD, AUDUSD, NZDUSD): pip * contract.
    - USD-base pairs (USDJPY, USDCAD, USDCHF): pip value is in the quote
      currency; convert to USD by dividing by the pair's own price.
    - Crosses (EURJPY, ...) would need a second pair's price — unsupported
      here; keep them out of backtest configs.

    The live path (mt5_client.pip_value_per_lot) never uses this — it reads
    the broker's real tick value/size instead.
    """
    base = symbol.upper().rstrip("MCI")  # strip Exness cent/etc. suffixes
    pip = pip_size(symbol)
    if base.endswith("USD"):
        return pip * CONTRACT_SIZE * CENTS_PER_UNIT
    if base.startswith("USD"):
        if price <= 0:
            raise ValueError(f"Need a positive price to value {symbol} pips")
        return pip * CONTRACT_SIZE / price * CENTS_PER_UNIT
    raise ValueError(
        f"Backtest pip-value model supports only USD-quote or USD-base pairs, got {symbol}"
    )


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
    initial_sl: float = 0.0  # opening stop; used by runner trail min-R lock
    mae_pips: float = 0.0
    mfe_pips: float = 0.0

    def update_excursion(self, high: float, low: float) -> None:
        pip = pip_size(self.symbol)
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
    h1: pd.DataFrame  # higher-timeframe bars: H1 or H4, per the strategy's HTF
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

        self.strat = load_strategy(getattr(params, "strategy", "trend_pullback"))
        # Higher timeframe the strategy consumes (H1 default; h4_trend uses H4).
        # _SymbolData.h1 carries bars of THIS timeframe — callers must load it.
        self.htf: str = getattr(self.strat, "HTF", "H1")
        self._htf_delta = pd.Timedelta(minutes={"H1": 60, "H4": 240}[self.htf])
        # Strategies that only decide on HTF closes let us skip the strategy
        # call (and decision-log row) on every other M15 bar — for H4 that is
        # 15 of every 16 bars. Uses actual bar times, so any broker HTF
        # alignment works.
        self._decides_on_htf_close = bool(getattr(self.strat, "DECIDES_ON_HTF_CLOSE", False))
        # Single-pass context+signal, if the strategy offers it (halves work).
        self._eval_with_ctx = getattr(self.strat, "evaluate_with_context", None)
        self.decision_log = DecisionLogger(log_dir)
        self.trade_log = TradeLogger(log_dir)
        self.equity_log = EquityLogger(log_dir)
        self._last_equity_log_hour: Any = None
        logger.info("Strategy: %s", getattr(params, "strategy", "trend_pullback"))

        # Pre-index bars so the main loop is O(1) per symbol per timestamp
        # instead of scanning full DataFrames every bar.
        self._m15_by_time: dict[str, dict[Any, int]] = {}
        self._h1_times: dict[str, np.ndarray] = {}
        for symbol, sd in self.data.items():
            self._m15_by_time[symbol] = {t: i for i, t in enumerate(sd.m15["time"].to_numpy())}
            self._h1_times[symbol] = sd.h1["time"].to_numpy()

    # -- main loop ---------------------------------------------------------

    def run(self) -> None:
        timeline = self._build_timeline()
        n = len(timeline)
        logger.info("Backtest timeline: %d M15 timestamps across %d symbols", n, len(self.data))
        try:
            for i, ts in enumerate(timeline):
                self._roll_day_if_needed(ts)
                for symbol, idx in self._symbols_at(ts).items():
                    self._process_exits(symbol, idx, ts)
                    self._process_eval(symbol, idx, ts)
                    self._update_trailing(symbol, idx, ts)
                self._maybe_log_equity(ts)
                if i > 0 and i % 5000 == 0:
                    logger.info("Backtest progress: %d / %d (%.0f%%)", i, n, 100.0 * i / n)
            self._finalize_open_positions_marker()
            logger.info("Backtest complete: %d closed trades", len(self.closed_trades))
        finally:
            self.decision_log.close()
            self.trade_log.close()
            self.equity_log.close()

    def _build_timeline(self) -> list[pd.Timestamp]:
        all_times: set[Any] = set()
        for by_time in self._m15_by_time.values():
            all_times.update(by_time)
        return sorted(all_times)

    def _symbols_at(self, ts: pd.Timestamp) -> dict[str, int]:
        result = {}
        for symbol, by_time in self._m15_by_time.items():
            idx = by_time.get(ts)
            if idx is not None:
                result[symbol] = idx
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
        pip = pip_size(position.symbol)
        pip_value = assumed_pip_value_per_lot(position.symbol, exit_price)
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

    def _update_trailing(self, symbol: str, idx: int, ts: pd.Timestamp) -> None:
        """Apply the strategy's trailing-stop proposal (if it has one) at this
        candle close. Ratchet only: the stop may tighten, never loosen. Runs
        after exits/eval so the new stop applies from the NEXT candle — same
        as live, where the SL modify lands after the bar closes.
        Gated to H1 boundaries: trailing anchors move at most once per H1/H4
        bar, so recomputing on all 4 intra-hour M15 closes is pure waste.
        """
        update_fn = getattr(self.strat, "update_stop", None)
        if update_fn is None or ts.minute != 45:
            return
        position = self.open_trades.get(symbol)
        if position is None:
            return

        h1_slice, m15_slice = self._slices(symbol, idx, ts)
        # HTF-close strategies: the trail anchor/ATR only move on HTF closes.
        if self._decides_on_htf_close and not self._is_htf_decision_bar(h1_slice, ts):
            return
        trail_kwargs: dict = {}
        if "initial_sl" in inspect.signature(update_fn).parameters:
            trail_kwargs["initial_sl"] = position.initial_sl
        proposal = update_fn(
            symbol, position.side, position.entry, position.entry_time,
            position.sl, h1_slice, m15_slice, self.params,
            **trail_kwargs,
        )
        if proposal is None:
            return
        if position.side == "LONG" and proposal > position.sl:
            position.sl = float(proposal)
        elif position.side == "SHORT" and proposal < position.sl:
            position.sl = float(proposal)

    # -- evaluation / entries -------------------------------------------------

    def _slices(self, symbol: str, idx: int, ts: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Data visible at the close of the M15 bar stamped ts ("now" = ts+15min).

        Lookahead guard: a higher-timeframe bar stamped T (duration D) closes
        at T+D, so it is only visible when T+D <= ts+15min, i.e.
        T <= ts+15min-D (H1: ts-45min; H4: ts-3h45min). Including bars up to
        ts (the old behavior) leaked part of the HTF close into the strategy —
        live could never see that.
        """
        sd = self.data[symbol]
        h1_times = self._h1_times[symbol]
        cutoff = ts + pd.Timedelta(minutes=15) - self._htf_delta
        h1_end = int(np.searchsorted(h1_times, cutoff, side="right"))
        h1_start = max(0, h1_end - WARMUP_H1_BARS)
        h1_slice = sd.h1.iloc[h1_start:h1_end]
        m15_slice = sd.m15.iloc[max(0, idx - WARMUP_M15_BARS + 1) : idx + 1]
        return h1_slice, m15_slice

    def _is_htf_decision_bar(self, h1_slice: pd.DataFrame, ts: pd.Timestamp) -> bool:
        """True when the M15 bar stamped ts closes exactly at the close of the
        newest visible HTF bar — the only bars an HTF-close strategy acts on."""
        if h1_slice.empty:
            return False
        last_htf_time = h1_slice["time"].iloc[-1]
        return bool(last_htf_time + self._htf_delta == ts + pd.Timedelta(minutes=15))

    def _process_eval(self, symbol: str, idx: int, ts: pd.Timestamp) -> None:
        h1_slice, m15_slice = self._slices(symbol, idx, ts)

        # HTF-close strategies: nothing can happen off-boundary — skip the
        # strategy call AND the log row (16x fewer of both for H4).
        if self._decides_on_htf_close and not self._is_htf_decision_bar(h1_slice, ts):
            return

        if h1_slice.empty or len(m15_slice) < 2:
            self._log_eval(symbol, ts, Context("NONE", float("nan"), float("nan"), float("nan"),
                                                       float("nan"), float("nan"), float("nan"), float("nan"),
                                                       False, None), None, None, None)
            return

        if self._eval_with_ctx is not None:
            ctx, signal = self._eval_with_ctx(symbol, h1_slice, m15_slice, self.params)
        else:
            ctx = self.strat.compute_context(symbol, h1_slice, m15_slice, self.params)
            signal = self.strat.evaluate(symbol, h1_slice, m15_slice, self.params)

        verdict = None
        reject_reason = None
        lots = None
        if signal is not None:
            account = self._account_state(symbol, ts, price=signal.entry)
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

    def _account_state(self, symbol: str, ts: pd.Timestamp, price: float) -> rm.AccountState:
        if getattr(self.params, "kill_switch_enabled", True):
            kill_threshold = self.hwm * (1 - self.params.max_drawdown_kill)
            if not self.kill_switch_triggered and self.equity <= kill_threshold:
                self.kill_switch_triggered = True
        else:
            self.kill_switch_triggered = False

        pip_value = assumed_pip_value_per_lot(symbol, price)
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

    def _open_position(self, signal: Signal, verdict: rm.Approved, ts: pd.Timestamp) -> None:
        pip = pip_size(signal.symbol)
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
            initial_sl=signal.sl,
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
