"""Quotes we would have rested, and what the market did next.

Why this exists
---------------
A resting quote is not a taker fill. It executes when the market comes to you,
which is disproportionately when you are about to be wrong — that is adverse
selection, and it is the single number that decides whether the optimism-tax
quoting edge survives contact with a real book.

It cannot be inferred from a fair value, and it cannot be assumed. A
counterfactual that credits us the spread whenever we quoted would report a
profit on every quote and measure nothing at all.

So this records the quote alongside the market state at the moment it was
generated, and a later pass reads the market again and asks two questions that
have observable answers:

* At t1: did the market trade THROUGH our price? That is the fill.
* At t2, later still: where had the price gone? That is the mark.

The two readings must be separate, and the first version of this was wrong for
exactly that reason. Marking a fill against the same snapshot that established
it is degenerate: filling a resting bid at B requires the market ask to fall to
B or below, and the mid is always below the ask, so the mark is negative by
construction. Every fill would have read as adverse — the metric measuring its
own definition rather than the market. Adverse selection is about where the
price went AFTER the fill, so it needs a later reading.

Nothing here places an order or claims a fill occurred. It records what the
market did, so a fill model can later be measured rather than assumed.
"""
from __future__ import annotations

import time

from config import CONFIG
from memory.db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS quote_observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    zone                TEXT NOT NULL,
    fair_value_cents    REAL NOT NULL,
    bid_cents           REAL NOT NULL,
    ask_cents           REAL NOT NULL,
    bid_size            INTEGER NOT NULL,
    ask_size            INTEGER NOT NULL,
    -- Market at the moment the quote was generated.
    t0                  REAL NOT NULL,
    t0_yes_bid          REAL NOT NULL,
    t0_yes_ask          REAL NOT NULL,
    -- Stage 1, the fill check. NULL means the fill has not been checked yet.
    t1                  REAL,
    t1_yes_bid          REAL,
    t1_yes_ask          REAL,
    filled_bid          INTEGER,
    filled_ask          INTEGER,
    -- Stage 2, the mark, read LATER than t1 and never from the same snapshot.
    -- NULL t2 on a filled row means "filled, mark still pending".
    t2                  REAL,
    t2_yes_bid          REAL,
    t2_yes_ask          REAL,
    -- Mark-to-market of each filled side, in cents. Positive is favourable.
    bid_mark_cents      REAL,
    ask_mark_cents      REAL,
    resolved_at         REAL
);
CREATE INDEX IF NOT EXISTS idx_quote_obs_fill ON quote_observations(t1);
CREATE INDEX IF NOT EXISTS idx_quote_obs_mark ON quote_observations(t2);
CREATE INDEX IF NOT EXISTS idx_quote_obs_ticker ON quote_observations(ticker);
"""


class QuoteStore:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or CONFIG.ledger_db_path
        with connect(self.db_path) as c:
            c.executescript(SCHEMA)

    def record(self, quote, yes_bid: float, yes_ask: float,
               at: float = None) -> int:
        """Persist a generated quote and the book it was generated against."""
        return self.record_many([(quote, yes_bid, yes_ask)], at=at)[0]

    def record_many(self, entries, at: float = None) -> list[int]:
        """Persist a whole pass of quotes in one transaction.

        One pass produces a quote per readable candidate, and a per-row
        connection would put a few hundred commits in the path of the trading
        loop. Measurement must not be able to slow down the thing it measures.
        """
        at = time.time() if at is None else at
        ids: list[int] = []
        with connect(self.db_path) as c:
            for quote, yes_bid, yes_ask in entries:
                cur = c.execute(
                    """INSERT INTO quote_observations
                       (ticker, zone, fair_value_cents, bid_cents, ask_cents,
                        bid_size, ask_size, t0, t0_yes_bid, t0_yes_ask)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (quote.ticker, quote.zone, quote.fair_value_cents,
                     quote.bid_cents, quote.ask_cents, quote.bid_size,
                     quote.ask_size, at, yes_bid, yes_ask),
                )
                ids.append(cur.lastrowid)
        return ids

    def awaiting_fill_check(self, older_than: float,
                            limit: int = 200) -> list[dict]:
        """Quotes recorded before ``older_than`` whose fill is unchecked.

        Bounded by age rather than resolved immediately: a quote read back in
        the same instant it was written has had no chance to be traded
        through, and resolving it would record a fill rate of zero for a
        reason that has nothing to do with the market.
        """
        with connect(self.db_path) as c:
            rows = c.execute(
                """SELECT * FROM quote_observations
                   WHERE t1 IS NULL AND t0 <= ?
                   ORDER BY t0 LIMIT ?""",
                (older_than, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def record_fill_check(self, obs_id: int, *, t1: float, yes_bid: float,
                          yes_ask: float, filled_bid: bool,
                          filled_ask: bool) -> None:
        """Stage 1. Records whether the market came to us, and nothing else."""
        with connect(self.db_path) as c:
            c.execute(
                """UPDATE quote_observations
                   SET t1 = ?, t1_yes_bid = ?, t1_yes_ask = ?,
                       filled_bid = ?, filled_ask = ?
                   WHERE id = ?""",
                (t1, yes_bid, yes_ask, int(filled_bid), int(filled_ask),
                 obs_id),
            )

    def awaiting_mark(self, older_than: float, limit: int = 200) -> list[dict]:
        """Filled quotes whose fill check is older than ``older_than``.

        Only filled rows: an unfilled quote has no position to mark, and
        marking one would invent a number for a trade that did not happen.
        """
        with connect(self.db_path) as c:
            rows = c.execute(
                """SELECT * FROM quote_observations
                   WHERE t1 IS NOT NULL AND t2 IS NULL
                     AND (filled_bid = 1 OR filled_ask = 1)
                     AND t1 <= ?
                   ORDER BY t1 LIMIT ?""",
                (older_than, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def record_mark(self, obs_id: int, *, t2: float, yes_bid: float,
                    yes_ask: float, bid_mark_cents, ask_mark_cents) -> None:
        """Stage 2. Marks a fill against a strictly later reading."""
        with connect(self.db_path) as c:
            c.execute(
                """UPDATE quote_observations
                   SET t2 = ?, t2_yes_bid = ?, t2_yes_ask = ?,
                       bid_mark_cents = ?, ask_mark_cents = ?, resolved_at = ?
                   WHERE id = ?""",
                (t2, yes_bid, yes_ask, bid_mark_cents, ask_mark_cents,
                 time.time(), obs_id),
            )

    def summary(self, zone: str = None) -> dict:
        """Fill and adverse-selection rates over resolved observations.

        Grouped nowhere by default: the caller asks per zone, because the
        whole strategy is that the zones behave differently and an aggregate
        across them describes none of them.
        """
        where = "WHERE t1 IS NOT NULL"
        params: list = []
        if zone:
            where += " AND zone = ?"
            params.append(zone)
        with connect(self.db_path) as c:
            row = c.execute(
                f"""SELECT
                      COUNT(*)                                    AS n,
                      SUM(filled_bid)                             AS bid_fills,
                      SUM(filled_ask)                             AS ask_fills,
                      SUM(filled_bid = 1 AND filled_ask = 1)      AS round_trips,
                      SUM(filled_bid = 1 AND bid_mark_cents < 0)  AS bid_adverse,
                      SUM(filled_ask = 1 AND ask_mark_cents < 0)  AS ask_adverse,
                      AVG(CASE WHEN filled_bid = 1 THEN bid_mark_cents END) AS bid_mark,
                      AVG(CASE WHEN filled_ask = 1 THEN ask_mark_cents END) AS ask_mark
                    FROM quote_observations {where}""",
                params,
            ).fetchone()
            out = dict(row)
            out["zone"] = zone or "all"
            for k in ("bid_fills", "ask_fills", "round_trips",
                      "bid_adverse", "ask_adverse"):
                out[k] = out[k] or 0
            return out
