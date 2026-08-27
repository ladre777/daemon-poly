"""
Persisted decision, latency and coverage telemetry.

The pass funnel already existed as a log line::

    Pass funnel: candidates=297 quant=149 llm_called=0 llm_disabled=109
    proposed=1 ladder_deduped=0 checked=1 approved=0 filled=0

That line is built from a ``defaultdict(int)`` local to ``run_once`` and is
discarded when the function returns. Everything it knows survives only as
text in a log aggregator with a retention window, which makes three ordinary
questions unanswerable:

    "what is the p95 checker latency this week"
    "how many abstentions were billing versus throttling"
    "what was the quote age at submission on the trades that filled"

This module keeps the same measurements as rows, so they can be aggregated
after the fact rather than grepped.

Two design rules, both load-bearing:

**It cannot break a trading pass.** Every public method swallows its own
exceptions and logs at warning. Telemetry is an observer; an observer that
can abort the thing it observes is a liability, not an instrument. The
trading path never sees an exception from here, and a failed write costs a
log line and a missing row.

**It cannot leak.** There is no column for prompts, model reasoning, checker
prose, credentials or environment values. Provider *identifiers* and outcome
*classifications* are stored; the text that flowed through them is not. This
is narrower than the existing ledger on purpose — ``edges.maker_reasoning``
already stores prose and is untouched by this work, but nothing new here
widens that surface.

Written to the same SQLite file as the rest of the memory layer, through the
same :func:`memory.db.connect` helper, so it inherits WAL mode and the busy
timeout rather than contending with them.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from config import CONFIG
from memory.db import connect

log = logging.getLogger("daemon_kalshi.telemetry")

SCHEMA = """
-- One row per pass. The numerators and denominators of every funnel and
-- coverage question, plus where the wall-clock went.
CREATE TABLE IF NOT EXISTS pass_telemetry (
    pass_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          REAL NOT NULL,
    finished_at         REAL,
    -- Configuration in force for this pass. Recorded per pass rather than
    -- read from the environment at report time, because a report run days
    -- later would otherwise attribute old rows to today's settings.
    dry_run             INTEGER,
    kalshi_env          TEXT,
    order_strategy      TEXT,
    balance_cents       REAL,
    -- Coverage. scan_cap_reached distinguishes "we stopped looking" from
    -- "we looked at everything and found nothing", which have opposite
    -- remedies and were previously indistinguishable.
    markets_seen        INTEGER,
    pages_scanned       INTEGER,
    scan_cap_reached    INTEGER,
    candidates          INTEGER,
    excluded_category   INTEGER,
    excluded_liquidity  INTEGER,
    excluded_no_spec    INTEGER,
    -- Stage durations, milliseconds. NULL where a stage did not run.
    scout_ms            REAL,
    pricing_ms          REAL,
    checker_ms          REAL,
    risk_ms             REAL,
    execution_ms        REAL
);

-- One row per (pass, stage, reason). The funnel, decomposed. A candidate
-- that stops at PROPOSED for reason CAPPED_PER_EVENT is one row here; the
-- report sums them into stage totals and refusal breakdowns without ever
-- collapsing two reasons into one bucket.
CREATE TABLE IF NOT EXISTS stage_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id             INTEGER NOT NULL,
    recorded_at         REAL NOT NULL,
    stage               TEXT NOT NULL,
    reason              TEXT,
    category            TEXT,
    source              TEXT,
    event_ticker        TEXT,
    count               INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_stage_pass ON stage_events(pass_id);
CREATE INDEX IF NOT EXISTS idx_stage_reason ON stage_events(reason);

-- One row per LLM call attempt, including the ones that never left the
-- process because a breaker was open. outcome carries a core.reasons.Reason
-- value on failure, so billing, throttling, timeout and truncation stay
-- distinguishable in aggregate.
CREATE TABLE IF NOT EXISTS provider_calls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id             INTEGER,
    recorded_at         REAL NOT NULL,
    role                TEXT,           -- 'maker' | 'checker'
    provider            TEXT,
    model               TEXT,
    started_at          REAL,
    finished_at         REAL,
    elapsed_ms          REAL,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    cached_tokens       INTEGER,
    ok                  INTEGER NOT NULL DEFAULT 0,
    outcome             TEXT,
    http_status         INTEGER,
    -- Which provider actually served it, when the primary did not. NULL
    -- means the primary answered; a value means a fallback did, which is a
    -- different reliability story from a clean success.
    fallback_provider   TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_outcome ON provider_calls(outcome);

-- Quote ages at each decision point, and what execution actually got versus
-- what the decision assumed. Written once per candidate that reached a
-- proposal, updated as it advances.
CREATE TABLE IF NOT EXISTS decision_telemetry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id             INTEGER,
    recorded_at         REAL NOT NULL,
    ticker              TEXT NOT NULL,
    event_ticker        TEXT,
    category            TEXT,
    source              TEXT,
    client_order_id     TEXT,
    edge_id             INTEGER,
    -- Quote freshness. quote_captured_at is the anchor; the ages are
    -- derived at write time so a report never has to guess the clock.
    quote_captured_at   REAL,
    age_at_proposal_ms  REAL,
    age_at_checker_ms   REAL,
    age_at_risk_ms      REAL,
    age_at_submit_ms    REAL,
    -- What the decision assumed it could transact at.
    decision_price_cents REAL,
    requested_count     INTEGER,
    -- What actually happened. NULL until reconciled; NULL is meaningfully
    -- different from 0 and the report renders it as N/A, never as zero.
    filled_count        INTEGER,
    avg_fill_price_cents REAL,
    fees_cents          REAL,
    slippage_cents      REAL,
    fill_latency_ms     REAL,
    terminal_state      TEXT,
    terminal_reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_pass ON decision_telemetry(pass_id);
CREATE INDEX IF NOT EXISTS idx_decision_order ON decision_telemetry(client_order_id);
"""

#: Columns a caller may set on ``decision_telemetry`` after the initial
#: insert. Listed explicitly rather than interpolated from kwargs so that a
#: typo becomes an error here instead of a silently dropped measurement.
_DECISION_UPDATABLE = frozenset({
    "age_at_checker_ms", "age_at_risk_ms", "age_at_submit_ms",
    "client_order_id", "edge_id", "filled_count", "avg_fill_price_cents",
    "fees_cents", "slippage_cents", "fill_latency_ms",
    "terminal_state", "terminal_reason",
})


def _ms(later: Optional[float], earlier: Optional[float]) -> Optional[float]:
    """Elapsed milliseconds, or None if either end is unknown.

    Returns None rather than a negative number when the clock runs backwards.
    A negative duration is not a small error, it is a broken measurement, and
    reporting it as ``None`` routes it to the "flagged" path instead of
    quietly dragging an average down.
    """
    if later is None or earlier is None:
        return None
    delta = (later - earlier) * 1000.0
    return None if delta < 0 else delta


class TelemetryStore:
    """Durable pass/stage/provider/decision telemetry.

    Every method is best-effort. See the module docstring: nothing here may
    propagate into the trading path.
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or CONFIG.ledger_db_path
        self._enabled = True
        try:
            with connect(self.db_path) as c:
                c.executescript(SCHEMA)
        except Exception as e:  # pragma: no cover - exercised via _guard tests
            # Disable rather than raise. A bot that will not start because its
            # instrumentation could not initialise is strictly worse than one
            # that trades without instrumentation and says so loudly.
            self._enabled = False
            log.warning("Telemetry disabled — schema init failed: %s", e)

    # -- internals --------------------------------------------------------

    def _guard(self, what: str, fn, default=None):
        if not self._enabled:
            return default
        try:
            return fn()
        except Exception as e:
            log.warning("Telemetry %s failed (continuing): %s", what, e)
            return default

    # -- pass lifecycle ---------------------------------------------------

    def begin_pass(self, *, dry_run: bool = None, kalshi_env: str = None,
                   order_strategy: str = None,
                   balance_cents: float = None) -> Optional[int]:
        """Open a pass row and return its id, or None if telemetry is off.

        Callers must tolerate None — that is the whole contract that keeps
        this from being able to break a pass.
        """
        def _do():
            with connect(self.db_path) as c:
                cur = c.execute(
                    """INSERT INTO pass_telemetry
                       (started_at, dry_run, kalshi_env, order_strategy, balance_cents)
                       VALUES (?,?,?,?,?)""",
                    (time.time(),
                     None if dry_run is None else int(bool(dry_run)),
                     kalshi_env, order_strategy, balance_cents),
                )
                return cur.lastrowid
        return self._guard("begin_pass", _do)

    def finish_pass(self, pass_id: Optional[int], **fields: Any) -> None:
        """Close a pass row, writing coverage counts and stage durations."""
        if pass_id is None:
            return
        allowed = {
            "markets_seen", "pages_scanned", "scan_cap_reached", "candidates",
            "excluded_category", "excluded_liquidity", "excluded_no_spec",
            "scout_ms", "pricing_ms", "checker_ms", "risk_ms", "execution_ms",
        }
        sets, params = ["finished_at = ?"], [time.time()]
        for k, v in fields.items():
            if k not in allowed:
                log.warning("Telemetry finish_pass ignoring unknown field %r", k)
                continue
            sets.append(f"{k} = ?")
            params.append(int(v) if k == "scan_cap_reached" and v is not None else v)
        params.append(pass_id)

        def _do():
            with connect(self.db_path) as c:
                c.execute(
                    f"UPDATE pass_telemetry SET {', '.join(sets)} WHERE pass_id = ?",
                    params,
                )
        self._guard("finish_pass", _do)

    # -- funnel -----------------------------------------------------------

    def record_stage(self, pass_id: Optional[int], stage: str, *,
                     reason: str = None, category: str = None,
                     source: str = None, event_ticker: str = None,
                     count: int = 1) -> None:
        """Record ``count`` candidates reaching ``stage``, optionally stopping
        there for ``reason``.

        ``stage`` and ``reason`` are the string values of
        :class:`core.reasons.Stage` and :class:`core.reasons.Reason`. They are
        not validated here — an unrecognised value is still more informative
        in the table than a rejected write would be — but the reporting layer
        flags any value it cannot map, so a typo surfaces rather than hides.
        """
        def _do():
            with connect(self.db_path) as c:
                c.execute(
                    """INSERT INTO stage_events
                       (pass_id, recorded_at, stage, reason, category, source,
                        event_ticker, count)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (pass_id, time.time(), str(stage),
                     None if reason is None else str(reason),
                     category, source, event_ticker, int(count)),
                )
        self._guard("record_stage", _do)

    def record_stages(self, pass_id: Optional[int], counts: dict) -> None:
        """Bulk form of :meth:`record_stage` for end-of-pass stat dictionaries.

        Accepts ``{(stage, reason): count}`` or ``{stage: count}``. Zero
        counts are skipped: a table full of zeroes makes a real zero — the
        interesting kind, where a stage that normally fires did not — harder
        to see, not easier.
        """
        def _do():
            rows = []
            now = time.time()
            for key, count in (counts or {}).items():
                if not count:
                    continue
                stage, reason = key if isinstance(key, tuple) else (key, None)
                rows.append((pass_id, now, str(stage),
                             None if reason is None else str(reason),
                             None, None, None, int(count)))
            if not rows:
                return
            with connect(self.db_path) as c:
                c.executemany(
                    """INSERT INTO stage_events
                       (pass_id, recorded_at, stage, reason, category, source,
                        event_ticker, count)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    rows,
                )
        self._guard("record_stages", _do)

    # -- provider calls ---------------------------------------------------

    def record_provider_call(self, pass_id: Optional[int], *, role: str,
                             provider: str, model: str = None,
                             started_at: float = None, finished_at: float = None,
                             ok: bool = False, outcome: str = None,
                             http_status: int = None,
                             prompt_tokens: int = None,
                             completion_tokens: int = None,
                             cached_tokens: int = None,
                             fallback_provider: str = None) -> None:
        """Record one LLM call attempt and how it ended."""
        def _do():
            with connect(self.db_path) as c:
                c.execute(
                    """INSERT INTO provider_calls
                       (pass_id, recorded_at, role, provider, model, started_at,
                        finished_at, elapsed_ms, prompt_tokens, completion_tokens,
                        cached_tokens, ok, outcome, http_status, fallback_provider)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pass_id, time.time(), role, provider, model, started_at,
                     finished_at, _ms(finished_at, started_at), prompt_tokens,
                     completion_tokens, cached_tokens, int(bool(ok)),
                     None if outcome is None else str(outcome),
                     http_status, fallback_provider),
                )
        self._guard("record_provider_call", _do)

    # -- per-decision -----------------------------------------------------

    def record_decision(self, pass_id: Optional[int], *, ticker: str,
                        event_ticker: str = None, category: str = None,
                        source: str = None, quote_captured_at: float = None,
                        proposed_at: float = None,
                        decision_price_cents: float = None,
                        requested_count: int = None) -> Optional[int]:
        """Open a decision row when a candidate becomes a proposal."""
        def _do():
            with connect(self.db_path) as c:
                cur = c.execute(
                    """INSERT INTO decision_telemetry
                       (pass_id, recorded_at, ticker, event_ticker, category,
                        source, quote_captured_at, age_at_proposal_ms,
                        decision_price_cents, requested_count)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (pass_id, time.time(), ticker, event_ticker, category,
                     source, quote_captured_at,
                     _ms(proposed_at, quote_captured_at),
                     decision_price_cents, requested_count),
                )
                return cur.lastrowid
        return self._guard("record_decision", _do)

    def update_decision(self, decision_id: Optional[int], **fields: Any) -> None:
        """Advance a decision row as it moves through checker/risk/execution."""
        if decision_id is None:
            return
        sets, params = [], []
        for k, v in fields.items():
            if k not in _DECISION_UPDATABLE:
                log.warning("Telemetry update_decision ignoring unknown field %r", k)
                continue
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return
        params.append(decision_id)

        def _do():
            with connect(self.db_path) as c:
                c.execute(
                    f"UPDATE decision_telemetry SET {', '.join(sets)} WHERE id = ?",
                    params,
                )
        self._guard("update_decision", _do)

    def mark_quote_age(self, decision_id: Optional[int], field: str,
                       at: float, quote_captured_at: float) -> None:
        """Convenience for the three post-proposal quote ages.

        ``field`` is one of ``age_at_checker_ms``, ``age_at_risk_ms``,
        ``age_at_submit_ms``.
        """
        if field not in {"age_at_checker_ms", "age_at_risk_ms", "age_at_submit_ms"}:
            log.warning("Telemetry mark_quote_age rejecting field %r", field)
            return
        self.update_decision(decision_id, **{field: _ms(at, quote_captured_at)})
