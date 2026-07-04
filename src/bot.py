"""Main bot loop: on each M15 candle close, evaluate every configured pair,
log the decision, and place a trade if the risk manager approves it. Shared
by both --paper and live modes; only the executor (LiveBroker/PaperBroker)
differs, and both drive the exact same strategy/risk_manager code as the
backtester.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from src.config import Settings, load_secrets, load_settings
from src.data.mt5_client import MT5Client
from src.data.store import Store
from src.execution.broker import LiveBroker
from src.execution.paper import PaperBroker
from src.logger import DecisionLogger, EquityLogger, TradeLogger, console_logger
from src.risk import risk_manager as rm
from src.state import BotState, StateStore, TradeState
from src.strategy import load_strategy
from src.strategy.common import Signal
from src.telegram import TelegramAlerts

HISTORY_BARS = 300
HEARTBEAT_EVERY_SECONDS = 3600
CLOSE_BUFFER_SECONDS = 2  # wait a hair past the boundary so the closed candle is available


def next_m15_boundary(after: datetime) -> datetime:
    base = after.replace(second=0, microsecond=0)
    add_minutes = 15 - (base.minute % 15)
    return base + timedelta(minutes=add_minutes)


class Bot:
    def __init__(self, paper: bool, db_path: str = "forex_bot.db", state_path: str = "state/state.json", log_dir: str = "logs"):
        self.settings: Settings = load_settings()
        self.secrets = load_secrets()
        self.strat = load_strategy(getattr(self.settings, "strategy", "trend_pullback"))
        self.logger = console_logger("bot")
        self.paper = paper
        self.logger.info("Strategy: %s", getattr(self.settings, "strategy", "trend_pullback"))

        self.client = MT5Client(
            host=self.secrets.mt5_host,
            port=self.secrets.mt5_port,
            login=self.secrets.mt5_login,
            password=self.secrets.mt5_password,
            server=self.secrets.mt5_server,
        )
        self.store = Store(db_path)
        self.state_store = StateStore(state_path)
        self.telegram = TelegramAlerts(self.secrets.telegram_bot_token, self.secrets.telegram_chat_id)

        self.decision_log = DecisionLogger(log_dir)
        self.trade_log = TradeLogger(log_dir)
        self.equity_log = EquityLogger(log_dir)

        self.broker = None
        self.state: Optional[BotState] = None
        self._disconnected_alerted = False
        self._kill_switch_alerted = False
        self._daily_cap_alerted_day: Optional[str] = None
        self._last_heartbeat = 0.0

    def start(self) -> None:
        self.client.connect()
        account = self.client.account_info()
        self.logger.info(
            "Connected: login=%s balance=%s equity=%s", account["login"], account["balance"], account["equity"]
        )

        self.broker = PaperBroker(self.client) if self.paper else LiveBroker(self.client)
        self.state = self.state_store.load(default_equity=account["equity"])
        self._kill_switch_alerted = self.state.kill_switch_triggered
        self._reconcile()

    def _reconcile(self) -> None:
        """Never assume local state is truth after a restart: read MT5's own
        open positions and reconcile against state.json.
        """
        live_positions = self.client.open_positions()
        live_by_symbol = {p["symbol"]: p for p in live_positions}

        for symbol in set(self.state.open_trades) - set(live_by_symbol):
            self.logger.warning(
                "state.json had an open trade on %s that MT5 no longer reports; dropping it locally.", symbol
            )
            self.state.open_trades.pop(symbol, None)

        mt5 = self.client.raw
        for symbol, pos in live_by_symbol.items():
            if symbol in self.state.open_trades:
                continue
            self.logger.warning(
                "MT5 reports an open position on %s that local state didn't know about; adopting it.", symbol
            )
            side = "LONG" if pos["type"] == mt5.ORDER_TYPE_BUY else "SHORT"
            self.state.open_trades[symbol] = TradeState(
                trade_id=str(pos["ticket"]),
                symbol=symbol,
                side=side,
                lots=pos["volume"],
                entry=pos["price_open"],
                sl=pos["sl"],
                tp=pos["tp"],
                sl_pips=0.0,
                risk_amount=0.0,
                entry_time=datetime.fromtimestamp(pos["time"], tz=timezone.utc).isoformat(),
                entry_context={},
            )
        self.state_store.save(self.state)

    def run_forever(self) -> None:
        self.start()
        while True:
            now = datetime.now(timezone.utc)
            boundary = next_m15_boundary(now)
            sleep_seconds = max((boundary - now).total_seconds(), 0) + CLOSE_BUFFER_SECONDS
            time.sleep(sleep_seconds)
            try:
                self._tick()
            except Exception:
                self.logger.exception("Unhandled error during tick — continuing next cycle")

    def _tick(self) -> None:
        if not self.client.is_connected():
            if not self._disconnected_alerted:
                self.telegram.terminal_disconnected()
                self._disconnected_alerted = True
            self.logger.error("MT5 terminal disconnected — skipping this cycle, no new entries.")
            return
        self._disconnected_alerted = False

        account = self.client.account_info()
        equity, balance = account["equity"], account["balance"]
        now = datetime.now(timezone.utc)

        if self.state.roll_day_if_needed(now, equity):
            self._daily_cap_alerted_day = None
        self.state.update_hwm(equity)

        if self.settings.kill_switch_enabled:
            was_tripped = self.state.kill_switch_triggered
            now_tripped = self.state.maybe_trip_kill_switch(equity, self.settings.max_drawdown_kill)
            if now_tripped and not was_tripped:
                self.telegram.kill_switch_triggered(equity, self.state.hwm)
        else:
            # Research mode: do not latch or alert; risk_manager also skips the check.
            self.state.kill_switch_triggered = False


        daily_threshold = self.state.day_start_equity * (1 - self.settings.daily_loss_limit)
        if equity <= daily_threshold and self._daily_cap_alerted_day != self.state.current_day:
            self.telegram.daily_cap_hit(equity, self.state.day_start_equity)
            self._daily_cap_alerted_day = self.state.current_day

        self._check_exits(now)
        for symbol in self.settings.pairs:
            self._evaluate_symbol(symbol, now, equity, balance)

        self.state_store.save(self.state)
        self._maybe_heartbeat(equity)

    # -- exit polling ---------------------------------------------------------

    def _check_exits(self, now: datetime) -> None:
        if self.paper:
            self._check_exits_paper(now)
            return
        live_by_symbol = {p["symbol"]: p for p in self.client.open_positions()}
        for symbol, trade in list(self.state.open_trades.items()):
            if symbol in live_by_symbol:
                self._update_excursion(symbol, trade)
                self._maybe_trail(symbol, trade, live_by_symbol[symbol])
                continue
            self._finalize_closed_trade(symbol, trade, now)

    def _check_exits_paper(self, now: datetime) -> None:
        """Paper trades have no MT5 positions; simulate SL/TP against the
        current tick instead of positions_get (which would instantly
        'close' every paper trade).
        """
        for symbol, trade in list(self.state.open_trades.items()):
            tick = self.client.symbol_info_tick(symbol)
            price = tick["bid"] if trade.side == "LONG" else tick["ask"]
            if trade.side == "LONG":
                hit_sl, hit_tp = price <= trade.sl, price >= trade.tp
            else:
                hit_sl, hit_tp = price >= trade.sl, price <= trade.tp
            if hit_sl or hit_tp:
                outcome = "SL" if hit_sl else "TP"  # pessimistic if both
                exit_price = trade.sl if hit_sl else trade.tp
                self._finalize_closed_trade(symbol, trade, now, outcome=outcome, exit_price=exit_price)
            else:
                self._update_excursion(symbol, trade)
                self._maybe_trail(symbol, trade, position=None)

    def _maybe_trail(self, symbol: str, trade: TradeState, position: dict | None) -> None:
        """Apply the strategy's trailing-stop proposal (ratchet only). In live
        mode the broker SL is modified too; in paper mode only local state."""
        update_fn = getattr(self.strat, "update_stop", None)
        if update_fn is None:
            return
        h1 = self._closed_rates(symbol, "H1")
        m15 = self._closed_rates(symbol, "M15")
        if h1.empty or m15.empty:
            return
        proposal = update_fn(
            symbol, trade.side, trade.entry, trade.entry_time, trade.sl, h1, m15, self.settings
        )
        if proposal is None:
            return
        tighter = (trade.side == "LONG" and proposal > trade.sl) or (
            trade.side == "SHORT" and proposal < trade.sl
        )
        if not tighter:
            return
        if position is not None:
            result = self.broker.modify_sl(position, float(proposal))
            if not result.success:
                self.logger.error("Trailing SL modify failed for %s: %s", symbol, result.comment)
                return
        trade.sl = float(proposal)
        self.logger.info("Trailed %s %s SL to %.5f", symbol, trade.side, trade.sl)

    def _update_excursion(self, symbol: str, trade: TradeState) -> None:
        tick = self.client.symbol_info_tick(symbol)
        price = tick["bid"] if trade.side == "LONG" else tick["ask"]
        pip = self.strat.pip_size(symbol)
        if trade.side == "LONG":
            trade.mae_pips = max(trade.mae_pips, (trade.entry - price) / pip)
            trade.mfe_pips = max(trade.mfe_pips, (price - trade.entry) / pip)
        else:
            trade.mae_pips = max(trade.mae_pips, (price - trade.entry) / pip)
            trade.mfe_pips = max(trade.mfe_pips, (trade.entry - price) / pip)

    def _finalize_closed_trade(
        self,
        symbol: str,
        trade: TradeState,
        now: datetime,
        outcome: str | None = None,
        exit_price: float | None = None,
    ) -> None:
        pip = self.strat.pip_size(symbol)
        if outcome is None:
            tick = self.client.symbol_info_tick(symbol)
            last_price = tick["bid"] if trade.side == "LONG" else tick["ask"]
            outcome = "TP" if abs(last_price - trade.tp) < abs(last_price - trade.sl) else "SL"
        if exit_price is None:
            exit_price = trade.tp if outcome == "TP" else trade.sl

        pip_value = self.client.pip_value_per_lot(symbol, pip)
        price_diff = (exit_price - trade.entry) if trade.side == "LONG" else (trade.entry - exit_price)
        pnl = (price_diff / pip) * pip_value * trade.lots
        r_result = pnl / trade.risk_amount if trade.risk_amount > 0 else 0.0
        hold_minutes = (now - datetime.fromisoformat(trade.entry_time)).total_seconds() / 60

        record = {
            "ts": now,
            "event": "exit",
            "trade_id": trade.trade_id,
            "symbol": symbol,
            "side": trade.side,
            "entry": trade.entry,
            "sl": trade.sl,
            "tp": trade.tp,
            "exit": exit_price,
            "outcome": outcome,
            "r_result": r_result,
            "pnl": pnl,
            "hold_minutes": hold_minutes,
            "mae_pips": trade.mae_pips,
            "mfe_pips": trade.mfe_pips,
            "entry_context": trade.entry_context,
        }
        self.trade_log.write(record, ts=now)
        self.store.record_trade_exit(
            trade.trade_id, int(now.timestamp()), exit_price, outcome, r_result, pnl,
            hold_minutes, trade.mae_pips, trade.mfe_pips,
        )
        self.state.record_trade_close(symbol, now, was_loss=pnl < 0)
        self.telegram.trade_closed(symbol, trade.side, outcome, r_result, pnl)

    # -- evaluation / entries -------------------------------------------------

    def _closed_rates(self, symbol: str, timeframe: str) -> "pd.DataFrame":
        """Fetch candles and drop the last row: MT5 position 0 is the current
        FORMING bar, and strategies must only ever see closed candles (the
        backtester never sees forming bars — live must match).
        """
        df = self.client.copy_rates(symbol, timeframe, HISTORY_BARS)
        return df.iloc[:-1] if len(df) else df

    def _evaluate_symbol(self, symbol: str, now: datetime, equity: float, balance: float) -> None:
        h1 = self._closed_rates(symbol, "H1")
        m15 = self._closed_rates(symbol, "M15")
        self.store.upsert_candles(symbol, "H1", h1)
        self.store.upsert_candles(symbol, "M15", m15)

        if h1.empty or len(m15) < 2:
            return

        ctx = self.strat.compute_context(symbol, h1, m15, self.settings)
        signal = None
        if symbol not in self.state.open_trades:
            signal = self.strat.evaluate(symbol, h1, m15, self.settings)

        pip = self.strat.pip_size(symbol)
        spread_pips = self.client.spread_pips(symbol, pip)

        verdict = None
        reject_reason = None
        lots = None
        if signal is not None:
            account_state = self._build_account_state(symbol, now, equity, balance, spread_pips)
            verdict = rm.evaluate(signal, account_state, self.settings)
            if isinstance(verdict, rm.Rejected):
                reject_reason = verdict.reason
            else:
                lots = verdict.lots
                self._open_trade(signal, verdict, now)

        self.decision_log.write(
            {
                "ts": now,
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
                "spread_pips": spread_pips,
                "pullback_active": ctx.pullback_active,
                "pullback_age": ctx.pullback_age,
                "signal": signal.side if signal else None,
                "risk_verdict": "APPROVE" if isinstance(verdict, rm.Approved) else ("REJECT" if verdict else None),
                "reject_reason": reject_reason,
                "lots": lots,
                "open_trades": len(self.state.open_trades),
                "equity": equity,
                "day_pnl": equity - self.state.day_start_equity,
            },
            ts=now,
        )

    def _build_account_state(
        self, symbol: str, now: datetime, equity: float, balance: float, spread_pips: float
    ) -> rm.AccountState:
        symbol_info_raw = self.client.symbol_info(symbol)
        pip = self.strat.pip_size(symbol)
        symbol_info = rm.SymbolInfo(
            pip_value_per_lot=self.client.pip_value_per_lot(symbol, pip),
            volume_step=symbol_info_raw["volume_step"],
            volume_min=symbol_info_raw["volume_min"],
        )
        return rm.AccountState(
            equity=equity,
            balance=balance,
            day_start_equity=self.state.day_start_equity,
            hwm=self.state.hwm,
            kill_switch_triggered=self.state.kill_switch_triggered,
            now_utc=now,
            spread_pips=spread_pips,
            symbol_info=symbol_info,
            open_trades=self.state.to_risk_open_trades(),
            last_trade_by_symbol=self.state.to_risk_last_trade_by_symbol(),
            last_entry_time_by_symbol=self.state.to_risk_last_entry_time_by_symbol(),
        )

    def _open_trade(self, signal: Signal, verdict: rm.Approved, now: datetime) -> None:
        fill = self.broker.open_position(signal, verdict)
        if not fill.success:
            self.logger.error("Order failed for %s %s: %s", signal.symbol, signal.side, fill.comment)
            return

        entry_price = fill.price if fill.price is not None else signal.entry
        trade_id = str(fill.order_id) if fill.order_id else f"{signal.symbol}-{now.isoformat()}"
        entry_context = asdict(signal.context) if signal.context else {}

        trade = TradeState(
            trade_id=trade_id,
            symbol=signal.symbol,
            side=signal.side,
            lots=verdict.lots,
            entry=entry_price,
            sl=signal.sl,
            tp=signal.tp,
            sl_pips=signal.sl_pips,
            risk_amount=verdict.risk_amount,
            entry_time=now.isoformat(),
            entry_context=entry_context,
        )
        self.state.record_trade_open(trade)
        self.store.record_trade_entry(
            {
                "trade_id": trade_id,
                "symbol": signal.symbol,
                "side": signal.side,
                "lots": verdict.lots,
                "entry": entry_price,
                "sl": signal.sl,
                "tp": signal.tp,
                "sl_pips": signal.sl_pips,
                "risk_amount": verdict.risk_amount,
                "entry_time": int(now.timestamp()),
                "entry_context": entry_context,
            }
        )
        self.trade_log.write(
            {
                "ts": now,
                "event": "entry",
                "trade_id": trade_id,
                "symbol": signal.symbol,
                "side": signal.side,
                "lots": verdict.lots,
                "entry": entry_price,
                "sl": signal.sl,
                "tp": signal.tp,
                "sl_pips": signal.sl_pips,
                "risk_amount": verdict.risk_amount,
                "entry_context": entry_context,
            },
            ts=now,
        )
        self.telegram.trade_opened(signal.symbol, signal.side, verdict.lots, entry_price, signal.sl, signal.tp)

    def _maybe_heartbeat(self, equity: float) -> None:
        now_ts = time.time()
        if now_ts - self._last_heartbeat < HEARTBEAT_EVERY_SECONDS:
            return
        self._last_heartbeat = now_ts
        self.telegram.heartbeat(equity, len(self.state.open_trades))
        self.equity_log.write(
            {
                "ts": datetime.now(timezone.utc),
                "equity": equity,
                "balance": equity,
                "open_risk": sum(t.risk_amount for t in self.state.open_trades.values()),
                "dist_to_daily_cap": equity - self.state.day_start_equity * (1 - self.settings.daily_loss_limit),
                "dist_to_kill_switch": equity - self.state.hwm * (1 - self.settings.max_drawdown_kill),
                "hwm": self.state.hwm,
            }
        )
