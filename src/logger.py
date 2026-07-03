"""JSONL structured logging: one file per log type per UTC day, 90-day
retention. The exact same schema/writer is used by the backtester and the
live bot so logs can be diffed/diagnosed the same way regardless of source.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

RETENTION_DAYS = 90


class JsonlLogger:
    def __init__(self, log_dir: str | Path, prefix: str):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._prefix = prefix
        self._purge_old()

    def _path_for(self, ts: datetime) -> Path:
        return self._dir / f"{self._prefix}-{ts.strftime('%Y-%m-%d')}.jsonl"

    def write(self, record: dict[str, Any], ts: datetime | None = None) -> None:
        ts = ts or datetime.now(timezone.utc)
        with open(self._path_for(ts), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def _purge_old(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        for path in self._dir.glob(f"{self._prefix}-*.jsonl"):
            date_str = path.stem[len(self._prefix) + 1 :]
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if file_date < cutoff:
                path.unlink(missing_ok=True)


class DecisionLogger(JsonlLogger):
    def __init__(self, log_dir: str | Path):
        super().__init__(log_dir, "decisions")


class TradeLogger(JsonlLogger):
    def __init__(self, log_dir: str | Path):
        super().__init__(log_dir, "trades")


class EquityLogger(JsonlLogger):
    def __init__(self, log_dir: str | Path):
        super().__init__(log_dir, "equity")


def console_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
