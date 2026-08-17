"""
Graceful shutdown and concurrent database access.

Both are operational properties that only bite in production. Railway sends
SIGTERM on every redeploy, and the main loop and the settlement reconciler
both write to the same SQLite file — so "works on my machine, single-threaded,
never interrupted" is not evidence of either.

The property under test for shutdown is specifically that a signal does NOT
interrupt work in progress. Being torn down between `place_order` returning
and the fill reaching the ledger turns an orderly redeploy into a position
nobody recorded.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import threading
import time

import pytest

import main
from config import CONFIG
from memory.db import connect, transaction

from tests.conftest import make_candidate
from tests.test_pass_loop import (
    StubChecker,
    StubMaker,
    StubQuantMaker,
    _positions_follow_fills,
)


@pytest.fixture
def restore_handlers():
    """Signal handlers are process-global; put them back afterwards."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


# --------------------------------------------------------------------------
# shutdown
# --------------------------------------------------------------------------

def test_sigterm_sets_the_flag_without_raising(restore_handlers):
    shutdown = main.install_shutdown_handlers()
    assert shutdown["signal"] is None

    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(0.05)

    assert shutdown["signal"] == "SIGTERM"


def test_sigint_is_handled_the_same_way(restore_handlers):
    shutdown = main.install_shutdown_handlers()
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(0.05)
    assert shutdown["signal"] == "SIGINT"


def test_the_first_signal_wins(restore_handlers):
    """A second SIGTERM during shutdown must not restart the countdown."""
    shutdown = main.install_shutdown_handlers()
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(0.05)
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(0.05)
    assert shutdown["signal"] == "SIGINT"


def test_sigterm_during_a_scan_does_not_abort_the_pass(
    restore_handlers, client, order_store, edge_store, account, execution, risk, ledger
):
    """The signal arrives mid-scan. The pass must still complete and trade."""
    CONFIG.risk.dry_run = False
    candidates = [make_candidate()]
    _positions_follow_fills(client, candidates)

    shutdown = main.install_shutdown_handlers()

    class SignallingScout:
        def refresh_quote(self, candidate):
            return True

        def scan(self):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)
            return list(candidates)

    filled = main.run_once(
        SignallingScout(), StubMaker(), StubQuantMaker(), StubChecker(),
        risk, execution, ledger, account,
    )

    assert shutdown["signal"] == "SIGTERM", "the signal must have been delivered"
    assert filled == 1, "an in-flight pass finishes rather than being torn down"
    assert len(client.place_order_calls) == 1


def test_sigterm_between_maker_and_execution_does_not_lose_the_order(
    restore_handlers, client, order_store, edge_store, account, execution, risk, ledger
):
    """The dangerous window: signal after the proposal, before submission."""
    CONFIG.risk.dry_run = False
    candidates = [make_candidate()]
    _positions_follow_fills(client, candidates)

    shutdown = main.install_shutdown_handlers()
    base = StubMaker()

    class SignallingMaker:
        def propose(self, candidate):
            proposal = base.propose(candidate)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)
            return proposal

    filled = main.run_once(
        SignallingScoutFactory(candidates), SignallingMaker(), StubQuantMaker(),
        StubChecker(), risk, execution, ledger, account,
    )

    assert shutdown["signal"] == "SIGTERM"
    assert filled == 1
    # The order was submitted AND recorded — not submitted and then lost.
    assert len(client.place_order_calls) == 1
    submitted = client.place_order_calls[0]["client_order_id"]
    assert order_store.get_order(submitted) is not None, (
        "an order submitted before the signal must still be in the ledger"
    )


class SignallingScoutFactory:
    def __init__(self, candidates):
        self._candidates = candidates

    def refresh_quote(self, candidate):
        return True

    def scan(self):
        return list(self._candidates)


# --------------------------------------------------------------------------
# concurrent database access
# --------------------------------------------------------------------------

def test_two_writers_serialise_instead_of_raising_database_is_locked(tmp_path):
    """The main loop and the settlement reconciler both write.

    Without busy_timeout this raises `sqlite3.OperationalError: database is
    locked` under exactly the interleaving that production produces, and the
    losing writer's data is simply gone.
    """
    db_path = str(tmp_path / "concurrent.db")
    with connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, who TEXT)")
        conn.commit()

    errors: list[BaseException] = []
    started = threading.Barrier(2, timeout=5)

    def writer(who: str, hold: float):
        try:
            started.wait()
            with transaction(db_path) as conn:
                conn.execute("INSERT INTO t (who) VALUES (?)", (who,))
                time.sleep(hold)          # hold the write lock
        except BaseException as e:        # noqa: BLE001 - recorded for assertion
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=("loop", 0.30)),
        threading.Thread(target=writer, args=("reconciler", 0.0)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"concurrent writers must serialise, got {errors!r}"
    with connect(db_path) as conn:
        rows = {row[0] for row in conn.execute("SELECT who FROM t")}
    assert rows == {"loop", "reconciler"}, "neither writer may be silently dropped"


def test_busy_timeout_is_actually_set(tmp_path):
    """The property above depends entirely on this pragma being applied."""
    db_path = str(tmp_path / "pragma.db")
    with connect(db_path) as conn:
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms >= 1000, f"busy_timeout is {timeout_ms}ms — too short to serialise"


def test_wal_mode_is_enabled(tmp_path):
    """WAL lets the reader carry on while a writer holds the lock."""
    db_path = str(tmp_path / "wal.db")
    with connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_a_reader_is_not_blocked_by_an_open_writer(tmp_path):
    """Under WAL, reconciliation reads must not stall behind the write lock."""
    db_path = str(tmp_path / "reader.db")
    with connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, who TEXT)")
        conn.execute("INSERT INTO t (who) VALUES ('before')")
        conn.commit()

    with transaction(db_path) as writer:
        writer.execute("INSERT INTO t (who) VALUES ('during')")
        # Reader opens while the writer still holds its transaction.
        started = time.monotonic()
        with connect(db_path) as reader:
            rows = {r[0] for r in reader.execute("SELECT who FROM t")}
        elapsed = time.monotonic() - started

    assert rows == {"before"}, "a reader must see the committed state, not the open write"
    assert elapsed < 1.0, f"reader blocked for {elapsed:.2f}s behind a writer"


def test_a_locked_database_eventually_raises_rather_than_hanging_forever(tmp_path):
    """Serialising is right; waiting indefinitely is not — a deadlocked bot
    that never logs anything is worse than one that fails loudly."""
    db_path = str(tmp_path / "held.db")
    with connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()

    holder = sqlite3.connect(db_path, timeout=0.1)
    holder.execute("PRAGMA journal_mode = WAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t (id) VALUES (1)")
    try:
        blocked = sqlite3.connect(db_path, timeout=0.2)
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            blocked.execute("BEGIN IMMEDIATE")
            blocked.execute("INSERT INTO t (id) VALUES (2)")
        assert time.monotonic() - started < 5.0, "must give up, not hang forever"
        blocked.close()
    finally:
        holder.rollback()
        holder.close()


# --------------------------------------------------------------------------
# outbound request pacing
# --------------------------------------------------------------------------

def test_requests_are_paced_to_the_configured_floor():
    """A 400-page scan at no floor was ~8 req/s sustained against Kalshi."""
    from core.kalshi_client import KalshiClient

    client = KalshiClient.__new__(KalshiClient)     # no key material needed
    client._pace_lock = threading.Lock()
    client._next_request_at = 0.0

    original = CONFIG.kalshi.min_request_interval_seconds
    CONFIG.kalshi.min_request_interval_seconds = 0.05
    try:
        started = time.monotonic()
        for _ in range(5):
            client._pace()
        elapsed = time.monotonic() - started
    finally:
        CONFIG.kalshi.min_request_interval_seconds = original

    # Five calls, four enforced gaps.
    assert elapsed >= 0.05 * 4 * 0.9, f"pacing not applied ({elapsed:.3f}s)"
    assert elapsed < 1.0, "pacing must not add unbounded delay"


def test_pacing_can_be_disabled():
    from core.kalshi_client import KalshiClient

    client = KalshiClient.__new__(KalshiClient)
    client._pace_lock = threading.Lock()
    client._next_request_at = 0.0

    original = CONFIG.kalshi.min_request_interval_seconds
    CONFIG.kalshi.min_request_interval_seconds = 0
    try:
        started = time.monotonic()
        for _ in range(50):
            client._pace()
        assert time.monotonic() - started < 0.1
    finally:
        CONFIG.kalshi.min_request_interval_seconds = original
