"""Shared strategy types and helpers. Strategies must stay pure (no MT5 imports)."""
from __future__ import annotations

from dataclasses import dataclass

JPY_PIP_SIZE = 0.01
DEFAULT_PIP_SIZE = 0.0001
# Crypto vs USD: 1 pip = $1 price move (Exness BTCUSD / ETHUSD convention).
CRYPTO_PIP_SIZE = 1.0
# Gold (XAUUSD): 1 pip = 0.1 in price. Pip choice cancels out of PnL and risk
# sizing (both scale by 1/pip and pip); it only sets the unit for spread configs.
GOLD_PIP_SIZE = 0.1


def _is_crypto_base(base: str) -> bool:
    return base.startswith("BTC") or base.startswith("ETH")


def pip_size(symbol: str) -> float:
    # Exness cent symbols end with "m" (EURUSDm); still non-JPY pip size.
    base = symbol.upper().rstrip("MCI")
    if _is_crypto_base(base):
        return CRYPTO_PIP_SIZE
    if base.startswith("XAU"):
        return GOLD_PIP_SIZE
    return JPY_PIP_SIZE if base.endswith("JPY") else DEFAULT_PIP_SIZE


@dataclass
class Context:
    """Indicator/state snapshot for the just-closed M15 candle. Logged every bar.

    Field names are shared across strategies so decision logs stay compatible.
    ``setup_active`` / ``setup_age`` are strategy-specific (pullback or breakout).
    """

    regime: str  # "LONG" | "SHORT" | "NONE"
    h1_close: float
    h1_ema50: float
    ema50_slope: float
    m15_close: float
    m15_ema20: float
    rsi: float
    atr_pips: float
    pullback_active: bool  # generic "setup active" flag (name kept for log compat)
    pullback_age: int | None


@dataclass
class Signal:
    symbol: str
    side: str  # "LONG" | "SHORT"
    entry: float
    sl: float
    tp: float
    sl_pips: float
    context: Context
