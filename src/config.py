"""Configuration loading: static strategy/risk params from config/settings.yaml,
secrets (MT5 credentials, Telegram token) from environment / .env.

Research flags (e.g. KILL_SWITCH_ENABLED) may be overridden from the environment
so backtests can run without the drawdown latch. Default remains enabled.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_YAML_PATH = REPO_ROOT / "config" / "settings.yaml"

# override=True so .env wins over a stale shell export of KILL_SWITCH_ENABLED.
load_dotenv(REPO_ROOT / ".env", override=True)



def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")




class Settings(BaseSettings):
    """Strategy/risk parameters. Immutable at runtime — edit the YAML and restart."""

    model_config = SettingsConfigDict(frozen=True)

    pairs: list[str]
    strategy: str = "trend_pullback"

    risk_per_trade: float
    max_open_trades: int
    max_per_symbol: int
    max_same_currency_bets: int = 1  # 1 = old binary correlation block
    daily_loss_limit: float
    max_drawdown_kill: float
    cooldown_after_loss_min: int

    session_utc: list[str]

    max_spread_pips: dict[str, float]

    trend_ema: int
    pullback_ema: int
    rsi_period: int
    atr_period: int
    atr_sl_mult: float
    tp_r_multiple: float
    spread_buffer_pips: float

    pullback_lookback: int
    pullback_expiry: int
    swing_lookback: int
    h1_slope_lookback: int

    # breakout_trend
    breakout_lookback: int = 20
    min_trend_atr_frac: float = 0.15
    require_impulse_candle: bool = True
    breakout_rsi_long_max: float = 70.0
    breakout_rsi_short_min: float = 30.0

    # h4_trend (Turtle-adapted Donchian on H4 resampled from H1)
    h4_breakout_lookback: int = 20
    h4_trend_ema: int = 50
    h4_slope_lookback: int = 3
    h4_atr_period: int = 14
    h4_atr_sl_mult: float = 2.0
    h4_trail_atr_mult: float = 3.0
    h4_tp_r_cap: float = 8.0
    h4_breakeven_after_atr: float = 2.0
    h4_min_slope_atr_frac: float = 0.0
    h4_min_atr_percentile: float = 0.0
    h4_atr_percentile_lookback: int = 126

    # h4_pullback — dip/rally entries in H4 trends, fixed TP
    h4_pullback_ema: int = 20
    h4_pullback_lookback: int = 12
    h4_pullback_expiry: int = 4
    h4_pullback_extension_atr: float = 1.0
    h4_pullback_tp_r: float = 2.0
    h4_pullback_tp_cap: float = 8.0
    h4_pullback_runners: bool = True  # far TP + trail exit (vs fixed tp_r)
    h4_trail_start_atr: float = 1.0
    h4_trail_min_lock_r: float = 1.25  # min profit (in R) once runner trail is active
    h4_max_sl_atr: float = 1.5  # cap initial stop width on h4_pullback
    h4_swing_lookback: int = 8
    h4_pullback_max_age: int = 2

    backtest_start_equity: float

    kill_switch_enabled: bool = True

    @classmethod
    def load(cls, path: Path = SETTINGS_YAML_PATH) -> "Settings":
        with open(path, "r") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
        # .env / process env overrides yaml (research). Default remains enabled.
        ks = _env_bool("KILL_SWITCH_ENABLED")
        if ks is not None:
            raw["kill_switch_enabled"] = ks
        return cls(**raw)



class Secrets(BaseSettings):
    """Runtime secrets pulled from environment / .env. Never committed."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    mt5_host: str = "localhost"
    mt5_port: int = 8001
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def load_settings(path: Path | None = None) -> Settings:
    return Settings.load(path or SETTINGS_YAML_PATH)


def load_secrets() -> Secrets:
    return Secrets()
