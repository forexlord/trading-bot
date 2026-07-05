"""Tests for per-symbol config resolution."""
from src.config import Settings
from src.symbol_config import resolve_symbol_config


def _minimal_settings(**overrides) -> Settings:
    base = {
        "pairs": ["EURUSDm", "BTCUSDm"],
        "strategy": "h4_pullback",
        "risk_per_trade": 0.025,
        "max_open_trades": 7,
        "max_per_symbol": 1,
        "max_same_currency_bets": 4,
        "daily_loss_limit": 0.07,
        "max_drawdown_kill": 0.25,
        "cooldown_after_loss_min": 10,
        "session_utc": ["00:00", "23:59"],
        "max_spread_pips": {"EURUSDm": 1.5, "BTCUSDm": 80.0},
        "trend_ema": 50,
        "pullback_ema": 20,
        "rsi_period": 14,
        "atr_period": 14,
        "atr_sl_mult": 1.2,
        "tp_r_multiple": 1.5,
        "spread_buffer_pips": 1.0,
        "pullback_lookback": 20,
        "pullback_expiry": 6,
        "swing_lookback": 10,
        "h1_slope_lookback": 5,
        "backtest_start_equity": 400000,
        "symbol_profiles": {
            "crypto": {
                "symbols": ["BTCUSDm"],
                "max_spread_pips": {"BTCUSDm": 80.0},
            }
        },
    }
    base.update(overrides)
    return Settings(**base)


def test_forex_uses_global_defaults():
    settings = _minimal_settings()
    _, params = resolve_symbol_config(settings, "EURUSDm")
    assert params.risk_per_trade == 0.025
    assert params.h4_trail_atr_mult == 3.0  # Settings default


def test_crypto_profile_overrides():
    settings = _minimal_settings()
    strat, params = resolve_symbol_config(settings, "BTCUSDm")
    assert strat == "h4_pullback"
    assert params.risk_per_trade == 0.025
    assert params.h4_trail_atr_mult == 3.0
    assert params.max_spread_pips["BTCUSDm"] == 80.0
