"""Strategy package: selectable pure-function strategies for live + backtest."""
from __future__ import annotations

from types import ModuleType


def load_strategy(name: str) -> ModuleType:
    """Return the strategy module implementing compute_context / evaluate / pip_size."""
    key = (name or "trend_pullback").strip().lower()
    if key in ("h4_trend", "donchian", "turtle"):
        from src.strategy import h4_trend as strat
    elif key in ("h4_pullback", "h4pb", "pullback_h4"):
        from src.strategy import h4_pullback as strat
    elif key in ("breakout_trend", "breakout"):
        from src.strategy import breakout_trend as strat
    elif key in ("trend_pullback", "pullback"):
        from src.strategy import trend_pullback as strat
    else:
        raise ValueError(
            f"Unknown strategy: {name!r} (use trend_pullback, breakout_trend, h4_trend, or h4_pullback)"
        )
    return strat
