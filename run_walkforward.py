"""Walk-forward parameter search for h4_trend.

Grid-searches h4_breakout_lookback, h4_trail_atr_mult, and h4_atr_sl_mult on
the in-sample half of history, validates the winner on out-of-sample, then
runs a full-period backtest with the best combo plus the current risk/filter
settings from settings.yaml.

Usage (on the VPS where forex_bot.db lives):

    KILL_SWITCH_ENABLED=false python run_walkforward.py
    KILL_SWITCH_ENABLED=false python run_walkforward.py --apply-best

``--apply-best`` patches the three tuned keys in config/settings.yaml.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.backtest.engine import BacktestEngine, _SymbolData
from src.backtest.report import compute_stats
from src.config import SETTINGS_YAML_PATH, load_settings
from src.data.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_walkforward")

WARMUP = pd.Timedelta(days=60)

GRID = {
    "h4_breakout_lookback": [15, 20, 25],
    "h4_trail_atr_mult": [2.5, 3.0, 3.5],
    "h4_atr_sl_mult": [1.5, 2.0, 2.5],
}

MIN_IS_TRADES = 15


def _load_data(
    db_path: str,
    pairs: list[str],
    htf: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, _SymbolData]:
    from src.strategy.h4_trend import resample_h4

    store = Store(db_path)
    data: dict[str, _SymbolData] = {}
    try:
        for symbol in pairs:
            higher = store.read_candles(symbol, htf, start=start, end=end)
            if higher.empty and htf == "H4":
                h1_full = store.read_candles(symbol, "H1")
                if not h1_full.empty:
                    h4_full = resample_h4(h1_full)
                    store.upsert_candles(symbol, "H4", h4_full)
                    higher = store.read_candles(symbol, htf, start=start, end=end)
            m15 = store.read_candles(symbol, "M15", start=start, end=end)
            if higher.empty or m15.empty:
                raise SystemExit(
                    f"No {htf}/M15 candles for {symbol} — run pull_data.py first."
                )
            data[symbol] = _SymbolData(h1=higher, m15=m15)
    finally:
        store.close()
    return data


def _date_span(data: dict[str, _SymbolData]) -> tuple[pd.Timestamp, pd.Timestamp]:
    starts, ends = [], []
    for sd in data.values():
        starts.append(sd.m15["time"].min())
        ends.append(sd.m15["time"].max())
    return min(starts), max(ends)


def _run_backtest(
    data: dict[str, _SymbolData],
    params: Any,
    log_dir: str,
    start_equity: float,
) -> dict:
    engine = BacktestEngine(data=data, params=params, log_dir=log_dir, start_equity=start_equity)
    engine.run()
    trades = pd.DataFrame(engine.closed_trades)
    equity = pd.DataFrame(engine.equity_history)
    return compute_stats(trades, equity)


def _score(stats: dict) -> float:
    """Higher is better. Favour profit factor with enough trades, then net PnL."""
    n = stats.get("trade_count", 0)
    if n < MIN_IS_TRADES:
        return float("-inf")
    pf = stats.get("profit_factor", 0.0)
    if pf != pf or pf <= 0:  # NaN or zero
        return float("-inf")
    net = stats.get("net_pnl", 0.0)
    dd = stats.get("max_drawdown_pct")
    dd_penalty = abs(dd) if dd is not None else 0.0
    return pf * 1000.0 + net / 1000.0 - dd_penalty * 500.0


def _grid_combos() -> list[dict[str, float | int]]:
    keys = list(GRID.keys())
    return [dict(zip(keys, vals)) for vals in itertools.product(*(GRID[k] for k in keys))]


def _apply_yaml_patch(path: Path, updates: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8")
    for key, value in updates.items():
        pattern = rf"^({re.escape(key)}:\s*).*"
        repl = rf"\g<1>{value}"
        new_text, n = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
        if n == 0:
            raise SystemExit(f"Key {key!r} not found in {path}")
        text = new_text
    path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="forex_bot.db")
    parser.add_argument("--log-dir", default="logs/walkforward")
    parser.add_argument("--apply-best", action="store_true", help="write winning params to settings.yaml")
    args = parser.parse_args()

    settings = load_settings()
    from src.strategy import load_strategy

    strat = load_strategy(settings.strategy)
    htf = getattr(strat, "HTF", "H1")

    full_data = _load_data(args.db, settings.pairs, htf)
    t0, t1 = _date_span(full_data)
    mid = t0 + (t1 - t0) / 2
    logger.info("History %s → %s, split at %s", t0.date(), t1.date(), mid.date())

    is_start = t0.to_pydatetime().replace(tzinfo=timezone.utc)
    is_end = mid.to_pydatetime().replace(tzinfo=timezone.utc)
    oos_warmup = (mid - WARMUP).to_pydatetime().replace(tzinfo=timezone.utc)
    oos_end = t1.to_pydatetime().replace(tzinfo=timezone.utc)

    is_data = _load_data(args.db, settings.pairs, htf, start=is_start, end=is_end)
    oos_data = _load_data(args.db, settings.pairs, htf, start=oos_warmup, end=oos_end)

    log_root = Path(args.log_dir)
    log_root.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    best_combo: dict | None = None
    best_score = float("-inf")

    combos = _grid_combos()
    logger.info("Grid search: %d combos on in-sample (%s → %s)", len(combos), is_start.date(), is_end.date())

    for i, combo in enumerate(combos):
        params = settings.model_copy(update=combo)
        run_dir = tempfile.mkdtemp(prefix=f"is_{i}_", dir=log_root)
        stats = _run_backtest(is_data, params, run_dir, settings.backtest_start_equity)
        sc = _score(stats)
        row = {**combo, "score": sc, **{f"is_{k}": v for k, v in stats.items()}}
        results.append(row)
        if sc > best_score:
            best_score = sc
            best_combo = combo
        shutil.rmtree(run_dir, ignore_errors=True)

    if best_combo is None or best_score == float("-inf"):
        raise SystemExit("No in-sample combo met MIN_IS_TRADES — widen grid or check data.")

    logger.info("Best in-sample combo: %s (score=%.2f)", best_combo, best_score)

    best_params = settings.model_copy(update=best_combo)
    oos_dir = str(log_root / "oos")
    Path(oos_dir).mkdir(parents=True, exist_ok=True)
    oos_stats = _run_backtest(oos_data, best_params, oos_dir, settings.backtest_start_equity)

    full_dir = str(log_root / "full")
    Path(full_dir).mkdir(parents=True, exist_ok=True)
    full_stats = _run_backtest(full_data, best_params, full_dir, settings.backtest_start_equity)

    best_row = next(r for r in results if all(r[k] == best_combo[k] for k in best_combo))

    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "split_mid": mid.isoformat(),
        "best_params": best_combo,
        "best_is_score": best_score,
        "in_sample": {k.replace("is_", ""): v for k, v in best_row.items() if k.startswith("is_")},
        "out_of_sample": oos_stats,
        "full_period": full_stats,
        "all_combos": results,
    }

    out_path = log_root / "walkforward.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote %s", out_path)

    def _print_stats(label: str, s: dict) -> None:
        net_usd = s.get("net_pnl", 0) / 100.0
        dd = s.get("max_drawdown_pct")
        dd_s = f"{dd:.1%}" if dd is not None else "n/a"
        print(
            f"\n{label}:"
            f"\n  trades={s.get('trade_count', 0)}"
            f"  WR={s.get('win_rate', 0):.1%}"
            f"  PF={s.get('profit_factor', 0):.2f}"
            f"  net=${net_usd:.2f}"
            f"  maxDD={dd_s}"
        )

    print(f"\n=== Walk-forward result ===")
    print(f"Best params: {best_combo}")
    _print_stats("Out-of-sample", oos_stats)
    _print_stats("Full period (with filters + risk tuning)", full_stats)

    if args.apply_best:
        _apply_yaml_patch(SETTINGS_YAML_PATH, best_combo)
        logger.info("Patched %s with best params", SETTINGS_YAML_PATH)


if __name__ == "__main__":
    main()
