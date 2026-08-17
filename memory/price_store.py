"""
Durable index observations, so a restart does not reset the volatility clock.

Why this exists
---------------
The quant path refuses to price a market until it has `MIN_VOL_SPAN_SECONDS`
(600) of observations — correctly, since a volatility estimate off a handful
of ticks is noise, and volatility sits in the denominator of every probability
it produces.

But the buffer lived only in memory. Every redeploy set it back to zero, and
production redeploys often. Live logs across a whole session:

    Only 0s of price history for eth (need 600s)   — fresh container
    Only 112s of price history for eth (need 600s)
    Only 424s of price history for eth (need 600s)
    Pass funnel: ... quant 68 (no proposal 68, below edge 0) ...

The 15-minute and hourly crypto families never priced once, in any run,
because no container survived long enough. The gate was never wrong; the data
was being thrown away.

The fix is to keep the observations, not to lower the bar. The same 600
seconds of real ticks are still required — they just survive a restart now.

Staleness
---------
Points older than the volatility lookback are dropped on both save and load.
A container that was down for an hour must not come back and compute a
"600-second span" across a 60-minute hole: `realized_vol` normalises each
return by its own elapsed time, so a single enormous gap would contribute a
near-zero per-second return and drag the estimate down. Reloading only recent
points keeps the estimate honest, and simply means a long outage warms up
from scratch — which is the correct outcome.
"""
from __future__ import annotations

import logging
import time

from config import CONFIG
from memory.db import connect

log = logging.getLogger("daemon_kalshi.price_store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    symbol      TEXT NOT NULL,
    observed_at REAL NOT NULL,
    price       REAL NOT NULL,
    PRIMARY KEY (symbol, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_price_history_symbol
    ON price_history(symbol, observed_at);
"""


class PriceStore:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or CONFIG.ledger_db_path
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        return connect(self.db_path)

    # -- persistence -------------------------------------------------------

    def save(self, symbol: str, points: list[tuple[float, float]],
             max_age_seconds: float = None) -> int:
        """Persist (timestamp, price) points for one symbol.

        Idempotent: the primary key is (symbol, observed_at), so re-saving a
        buffer that overlaps what is already stored inserts only what is new.
        That matters because this is called once per scan pass with the whole
        rolling buffer, not with a delta.
        """
        symbol = (symbol or "").lower()
        if not symbol or not points:
            return 0
        cutoff = time.time() - self._max_age(max_age_seconds)
        fresh = [(symbol, at, price) for at, price in points
                 if at >= cutoff and price > 0]
        if not fresh:
            return 0
        with self._conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO price_history (symbol, observed_at, price) "
                "VALUES (?,?,?)", fresh,
            )
        return len(fresh)

    def load(self, symbol: str, max_age_seconds: float = None
             ) -> list[tuple[float, float]]:
        """Recent points for one symbol, oldest first.

        Anything older than the lookback is left behind rather than returned:
        a long outage should warm up from scratch, not reconstruct a span
        across a hole in the data.
        """
        symbol = (symbol or "").lower()
        if not symbol:
            return []
        cutoff = time.time() - self._max_age(max_age_seconds)
        with self._conn() as c:
            rows = c.execute(
                "SELECT observed_at, price FROM price_history "
                "WHERE symbol=? AND observed_at >= ? ORDER BY observed_at",
                (symbol, cutoff),
            ).fetchall()
        return [(float(r["observed_at"]), float(r["price"])) for r in rows]

    def prune(self, max_age_seconds: float = None) -> int:
        """Drop points past the lookback. Called after save so the table
        stays bounded without a separate job."""
        cutoff = time.time() - self._max_age(max_age_seconds)
        with self._conn() as c:
            cur = c.execute("DELETE FROM price_history WHERE observed_at < ?",
                            (cutoff,))
            return cur.rowcount or 0

    @staticmethod
    def _max_age(explicit: float = None) -> float:
        if explicit is not None:
            return explicit
        # The vol estimate never looks further back than its own lookback, so
        # anything older is dead weight either way.
        return CONFIG.risk.vol_history_retention_seconds
