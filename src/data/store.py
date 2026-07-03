"""SQLite persistence for candles, trades, and equity snapshots. Shared by
pull_data.py, the backtester, and the live bot so both read/write the same
schema.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    tick_volume INTEGER,
    spread INTEGER,
    real_volume INTEGER,
    PRIMARY KEY (symbol, timeframe, time)
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    lots REAL NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    sl_pips REAL,
    risk_amount REAL,
    entry_time INTEGER NOT NULL,
    entry_context TEXT,
    exit_time INTEGER,
    exit_price REAL,
    outcome TEXT,
    r_result REAL,
    pnl REAL,
    hold_minutes REAL,
    mae_pips REAL,
    mfe_pips REAL
);

CREATE TABLE IF NOT EXISTS equity (
    time INTEGER NOT NULL PRIMARY KEY,
    equity REAL NOT NULL,
    balance REAL NOT NULL
);
"""

_CANDLE_COLUMNS = ("open", "high", "low", "close", "tick_volume", "spread", "real_volume")


class Store:
    def __init__(self, path: str | Path):
        self._path = str(path)
        self._conn = sqlite3.connect(self._path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- candles ---------------------------------------------------------

    def upsert_candles(self, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        rows = [
            (
                symbol,
                timeframe,
                int(row.time.timestamp()),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                int(row.tick_volume) if "tick_volume" in df.columns else None,
                int(row.spread) if "spread" in df.columns else None,
                int(row.real_volume) if "real_volume" in df.columns else None,
            )
            for row in df.itertuples()
        ]
        self._conn.executemany(
            """INSERT INTO candles
                 (symbol, timeframe, time, open, high, low, close, tick_volume, spread, real_volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, timeframe, time) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                 tick_volume=excluded.tick_volume, spread=excluded.spread, real_volume=excluded.real_volume""",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def read_candles(
        self,
        symbol: str,
        timeframe: str,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        query = "SELECT * FROM candles WHERE symbol = ? AND timeframe = ?"
        params: list[Any] = [symbol, timeframe]
        if start is not None:
            query += " AND time >= ?"
            params.append(int(start.timestamp()))
        if end is not None:
            query += " AND time <= ?"
            params.append(int(end.timestamp()))
        query += " ORDER BY time ASC"
        df = pd.read_sql_query(query, self._conn, params=params)
        if not df.empty:
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

    def find_gaps(self, symbol: str, timeframe: str, expected_interval_minutes: int) -> list[tuple]:
        """Returns (prev_time, next_time) pairs where consecutive candles are
        further apart than expected, excluding the normal Fri-close/Sun-open
        weekend gap.
        """
        df = self.read_candles(symbol, timeframe)
        if df.empty:
            return []
        gaps = []
        prev_time = df["time"].iloc[0]
        for curr_time in df["time"].iloc[1:]:
            elapsed_min = (curr_time - prev_time).total_seconds() / 60
            if elapsed_min > expected_interval_minutes * 1.5 and not _is_weekend_gap(prev_time, curr_time):
                gaps.append((prev_time, curr_time))
            prev_time = curr_time
        return gaps

    # -- trades ------------------------------------------------------------

    def record_trade_entry(self, trade: dict) -> None:
        record = dict(trade)
        if not isinstance(record.get("entry_context"), (str, type(None))):
            record["entry_context"] = json.dumps(record["entry_context"])
        self._conn.execute(
            """INSERT INTO trades
                 (trade_id, symbol, side, lots, entry, sl, tp, sl_pips, risk_amount,
                  entry_time, entry_context)
               VALUES
                 (:trade_id, :symbol, :side, :lots, :entry, :sl, :tp, :sl_pips, :risk_amount,
                  :entry_time, :entry_context)""",
            record,
        )
        self._conn.commit()

    def record_trade_exit(
        self,
        trade_id: str,
        exit_time: int,
        exit_price: float,
        outcome: str,
        r_result: float,
        pnl: float,
        hold_minutes: float,
        mae_pips: float,
        mfe_pips: float,
    ) -> None:
        self._conn.execute(
            """UPDATE trades SET
                 exit_time=?, exit_price=?, outcome=?, r_result=?, pnl=?,
                 hold_minutes=?, mae_pips=?, mfe_pips=?
               WHERE trade_id=?""",
            (exit_time, exit_price, outcome, r_result, pnl, hold_minutes, mae_pips, mfe_pips, trade_id),
        )
        self._conn.commit()

    def open_trades(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM trades WHERE exit_time IS NULL")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def all_trades(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM trades ORDER BY entry_time ASC", self._conn)

    # -- equity --------------------------------------------------------------

    def record_equity(self, ts: int, equity: float, balance: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO equity (time, equity, balance) VALUES (?, ?, ?)",
            (ts, equity, balance),
        )
        self._conn.commit()

    def equity_curve(self) -> pd.DataFrame:
        df = pd.read_sql_query("SELECT * FROM equity ORDER BY time ASC", self._conn)
        if not df.empty:
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df


def _is_weekend_gap(prev_time: pd.Timestamp, curr_time: pd.Timestamp) -> bool:
    return (
        prev_time.dayofweek == 4  # Friday
        and curr_time.dayofweek in (5, 6, 0)  # Sat, Sun, or Mon
        and (curr_time - prev_time) <= pd.Timedelta(days=3)
    )
