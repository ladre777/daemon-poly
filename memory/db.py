"""
Shared SQLite connection handling for edge memory and the order store.

Both stores live in the same database file so that an order, its fills, its
settlement and the edge that produced it can be joined in one transaction —
reconciliation idempotency depends on that (a settlement write and the
"already settled" guard have to be in the same commit, or a crash between
them double-counts PnL).

Two settings here are load-bearing rather than cosmetic:

- ``busy_timeout``: the main loop and the settlement reconciler both write.
  Without it, the second writer gets an instant "database is locked" instead
  of waiting, and on a trading bot that surfaces as a lost order record.
- ``WAL``: readers don't block the writer, so a long calibration query can't
  stall an order write.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_MS = 10_000


def _prepare(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")


@contextmanager
def connect(db_path: str):
    """Read/short-write connection. Commits on clean exit, rolls back on error.

    ``isolation_level=None`` puts us in autocommit so that the explicit
    ``transaction()`` helper below is the only thing that opens a multi
    statement transaction — implicit half-open transactions are exactly how
    a partial reconciliation write survives a crash.
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    _prepare(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction(db_path: str):
    """Explicit IMMEDIATE transaction for multi-statement writes that must be
    all-or-nothing (order state + fills, settlement + idempotency marker).

    IMMEDIATE takes the write lock up front instead of upgrading mid
    transaction, so two writers serialise on ``busy_timeout`` rather than one
    of them failing with SQLITE_BUSY partway through.
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    _prepare(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
