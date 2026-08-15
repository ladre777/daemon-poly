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

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

BUSY_TIMEOUT_MS = 10_000


def _prepare(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")


#: Where to put the database when the configured path is not usable. Chosen
#: so the bot can still run for paper/demo work rather than refusing to boot.
FALLBACK_DB_PATH = "/tmp/daemon_kalshi.db"


@dataclass
class StorageStatus:
    """Whether the ledger will actually survive a restart.

    This is the difference between the P0 guarantees holding and not holding.
    Every durable-state property — reconstructing exposure after a restart,
    not double-counting settled PnL, a kill switch that cannot un-trip itself
    — depends on this file outliving the process. On Railway it does that
    only if a Volume is mounted; without one the container filesystem is
    thrown away on every redeploy, and redeploys happen on every push.
    """

    configured_path: str
    effective_path: str
    durable: bool
    writable: bool
    reason: str

    @property
    def usable(self) -> bool:
        return self.writable


def storage_status(db_path: str = None) -> StorageStatus:
    """Inspect the configured ledger path without writing anything permanent.

    Durability is decided by whether the path sits inside a mounted volume.
    On Railway, ``RAILWAY_VOLUME_MOUNT_PATH`` is set only when a Volume is
    attached, which makes it a reliable signal; off Railway, any writable
    location on a normal filesystem is treated as durable.
    """
    from config import CONFIG

    configured = db_path or CONFIG.ledger_db_path
    on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_SERVICE_ID"))
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")

    writable, why_not = _probe_writable(configured)

    if not writable:
        return StorageStatus(
            configured_path=configured,
            effective_path=FALLBACK_DB_PATH,
            durable=False,
            writable=_probe_writable(FALLBACK_DB_PATH)[0],
            reason=why_not,
        )

    if on_railway:
        if not mount:
            return StorageStatus(
                configured, configured, durable=False, writable=True,
                reason=(
                    "no Railway Volume is attached to this service, so the "
                    "container filesystem is discarded on every redeploy"
                ),
            )
        if not str(Path(configured).resolve()).startswith(str(Path(mount).resolve())):
            return StorageStatus(
                configured, configured, durable=False, writable=True,
                reason=(
                    f"LEDGER_DB_PATH ({configured}) is outside the mounted "
                    f"volume ({mount}), so it is not persisted"
                ),
            )
        return StorageStatus(configured, configured, durable=True, writable=True,
                             reason=f"stored on the volume mounted at {mount}")

    return StorageStatus(configured, configured, durable=True, writable=True,
                         reason="local filesystem")


def _probe_writable(db_path: str) -> tuple[bool, str]:
    if db_path == ":memory:":
        return True, ""
    parent = Path(db_path).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"cannot create {parent}: {e}"
    probe = parent / ".daemon_kalshi_write_test"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        return False, f"{parent} is not writable: {e}"
    return True, ""


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
