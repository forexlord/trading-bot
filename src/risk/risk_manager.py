"""Risk manager: (signal, account_state, params) -> Approved | Rejected.

Pure function of data, zero MT5 imports — shared verbatim by the backtester
and the live bot. Checks run in a fixed order; the first failure rejects.

Kill-switch latching contract: this function treats
`account_state.kill_switch_triggered` as a one-way latch it does not own.
It rejects whenever that flag is already set OR the live drawdown breaches
the threshold this call — but it is the caller's (state.py's) job to
persist the latch the first time the drawdown breach is observed, so that
trading stays disabled even after equity recovers above the threshold and
even across restarts. Only a manual edit of the persisted state clears it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from src.strategy.common import Signal


@dataclass
class OpenTrade:
    symbol: str
    side: str  # "LONG" | "SHORT"


@dataclass
class LastTradeInfo:
    closed_at: datetime
    was_loss: bool


@dataclass
class SymbolInfo:
    pip_value_per_lot: float
    volume_step: float
    volume_min: float


@dataclass
class AccountState:
    equity: float
    balance: float
    day_start_equity: float
    hwm: float
    kill_switch_triggered: bool
    now_utc: datetime
    spread_pips: float  # current spread for signal.symbol
    symbol_info: SymbolInfo  # sizing info for signal.symbol
    open_trades: list[OpenTrade] = field(default_factory=list)
    last_trade_by_symbol: dict[str, LastTradeInfo] = field(default_factory=dict)
    last_entry_time_by_symbol: dict[str, datetime] = field(default_factory=dict)


@dataclass
class Approved:
    lots: float
    entry: float
    sl: float
    tp: float
    risk_amount: float


@dataclass
class Rejected:
    reason: str


Verdict = Approved | Rejected

MIN_REENTRY_MINUTES = 15  # one M15 candle


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _within_session(now_utc: datetime, session_utc: list[str]) -> bool:
    start, end = _parse_hhmm(session_utc[0]), _parse_hhmm(session_utc[1])
    now = now_utc.time()
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end  # overnight session, wraps midnight


def _is_crypto(symbol: str) -> bool:
    base = symbol.upper().rstrip("MCI")
    return base.startswith("BTC") or base.startswith("ETH")


def _currency_bets(symbol: str, side: str) -> dict[str, int]:
    base, quote = symbol[:3], symbol[3:6]
    sign = 1 if side == "LONG" else -1
    bets = {base: sign, quote: -sign}
    # Crypto USD leg is not counted against forex USD concentration.
    if _is_crypto(symbol):
        del bets[quote]
    return bets


def _max_shared_bet_count(signal: Signal, open_trades: list[OpenTrade]) -> int:
    """How many open trades already hold this signal's most-crowded
    currency+direction bet (e.g. short-USD). With a multi-pair USD portfolio
    the cap (max_same_currency_bets) bounds concentration instead of the old
    binary any-overlap block; cap=1 reproduces the old behavior exactly.
    """
    new_bets = _currency_bets(signal.symbol, signal.side)
    worst = 0
    for currency, sign in new_bets.items():
        count = 0
        for trade in open_trades:
            if _currency_bets(trade.symbol, trade.side).get(currency) == sign:
                count += 1
        worst = max(worst, count)
    return worst


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    d_value, d_step = Decimal(str(value)), Decimal(str(step))
    whole_steps = (d_value / d_step).to_integral_value(rounding=ROUND_FLOOR)
    return float(whole_steps * d_step)


def effective_risk_per_trade(balance: float, params: Any) -> float:
    """Return risk fraction for current balance (growth tiers or flat risk_per_trade).

    ``growth_risk_tiers`` is a list of ``{until_equity, risk_per_trade}`` sorted
    by ascending ``until_equity`` (account currency cents). The first tier whose
    ``until_equity`` >= balance wins; above the last tier uses that tier's risk.
    """
    tiers = getattr(params, "growth_risk_tiers", None) or []
    if not tiers:
        return float(params.risk_per_trade)
    ordered = sorted(tiers, key=lambda t: float(t["until_equity"]))
    for tier in ordered:
        if balance <= float(tier["until_equity"]):
            return float(tier["risk_per_trade"])
    return float(ordered[-1]["risk_per_trade"])


def evaluate(signal: Signal, account: AccountState, params: Any) -> Verdict:
    min_eq = float(getattr(params, "min_equity_cents", 0) or 0)
    if account.balance <= 0 or account.equity <= 0:
        return Rejected("insolvent")
    if min_eq > 0 and account.balance < min_eq:
        return Rejected("equity_too_low")

    if getattr(params, "kill_switch_enabled", True):
        kill_threshold = account.hwm * (1 - params.max_drawdown_kill)
        if account.kill_switch_triggered or account.equity <= kill_threshold:
            return Rejected("kill_switch")


    daily_threshold = account.day_start_equity * (1 - params.daily_loss_limit)
    if account.equity <= daily_threshold:
        return Rejected("daily_cap")

    if not _within_session(account.now_utc, params.session_utc):
        return Rejected("session")

    max_spread = params.max_spread_pips.get(signal.symbol)
    if max_spread is None or account.spread_pips > max_spread:
        return Rejected("spread")

    open_total = len(account.open_trades)
    open_this_symbol = sum(1 for t in account.open_trades if t.symbol == signal.symbol)
    if open_total >= params.max_open_trades or open_this_symbol >= params.max_per_symbol:
        return Rejected("max_open")

    max_same = int(getattr(params, "max_same_currency_bets", 1))
    if _max_shared_bet_count(signal, account.open_trades) >= max_same:
        return Rejected("correlation")

    last_trade = account.last_trade_by_symbol.get(signal.symbol)
    if last_trade is not None and last_trade.was_loss:
        elapsed_min = (account.now_utc - last_trade.closed_at).total_seconds() / 60
        if elapsed_min < params.cooldown_after_loss_min:
            return Rejected("cooldown")

    last_entry = account.last_entry_time_by_symbol.get(signal.symbol)
    if last_entry is not None:
        elapsed_min = (account.now_utc - last_entry).total_seconds() / 60
        if elapsed_min < MIN_REENTRY_MINUTES:
            return Rejected("cooldown")

    if signal.sl_pips <= 0:
        return Rejected("invalid_stop")

    risk_pct = effective_risk_per_trade(account.balance, params)
    risk_amount = account.balance * risk_pct
    pip_value_per_lot = account.symbol_info.pip_value_per_lot
    raw_lots = risk_amount / (signal.sl_pips * pip_value_per_lot)
    lots = _floor_to_step(raw_lots, account.symbol_info.volume_step)
    min_lot = account.symbol_info.volume_min

    if lots < min_lot:
        if not bool(getattr(params, "allow_min_lot", False)):
            return Rejected("lot_size_too_small")
        lots = min_lot

    actual_risk = lots * signal.sl_pips * pip_value_per_lot
    if lots == min_lot and bool(getattr(params, "allow_min_lot", False)) and raw_lots < min_lot:
        cap = account.balance * float(getattr(params, "max_risk_when_min_lot", 0.05))
        if actual_risk > cap * 1.05:
            return Rejected("lot_size_too_small")
    elif actual_risk > risk_amount * 1.05:
        return Rejected("risk_exceeds_cap")

    return Approved(lots=lots, entry=signal.entry, sl=signal.sl, tp=signal.tp, risk_amount=actual_risk)
