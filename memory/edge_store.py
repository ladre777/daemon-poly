"""
Edge memory — the thing that makes this a learning system instead of a
stateless scanner. Every time the Maker proposes an edge and the Checker
verifies it, we log the reasoning + the market's implied probability at that
moment. Once the market settles, ledger.py writes back the outcome. Scout and
Maker can then query this store to see which market types / reasoning
patterns actually calibrate well versus which ones sound plausible but lose,
and Risk Guardrail can down-weight categories with a poor track record
without you having to hand-tune it every week.

SQLite because this is a single-process bot on Railway with no need for a
separate DB service — swap for Postgres later if DÆMON-IBKR or another
worker needs to read the same memory concurrently.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from typing import Optional

from config import CONFIG

SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    category TEXT,
    source TEXT DEFAULT 'llm',      -- 'llm' | 'quant' — which Maker produced this
    created_at REAL NOT NULL,
    maker_probability REAL,
    maker_reasoning TEXT,
    market_implied_probability REAL,
    edge_size REAL,
    checker_verdict TEXT,          -- "approve" | "reject" | "abstain"
    checker_confidence REAL,
    checker_reasoning TEXT,
    action_taken TEXT,              -- "executed" | "skipped_risk" | "skipped_checker" | "dry_run"
    entry_price REAL,
    size_contracts INTEGER,
    settled INTEGER DEFAULT 0,      -- 0/1
    outcome TEXT,                    -- "yes" | "no" | NULL until settled
    pnl REAL,
    settled_at REAL
);
CREATE INDEX IF NOT EXISTS idx_edges_ticker ON edges(ticker);
CREATE INDEX IF NOT EXISTS idx_edges_category ON edges(category);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_settled ON edges(settled);

-- Persisted so a process restart (Railway redeploys on every push) can't
-- silently clear a tripped kill switch. Single-row table, id is always 1.
CREATE TABLE IF NOT EXISTS bot_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kill_switch_tripped INTEGER DEFAULT 0,
    kill_switch_tripped_at REAL,
    kill_switch_reason TEXT
);
"""


@dataclass
class EdgeRecord:
    ticker: str
    category: Optional[str] = None
    source: str = "llm"
    maker_probability: Optional[float] = None
    maker_reasoning: Optional[str] = None
    market_implied_probability: Optional[float] = None
    edge_size: Optional[float] = None
    checker_verdict: Optional[str] = None
    checker_confidence: Optional[float] = None
    checker_reasoning: Optional[str] = None
    action_taken: Optional[str] = None
    entry_price: Optional[float] = None
    size_contracts: Optional[int] = None


class EdgeStore:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or CONFIG.ledger_db_path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_edge(self, edge: EdgeRecord) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO edges
                (ticker, category, source, created_at, maker_probability, maker_reasoning,
                 market_implied_probability, edge_size, checker_verdict,
                 checker_confidence, checker_reasoning, action_taken,
                 entry_price, size_contracts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    edge.ticker, edge.category, edge.source, time.time(), edge.maker_probability,
                    edge.maker_reasoning, edge.market_implied_probability, edge.edge_size,
                    edge.checker_verdict, edge.checker_confidence, edge.checker_reasoning,
                    edge.action_taken, edge.entry_price, edge.size_contracts,
                ),
            )
            return cur.lastrowid

    def settle(self, edge_id: int, outcome: str, pnl: float):
        with self._conn() as c:
            c.execute(
                "UPDATE edges SET settled=1, outcome=?, pnl=?, settled_at=? WHERE id=?",
                (outcome, pnl, time.time(), edge_id),
            )

    def calibration_by_category(self) -> list[dict]:
        """
        For each (category, source): how often did Maker's stated
        probability track reality, and — more rigorously than PnL alone —
        what's the Brier score (mean squared error between stated
        probability and the 0/1 outcome)? PnL can be good on a lucky trade
        with a bad probability estimate; Brier score can't be gamed that
        way, since it's checking calibration itself, not the coin flip's
        result. A category where Maker says 70%+ but wins ~50% of the time
        will show up here as both a high Brier score and an inflated
        avg_maker_probability vs actual_yes_rate — either signal alone is
        useful, together they're hard to fake.
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT category, source,
                          COUNT(*) as n,
                          AVG(CASE WHEN outcome = 'yes' THEN 1.0 ELSE 0.0 END) as actual_yes_rate,
                          AVG(maker_probability) as avg_maker_probability,
                          AVG((maker_probability - CASE WHEN outcome='yes' THEN 1.0 ELSE 0.0 END)
                              * (maker_probability - CASE WHEN outcome='yes' THEN 1.0 ELSE 0.0 END)
                          ) as brier_score,
                          SUM(pnl) as total_pnl,
                          AVG(pnl) as avg_pnl
                   FROM edges
                   WHERE settled = 1 AND action_taken = 'executed'
                   GROUP BY category, source"""
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_edges(self, ticker: str = None, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            if ticker:
                rows = c.execute(
                    "SELECT * FROM edges WHERE ticker=? ORDER BY created_at DESC LIMIT ?",
                    (ticker, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM edges ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def daily_pnl(self, day_start_ts: float) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(pnl), 0) as pnl FROM edges WHERE settled_at >= ?",
                (day_start_ts,),
            ).fetchone()
            return row["pnl"]

    # -- persisted kill-switch state ---------------------------------------

    def load_kill_switch(self) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT kill_switch_tripped, kill_switch_tripped_at, kill_switch_reason "
                "FROM bot_state WHERE id = 1"
            ).fetchone()
            if not row:
                return {"tripped": False, "tripped_at": None, "reason": None}
            return {
                "tripped": bool(row["kill_switch_tripped"]),
                "tripped_at": row["kill_switch_tripped_at"],
                "reason": row["kill_switch_reason"],
            }

    def set_kill_switch(self, tripped: bool, reason: str = None):
        with self._conn() as c:
            c.execute(
                """INSERT INTO bot_state (id, kill_switch_tripped, kill_switch_tripped_at, kill_switch_reason)
                   VALUES (1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     kill_switch_tripped = excluded.kill_switch_tripped,
                     kill_switch_tripped_at = excluded.kill_switch_tripped_at,
                     kill_switch_reason = excluded.kill_switch_reason""",
                (int(tripped), time.time() if tripped else None, reason),
            )
