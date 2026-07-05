"""Per-symbol strategy + parameter resolution from settings.yaml symbol_profiles."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def resolve_symbol_config(settings: Any, symbol: str) -> tuple[str, SimpleNamespace]:
    """Return (strategy_name, merged_params) for one symbol.

    Global settings are the base. The first ``symbol_profiles`` entry whose
    ``symbols`` list contains *symbol* wins; its keys override the base
    (``max_spread_pips`` dicts are merged, not replaced).
    """
    if hasattr(settings, "model_dump"):
        base: dict[str, Any] = settings.model_dump()
    elif isinstance(settings, dict):
        base = dict(settings)
    else:
        base = vars(settings).copy()

    profiles = base.pop("symbol_profiles", None) or {}
    strategy = str(base.get("strategy", "trend_pullback"))

    for prof in profiles.values():
        syms = prof.get("symbols") or prof.get("pairs") or []
        if symbol not in syms:
            continue
        for key, val in prof.items():
            if key in ("symbols", "pairs", "name"):
                continue
            if key == "strategy":
                strategy = str(val)
            elif key == "max_spread_pips" and isinstance(val, dict):
                merged = dict(base.get("max_spread_pips") or {})
                merged.update(val)
                base["max_spread_pips"] = merged
            else:
                base[key] = val
        break

    base.pop("symbol_profiles", None)
    return strategy, SimpleNamespace(**base)
