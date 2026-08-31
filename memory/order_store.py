"""
Durable order, fill and settlement records.

This is the state that lets the bot answer "what am I actually exposed to?"
after a restart. Railway redeploys on every push, so anything held only in a
Python variable is gone on the next commit — the previous version tracked
open positions in an ``int`` local to ``main()``, which meant every redeploy
silently reset exposure to zero while the real positions stayed open on
Kalshi.

Uniqueness constraints do real work here:

- ``orders.client_order_id`` PRIMARY KEY — one row per order intent, so a
  retry updates rather than inserts.
- ``orders.exchange_order_id`` UNIQUE — the exchange's identity can only ever
  attach to one local order.
- ``fills.fill_id`` PRIMARY KEY — re-reading the fills endpoint cannot
  double-count quantity.
- ``settlements.settlement_key`` UNIQUE — re-running settlement
  reconciliation cannot double-count PnL.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from config import CONFIG
from core.order_state import Fill, OrderIntent, OrderRecord, OrderState
from memory.db import connect, transaction

log = logging.getLogger("daemon_kalshi.order_store")

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id     TEXT PRIMARY KEY,
    intent_key          TEXT NOT NULL,
    exchange_order_id   TEXT UNIQUE,
    ticker              TEXT NOT NULL,
    event_ticker        TEXT,
    category            TEXT,
    action              TEXT NOT NULL,
    side                TEXT NOT NULL,
    requested_count     INTEGER NOT NULL,
    limit_price_cents   REAL NOT NULL,
    time_in_force       TEXT NOT NULL,
    state               TEXT NOT NULL,
    filled_count        INTEGER NOT NULL DEFAULT 0,
    remaining_count     INTEGER NOT NULL DEFAULT 0,
    cancelled_count     INTEGER NOT NULL DEFAULT 0,
    expired_count       INTEGER NOT NULL DEFAULT 0,
    avg_fill_price_cents REAL,
    fees_cents          REAL NOT NULL DEFAULT 0,
    dry_run             INTEGER NOT NULL DEFAULT 0,
    source              TEXT,
    edge_id             INTEGER,
    created_at          REAL NOT NULL,
    submitted_at        REAL,
    last_reconciled_at  REAL,
    terminal_at         REAL,
    expires_at          REAL,
    close_time          REAL,
    cancel_requested_at REAL,
    last_error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker);
CREATE INDEX IF NOT EXISTS idx_orders_intent ON orders(intent_key);

CREATE TABLE IF NOT EXISTS fills (
    fill_id             TEXT PRIMARY KEY,
    exchange_order_id   TEXT,
    client_order_id     TEXT,
    ticker              TEXT NOT NULL,
    side                TEXT,
    action              TEXT,
    count               INTEGER NOT NULL,
    price_cents         REAL NOT NULL,
    fees_cents          REAL NOT NULL DEFAULT 0,
    created_at          REAL,
    recorded_at         REAL NOT NULL,
    settled             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills(ticker);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(client_order_id);
CREATE INDEX IF NOT EXISTS idx_fills_settled ON fills(settled);

CREATE TABLE IF NOT EXISTS settlements (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_key      TEXT NOT NULL UNIQUE,
    ticker              TEXT NOT NULL,
    market_id           TEXT,
    client_order_id     TEXT,
    exchange_order_id   TEXT,
    fill_id             TEXT,
    side                TEXT,
    action              TEXT,
    fill_count          INTEGER,
    fill_price_cents    REAL,
    fees_cents          REAL,
    settlement_result   TEXT,
    realized_pnl        REAL,
    settled_at          REAL,
    recorded_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlements_ticker ON settlements(ticker);

-- Last known exchange truth, so a restart has something to compare against
-- and can tell "never reconciled" apart from "reconciled a while ago".
-- Alert suppression for a standing signal.
--
-- Separate from `orders` on purpose. An order record answers "did we submit
-- this intent", and its identity deliberately includes an hourly dedupe
-- bucket so an unchanged signal may be retried later in the day. That is
-- correct for submission and wrong for alerting: it re-alerted the same
-- standing trade once an hour, every hour, as though each were news.
--
-- This table answers the different question "have we already told the
-- operator about this signal, and has anything changed since". Its key
-- excludes the time bucket, so the same signal is one row all day.
CREATE TABLE IF NOT EXISTS signal_alerts (
    signal_key        TEXT PRIMARY KEY,
    ticker            TEXT NOT NULL,
    action            TEXT NOT NULL,
    side              TEXT NOT NULL,
    source            TEXT NOT NULL,
    last_alerted_at   REAL NOT NULL,
    last_edge         REAL,
    last_price_cents  REAL,
    alert_count       INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_signal_alerts_ticker ON signal_alerts(ticker);

CREATE TABLE IF NOT EXISTS account_snapshot (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    balance_cents           REAL,
    available_balance_cents REAL,
    limits_json             TEXT,
    positions_json          TEXT,
    reconciled_at           REAL
);
"""


class OrderStore:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or CONFIG.ledger_db_path
        with connect(self.db_path) as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            c.execute(
                "INSERT INTO schema_version (id, version) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
                (SCHEMA_VERSION,),
            )

    @staticmethod
    def _migrate(conn) -> None:
        """Add columns that SCHEMA gained after this database was created.

        CREATE TABLE IF NOT EXISTS silently does nothing on an existing table,
        so a new column never reaches a database that predates it — and the
        queries then fail at runtime on exactly the machine holding the real
        order history. Same reasoning, and same shape, as EdgeStore._migrate.
        """
        have = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        if "close_time" not in have:
            conn.execute("ALTER TABLE orders ADD COLUMN close_time REAL")
        if "cancel_requested_at" not in have:
            conn.execute("ALTER TABLE orders ADD COLUMN cancel_requested_at REAL")

    # -- orders ------------------------------------------------------------

    def record_intent(self, intent: OrderIntent) -> OrderRecord:
        """Persist an order intent *before* it is sent to the exchange.

        Written first so that a crash or timeout during submission always
        leaves a local record carrying the deterministic client order ID —
        that ID is the only way to ask the exchange "did you get this?"
        afterwards.
        """
        now = time.time()
        record = OrderRecord(
            client_order_id=intent.client_order_id(),
            intent_key=intent.intent_key(),
            ticker=intent.ticker,
            action=intent.action,
            side=intent.side,
            requested_count=intent.count,
            limit_price_cents=intent.limit_price_cents,
            time_in_force=intent.time_in_force,
            state=OrderState.INTENT,
            event_ticker=intent.event_ticker,
            category=intent.category,
            source=intent.source,
            edge_id=intent.edge_id,
            remaining_count=intent.count,
            created_at=now,
        )
        with connect(self.db_path) as c:
            c.execute(
                """INSERT INTO orders
                   (client_order_id, intent_key, ticker, event_ticker, category,
                    action, side, requested_count, limit_price_cents, time_in_force,
                    state, remaining_count, source, edge_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(client_order_id) DO NOTHING""",
                (
                    record.client_order_id, record.intent_key, record.ticker,
                    record.event_ticker, record.category, record.action, record.side,
                    record.requested_count, record.limit_price_cents,
                    record.time_in_force, record.state.value, record.remaining_count,
                    record.source, record.edge_id, record.created_at,
                ),
            )
        existing = self.get_order(record.client_order_id)
        return existing or record

    def update_order(self, record: OrderRecord) -> None:
        with connect(self.db_path) as c:
            c.execute(
                """UPDATE orders SET
                     exchange_order_id = ?, state = ?, filled_count = ?,
                     remaining_count = ?, cancelled_count = ?, expired_count = ?,
                     avg_fill_price_cents = ?, fees_cents = ?, dry_run = ?,
                     submitted_at = ?, last_reconciled_at = ?, terminal_at = ?,
                     expires_at = ?, close_time = ?, cancel_requested_at = ?,
                     last_error = ?, edge_id = ?
                   WHERE client_order_id = ?""",
                (
                    record.exchange_order_id, record.state.value, record.filled_count,
                    record.remaining_count, record.cancelled_count, record.expired_count,
                    record.avg_fill_price_cents, record.fees_cents, int(record.dry_run),
                    record.submitted_at, record.last_reconciled_at, record.terminal_at,
                    record.expires_at, record.close_time,
                    record.cancel_requested_at, record.last_error, record.edge_id,
                    record.client_order_id,
                ),
            )

    def get_order(self, client_order_id: str) -> Optional[OrderRecord]:
        with connect(self.db_path) as c:
            row = c.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        return _row_to_order(row) if row else None

    def find_by_intent_key(self, intent_key: str) -> list[OrderRecord]:
        with connect(self.db_path) as c:
            rows = c.execute(
                "SELECT * FROM orders WHERE intent_key = ? ORDER BY created_at DESC",
                (intent_key,),
            ).fetchall()
        return [_row_to_order(r) for r in rows]

    def live_orders(self) -> list[OrderRecord]:
        """Orders the exchange may still act on. Drives pending exposure and
        the "is it safe to trade?" check."""
        states = [
            OrderState.SUBMITTED.value, OrderState.OPEN.value,
            OrderState.PARTIALLY_FILLED.value, OrderState.UNKNOWN.value,
        ]
        placeholders = ",".join("?" * len(states))
        with connect(self.db_path) as c:
            rows = c.execute(
                f"SELECT * FROM orders WHERE state IN ({placeholders})", states
            ).fetchall()
        # An IOC partial fill is terminal even though its state is in the live
        # set, so filter it out here rather than teaching SQL the TIF rules.
        return [o for o in (_row_to_order(r) for r in rows) if o.is_live]

    def unknown_orders(self) -> list[OrderRecord]:
        with connect(self.db_path) as c:
            rows = c.execute(
                "SELECT * FROM orders WHERE state = ?", (OrderState.UNKNOWN.value,)
            ).fetchall()
        return [_row_to_order(r) for r in rows]

    # -- fills -------------------------------------------------------------

    def record_fills(self, fills: list[Fill]) -> int:
        """Insert exchange-confirmed fills, ignoring ones already stored.

        Returns the number of genuinely new fills. ``INSERT OR IGNORE`` on the
        ``fill_id`` primary key is what makes re-polling the fills endpoint
        safe: the same fill read twice does not become two contracts.
        """
        if not fills:
            return 0
        now = time.time()
        new = 0
        with transaction(self.db_path) as c:
            for f in fills:
                cur = c.execute(
                    """INSERT OR IGNORE INTO fills
                       (fill_id, exchange_order_id, client_order_id, ticker, side,
                        action, count, price_cents, fees_cents, created_at, recorded_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f.fill_id, f.exchange_order_id, f.client_order_id, f.ticker,
                        f.side, f.action, f.count, f.price_cents, f.fees_cents,
                        f.created_at, now,
                    ),
                )
                new += cur.rowcount
        return new

    def fills_recorded_since(self, since_epoch: float) -> int:
        """How many fills the bot has recorded since `since_epoch`.

        Keyed on recorded_at (when we learned of the fill) rather than
        created_at (when the exchange made it), because the question this
        answers is "did we do this?" — and a fill we had not yet observed
        cannot explain a balance change we are looking at right now.
        """
        if since_epoch is None:
            return 0
        with connect(self.db_path) as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM fills WHERE recorded_at >= ?",
                (float(since_epoch),),
            ).fetchone()
            return int(row["n"]) if row else 0

    def fills_for_order(self, client_order_id: str) -> list[Fill]:
        with connect(self.db_path) as c:
            rows = c.execute(
                "SELECT * FROM fills WHERE client_order_id = ?", (client_order_id,)
            ).fetchall()
        return [_row_to_fill(r) for r in rows]

    def unsettled_fills(self, ticker: str = None) -> list[dict]:
        with connect(self.db_path) as c:
            if ticker:
                rows = c.execute(
                    "SELECT * FROM fills WHERE settled = 0 AND ticker = ?", (ticker,)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM fills WHERE settled = 0").fetchall()
        return [dict(r) for r in rows]

    def position_from_fills(self) -> dict[tuple[str, str], dict]:
        """Rebuild net position per (ticker, side) from stored fills.

        This is the local reconstruction that startup reconciliation compares
        against the exchange's own position list. Buys add, sells subtract.
        """
        with connect(self.db_path) as c:
            rows = c.execute(
                """SELECT ticker, side,
                          SUM(CASE WHEN action = 'sell' THEN -count ELSE count END) AS qty,
                          SUM(CASE WHEN action = 'sell' THEN 0 ELSE count * price_cents END) AS cost_cents,
                          SUM(CASE WHEN action = 'sell' THEN 0 ELSE count END) AS bought,
                          SUM(fees_cents) AS fees_cents
                     FROM fills GROUP BY ticker, side"""
            ).fetchall()
        out = {}
        for r in rows:
            bought = r["bought"] or 0
            out[(r["ticker"], r["side"])] = {
                "quantity": r["qty"] or 0,
                "avg_price_cents": (r["cost_cents"] / bought) if bought else 0.0,
                "fees_cents": r["fees_cents"] or 0.0,
            }
        return out

    # -- settlements -------------------------------------------------------

    def record_settlement(self, **kw) -> bool:
        """Write one fill-level settlement row and mark that fill settled.

        Both statements share a transaction, and ``settlement_key`` is UNIQUE,
        so running settlement reconciliation twice is a no-op the second time
        rather than a doubled PnL entry. Returns True if this call actually
        wrote a new row.
        """
        now = time.time()
        with transaction(self.db_path) as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO settlements
                   (settlement_key, ticker, market_id, client_order_id,
                    exchange_order_id, fill_id, side, action, fill_count,
                    fill_price_cents, fees_cents, settlement_result, realized_pnl,
                    settled_at, recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    kw["settlement_key"], kw["ticker"], kw.get("market_id"),
                    kw.get("client_order_id"), kw.get("exchange_order_id"),
                    kw.get("fill_id"), kw.get("side"), kw.get("action"),
                    kw.get("fill_count"), kw.get("fill_price_cents"),
                    kw.get("fees_cents"), kw.get("settlement_result"),
                    kw.get("realized_pnl"), kw.get("settled_at"), now,
                ),
            )
            wrote = cur.rowcount > 0
            if wrote and kw.get("fill_id"):
                c.execute(
                    "UPDATE fills SET settled = 1 WHERE fill_id = ?", (kw["fill_id"],)
                )
        return wrote

    def realized_pnl_since(self, since_ts: float) -> float:
        with connect(self.db_path) as c:
            row = c.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM settlements "
                "WHERE settled_at >= ?",
                (since_ts,),
            ).fetchone()
        return float(row["pnl"])

    def settlements_for_ticker(self, ticker: str) -> list[dict]:
        with connect(self.db_path) as c:
            rows = c.execute(
                "SELECT * FROM settlements WHERE ticker = ?", (ticker,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- account snapshot ---------------------------------------------------

    def save_account_snapshot(
        self, balance_cents, available_balance_cents, limits, positions, reconciled_at
    ) -> None:
        with connect(self.db_path) as c:
            c.execute(
                """INSERT INTO account_snapshot
                   (id, balance_cents, available_balance_cents, limits_json,
                    positions_json, reconciled_at)
                   VALUES (1,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     balance_cents = excluded.balance_cents,
                     available_balance_cents = excluded.available_balance_cents,
                     limits_json = excluded.limits_json,
                     positions_json = excluded.positions_json,
                     reconciled_at = excluded.reconciled_at""",
                (
                    balance_cents, available_balance_cents, json.dumps(limits),
                    json.dumps(positions), reconciled_at,
                ),
            )

    def load_account_snapshot(self) -> Optional[dict]:
        with connect(self.db_path) as c:
            row = c.execute("SELECT * FROM account_snapshot WHERE id = 1").fetchone()
        if not row:
            return None
        return {
            "balance_cents": row["balance_cents"],
            "available_balance_cents": row["available_balance_cents"],
            "limits": json.loads(row["limits_json"] or "{}"),
            "positions": json.loads(row["positions_json"] or "[]"),
            "reconciled_at": row["reconciled_at"],
        }


def _opt(row, key):
    """A column that may not exist on this row.

    Rows come from `SELECT *`, so after _migrate every column is present —
    but a caller selecting an explicit subset, or a row read mid-migration on
    another connection, would otherwise raise IndexError deep inside a
    reconciliation loop rather than returning "not known yet".
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _row_to_order(row) -> OrderRecord:
    return OrderRecord(
        client_order_id=row["client_order_id"],
        intent_key=row["intent_key"],
        ticker=row["ticker"],
        action=row["action"],
        side=row["side"],
        requested_count=row["requested_count"],
        limit_price_cents=row["limit_price_cents"],
        time_in_force=row["time_in_force"],
        state=OrderState(row["state"]),
        exchange_order_id=row["exchange_order_id"],
        event_ticker=row["event_ticker"] or "",
        category=row["category"] or "",
        source=row["source"] or "llm",
        edge_id=row["edge_id"],
        filled_count=row["filled_count"],
        remaining_count=row["remaining_count"],
        cancelled_count=row["cancelled_count"],
        expired_count=row["expired_count"],
        avg_fill_price_cents=row["avg_fill_price_cents"],
        fees_cents=row["fees_cents"],
        dry_run=bool(row["dry_run"]),
        created_at=row["created_at"],
        submitted_at=row["submitted_at"],
        last_reconciled_at=row["last_reconciled_at"],
        terminal_at=row["terminal_at"],
        expires_at=row["expires_at"],
        close_time=_opt(row, "close_time"),
        cancel_requested_at=_opt(row, "cancel_requested_at"),
        last_error=row["last_error"],
    )


def _row_to_fill(row) -> Fill:
    return Fill(
        fill_id=row["fill_id"],
        ticker=row["ticker"],
        count=row["count"],
        price_cents=row["price_cents"],
        side=row["side"] or "",
        action=row["action"] or "",
        fees_cents=row["fees_cents"],
        exchange_order_id=row["exchange_order_id"],
        client_order_id=row["client_order_id"],
        created_at=row["created_at"],
    )


# -- signal alert suppression ------------------------------------------------
#
# Appended as module-level helpers rather than OrderStore methods so the
# alerting concern stays visibly separate from order lifecycle: these read and
# write one table and know nothing about orders, fills, or settlement.

def signal_key(ticker: str, action: str, side: str, source: str = "llm") -> str:
    """Identity of a *signal*, deliberately excluding the time bucket.

    This is the difference that fixes the bug. `OrderIntent.intent_key()`
    includes `dedupe_bucket`, so the same standing trade is a new identity
    every hour — correct for "may we submit again", wrong for "is this news".
    """
    return "|".join([ticker, action.lower(), side.lower(), source.lower()])


@dataclass
class AlertDecision:
    """Whether to alert, and the reason — which the alert itself can quote."""

    should_alert: bool
    reason: str
    previous_edge: Optional[float] = None
    previous_price_cents: Optional[float] = None


class SignalAlertStore:
    """Remembers what the operator has already been told about a signal."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or CONFIG.ledger_db_path
        with connect(self.db_path) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def evaluate(self, key: str, edge: Optional[float],
                 price_cents: Optional[float], now: float = None) -> AlertDecision:
        """Decide whether this signal is worth telling the operator about.

        Suppression is never permanent. A signal goes quiet only while it is
        genuinely unchanged, and any of three things brings it back: it is new,
        it moved, or the reminder interval elapsed. A standing trade that is
        silently forgotten is its own failure mode.
        """
        now = time.time() if now is None else now
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT last_alerted_at, last_edge, last_price_cents "
                "FROM signal_alerts WHERE signal_key = ?", (key,)
            ).fetchone()

        if row is None:
            return AlertDecision(True, "new signal")

        last_at, last_edge, last_price = row[0], row[1], row[2]

        if edge is not None and last_edge is not None:
            moved = abs(edge - last_edge)
            if moved >= CONFIG.telegram.alert_edge_move_threshold:
                return AlertDecision(
                    True,
                    f"edge moved {moved * 100:.1f}pp "
                    f"({last_edge * 100:.1f}% -> {edge * 100:.1f}%)",
                    last_edge, last_price,
                )

        if price_cents is not None and last_price is not None:
            moved_c = abs(price_cents - last_price)
            if moved_c >= CONFIG.telegram.alert_price_move_cents:
                return AlertDecision(
                    True,
                    f"price moved {moved_c:.0f}c ({last_price:.0f}c -> {price_cents:.0f}c)",
                    last_edge, last_price,
                )

        age = now - last_at
        ttl = CONFIG.telegram.alert_reminder_seconds
        if ttl > 0 and age >= ttl:
            return AlertDecision(True, f"still standing after {age / 3600:.1f}h",
                                 last_edge, last_price)

        return AlertDecision(
            False,
            f"unchanged, last alerted {age / 60:.0f}m ago",
            last_edge, last_price,
        )

    def record(self, key: str, ticker: str, action: str, side: str, source: str,
               edge: Optional[float], price_cents: Optional[float],
               now: float = None) -> None:
        """Note that the operator has now been told."""
        now = time.time() if now is None else now
        with transaction(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO signal_alerts (
                    signal_key, ticker, action, side, source,
                    last_alerted_at, last_edge, last_price_cents, alert_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(signal_key) DO UPDATE SET
                    last_alerted_at  = excluded.last_alerted_at,
                    last_edge        = excluded.last_edge,
                    last_price_cents = excluded.last_price_cents,
                    alert_count      = signal_alerts.alert_count + 1
                """,
                (key, ticker, action.lower(), side.lower(), source.lower(),
                 now, edge, price_cents),
            )

    def get(self, key: str) -> Optional[dict]:
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM signal_alerts WHERE signal_key = ?", (key,)
            ).fetchone()
        return dict(row) if row else None
