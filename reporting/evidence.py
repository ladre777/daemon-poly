"""
Mode-separated performance evidence.

The single rule this module exists to enforce:

    A counterfactual number may never appear in a line labelled P&L.

The ledger holds three kinds of row, and only one of them is money. The
distinction is already made in :meth:`EdgeStore.calibration_by_category` and
in :func:`core.reasons.mode_for_action`; this module carries it all the way
out to the rendered report, so that no aggregate anywhere sums across modes.

Why that is worth a module. On 2026-08-27 the production summary read::

    Crypto/quant: n=9,675  brier=0.124  said 44% actual 45%  pnl -$39.07
    Weather/llm:  n=2,729  brier=0.205  said 29% actual 27%  pnl -$0.62
    Finance/llm:  n=1,581  brier=0.085  said 25% actual  5%  pnl +$87.79

Every one of those rows is ``refused`` — the trade never happened. The
Kalshi balance was $0.00 and the bot filled nothing, ever. Summed naively
that is "+$48.10 profit", a number describing no event in the real world. It
would be a completely honest-looking line in a report, and it would be
false. The +$87.79 in particular is the most quotable figure on the board
and the least real.

So: ``live`` totals come only from settlement rows. ``paper`` and
``refused`` totals are labelled counterfactual at every level, carry their
disqualifying conditions with them, and the aggregate refused section is
opt-in behind a flag.

The second rule: **absence is reported, never defaulted.** A missing fee, an
unreconciled fill, a denominator that does not exist — each renders as N/A
with the reason attached. ``0.0`` is a claim about the world; ``None``
silently becomes ``0`` in most arithmetic, and a zero fee is a very
attractive lie for a trading report to tell.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from enum import Enum

from core.reasons import Mode, mode_for_action

#: Calibration cells thinner than this are reported with a warning attached.
#: Not a gate and not a threshold on any trading decision — purely a label on
#: a statistic. Exposed as a report parameter so a reviewer can vary it.
DEFAULT_MIN_CALIBRATION_SAMPLE = 100

#: Fill-verified observations needed before any fee-net conclusion is drawn.
#: Deliberately explicit rather than buried: the readiness assessment reports
#: this number alongside the verdict, so a reader can disagree with it.
DEFAULT_MIN_FILL_SAMPLE = 30

#: Reliability-table buckets over stated probability.
_BUCKETS = ((0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
            (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.001))


@dataclass(frozen=True)
class Unavailable:
    """A value that does not exist, and precisely why.

    Rendered as ``N/A (reason)``. It is not a number and deliberately does
    not support arithmetic: any attempt to average or sum it raises, which
    is how a manufactured figure gets caught at the point of manufacture
    rather than in the report.
    """

    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"N/A ({self.reason})"


#: A quantity that may not exist.
Maybe = Union[float, int, Unavailable, None]


def _is_value(x: Maybe) -> bool:
    return x is not None and not isinstance(x, Unavailable)


def to_dict(obj):
    """Like :func:`dataclasses.asdict`, but :class:`Unavailable` survives.

    ``dataclasses.asdict`` recurses into every dataclass it finds, and
    ``Unavailable`` is one — so it silently becomes ``{"reason": "..."}``,
    which the renderer would then try to format as a number. The sentinel
    has to reach the renderer intact for ``N/A (reason)`` to be printable at
    all, so it is returned as itself here.

    Enums are reduced to their values on the way out, so the payload is
    JSON-serialisable without a custom encoder having to know the type.
    """
    if isinstance(obj, Unavailable):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name))
                for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


@dataclass
class LiveEvidence:
    """Fill- and settlement-confirmed quantities only.

    Every field here is derived from ``orders``, ``fills`` and
    ``settlements`` — never from ``edges.counterfactual_*``. If the tables
    are empty, every field is :class:`Unavailable`, which is the correct
    description of a bot that has never traded.
    """

    orders_submitted: int = 0
    orders_filled: int = 0
    orders_partially_filled: int = 0
    orders_cancelled: int = 0
    orders_rejected: int = 0
    orders_unknown: int = 0
    orders_expired: int = 0
    contracts_requested: int = 0
    contracts_filled: int = 0
    fill_rate: Maybe = None
    partial_fill_rate: Maybe = None
    gross_pnl: Maybe = None
    fees: Maybe = None
    net_realized_pnl: Maybe = None
    turnover_cents: Maybe = None
    open_marked_exposure: Maybe = None
    open_unmarked_exposure: Maybe = None
    avg_holding_seconds: Maybe = None
    max_holding_seconds: Maybe = None
    return_on_deployed_capital: Maybe = None
    max_drawdown: Maybe = None
    equity_curve: list = field(default_factory=list)
    avg_slippage_cents: Maybe = None
    notes: list = field(default_factory=list)


@dataclass
class CounterfactualEvidence:
    """Paper or refused rows. Never money.

    ``disqualifiers`` lists, in plain language, every reason these numbers
    are not performance. It is rendered next to the numbers rather than in a
    footnote, because a footnote is where a caveat goes to be ignored.
    """

    mode: Mode
    n: int = 0
    settled_n: int = 0
    modelled_pnl: Maybe = None
    avg_decision_price_cents: Maybe = None
    sizing_assumption: str = "unknown"
    disqualifiers: list = field(default_factory=list)
    by_category: list = field(default_factory=list)


@dataclass
class CalibrationCell:
    category: Optional[str]
    source: Optional[str]
    mode: Mode
    n: int
    brier: Maybe
    avg_stated_probability: Maybe
    observed_event_rate: Maybe
    counterfactual_pnl: Maybe
    reliability: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _table_names(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _scalar(conn, sql, params=()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------

def live_evidence(conn: sqlite3.Connection, *, since: float = None,
                  until: float = None) -> LiveEvidence:
    """Aggregate genuinely-executed activity.

    ``dry_run`` orders are excluded at the SQL level rather than filtered
    afterwards. That ordering matters: a dry-run order carries a
    ``limit_price_cents`` and a ``requested_count`` like any other, so a
    later filter that is forgotten in one branch silently promotes simulated
    activity into the live table.
    """
    ev = LiveEvidence()
    tables = _table_names(conn)
    if "orders" not in tables:
        reason = "orders table absent — no execution history in this database"
        for f in ("fill_rate", "partial_fill_rate", "gross_pnl", "fees",
                  "net_realized_pnl", "turnover_cents", "max_drawdown",
                  "return_on_deployed_capital", "avg_slippage_cents",
                  "avg_holding_seconds", "max_holding_seconds"):
            setattr(ev, f, Unavailable(reason))
        ev.notes.append(reason)
        return ev

    where = ["COALESCE(dry_run, 0) = 0"]
    params: list = []
    if since is not None:
        where.append("created_at >= ?")
        params.append(float(since))
    if until is not None:
        where.append("created_at < ?")
        params.append(float(until))
    w = " WHERE " + " AND ".join(where)

    row = conn.execute(
        f"""SELECT COUNT(*) AS n,
                   COALESCE(SUM(requested_count), 0) AS req,
                   COALESCE(SUM(filled_count), 0) AS fil,
                   SUM(CASE WHEN state='filled' THEN 1 ELSE 0 END) AS filled,
                   SUM(CASE WHEN filled_count > 0
                             AND filled_count < requested_count THEN 1 ELSE 0 END) AS partial,
                   SUM(CASE WHEN state='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                   SUM(CASE WHEN state='rejected' THEN 1 ELSE 0 END) AS rejected,
                   SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END) AS unknown,
                   SUM(CASE WHEN state='expired' THEN 1 ELSE 0 END) AS expired,
                   COALESCE(SUM(fees_cents), 0) AS fees
            FROM orders{w}""",
        params,
    ).fetchone()

    ev.orders_submitted = int(row["n"] or 0)
    ev.contracts_requested = int(row["req"] or 0)
    ev.contracts_filled = int(row["fil"] or 0)
    ev.orders_filled = int(row["filled"] or 0)
    ev.orders_partially_filled = int(row["partial"] or 0)
    ev.orders_cancelled = int(row["cancelled"] or 0)
    ev.orders_rejected = int(row["rejected"] or 0)
    ev.orders_unknown = int(row["unknown"] or 0)
    ev.orders_expired = int(row["expired"] or 0)

    if ev.orders_submitted == 0:
        reason = "no non-dry-run orders in window"
        ev.notes.append(
            "No live orders exist in this window. Every figure below is N/A "
            "because nothing was executed — not because a measurement failed."
        )
        for f in ("fill_rate", "partial_fill_rate", "gross_pnl", "fees",
                  "net_realized_pnl", "turnover_cents", "max_drawdown",
                  "return_on_deployed_capital", "avg_slippage_cents",
                  "avg_holding_seconds", "max_holding_seconds",
                  "open_marked_exposure", "open_unmarked_exposure"):
            setattr(ev, f, Unavailable(reason))
        return ev

    ev.fill_rate = ev.contracts_filled / ev.contracts_requested \
        if ev.contracts_requested else Unavailable("no contracts requested")
    ev.partial_fill_rate = ev.orders_partially_filled / ev.orders_submitted

    # -- realized money, settlements only -------------------------------
    if "settlements" not in tables:
        r = "settlements table absent"
        ev.gross_pnl = ev.net_realized_pnl = Unavailable(r)
        ev.fees = Unavailable(r)
    else:
        sw, sp = "", []
        if since is not None:
            sw, sp = " WHERE settled_at >= ?", [float(since)]
            if until is not None:
                sw += " AND settled_at < ?"
                sp.append(float(until))
        elif until is not None:
            sw, sp = " WHERE settled_at < ?", [float(until)]
        s = conn.execute(
            f"""SELECT COUNT(*) AS n,
                       SUM(realized_pnl) AS pnl,
                       SUM(fees_cents) AS fees,
                       SUM(CASE WHEN realized_pnl IS NULL THEN 1 ELSE 0 END) AS missing_pnl,
                       SUM(CASE WHEN fees_cents IS NULL THEN 1 ELSE 0 END) AS missing_fees
                FROM settlements{sw}""",
            sp,
        ).fetchone()
        n_settled = int(s["n"] or 0)
        if n_settled == 0:
            r = "no settled rows in window — fills may exist but nothing has resolved"
            ev.gross_pnl = ev.net_realized_pnl = Unavailable(r)
            ev.fees = Unavailable(r)
            ev.notes.append(r)
        elif s["missing_pnl"]:
            r = (f"{int(s['missing_pnl'])} of {n_settled} settlement rows have "
                 "NULL realized_pnl — refusing to treat missing as zero")
            ev.gross_pnl = ev.net_realized_pnl = Unavailable(r)
            ev.notes.append(r)
        else:
            fees = None if s["missing_fees"] else float(s["fees"] or 0.0)
            ev.gross_pnl = float(s["pnl"] or 0.0)
            if fees is None:
                ev.fees = Unavailable(
                    f"{int(s['missing_fees'])} settlement rows have NULL fees_cents")
                ev.net_realized_pnl = Unavailable(
                    "actual fees unavailable, so net cannot be computed; "
                    "modelled fees are not a substitute")
            else:
                ev.fees = fees
                ev.net_realized_pnl = ev.gross_pnl - fees

    # -- turnover and slippage from fills -------------------------------
    if "fills" not in tables:
        ev.turnover_cents = Unavailable("fills table absent")
        ev.avg_slippage_cents = Unavailable("fills table absent")
    else:
        turnover = _scalar(conn, "SELECT COALESCE(SUM(count * price_cents), 0) FROM fills")
        ev.turnover_cents = float(turnover or 0.0)
        slip = conn.execute(
            """SELECT AVG(
                   CASE WHEN o.action = 'buy' THEN o.avg_fill_price_cents - o.limit_price_cents
                        ELSE o.limit_price_cents - o.avg_fill_price_cents END) AS slip
               FROM orders o
               WHERE COALESCE(o.dry_run,0)=0 AND o.avg_fill_price_cents IS NOT NULL"""
        ).fetchone()
        ev.avg_slippage_cents = (
            float(slip["slip"]) if slip and slip["slip"] is not None
            else Unavailable("no filled orders with a recorded average price")
        )

    # -- holding time ----------------------------------------------------
    hold = conn.execute(
        """SELECT AVG(terminal_at - submitted_at) AS avg_h,
                  MAX(terminal_at - submitted_at) AS max_h
           FROM orders
           WHERE COALESCE(dry_run,0)=0 AND terminal_at IS NOT NULL
             AND submitted_at IS NOT NULL"""
    ).fetchone()
    if hold and hold["avg_h"] is not None:
        ev.avg_holding_seconds = float(hold["avg_h"])
        ev.max_holding_seconds = float(hold["max_h"])
    else:
        r = "no order has both submitted_at and terminal_at recorded"
        ev.avg_holding_seconds = Unavailable(r)
        ev.max_holding_seconds = Unavailable(r)

    # -- equity curve and drawdown --------------------------------------
    if "settlements" in tables and _is_value(ev.net_realized_pnl):
        curve = conn.execute(
            """SELECT CAST(settled_at/86400 AS INTEGER) AS day,
                      SUM(realized_pnl) AS pnl
               FROM settlements WHERE settled_at IS NOT NULL
               GROUP BY day ORDER BY day"""
        ).fetchall()
        running, peak, mdd = 0.0, 0.0, 0.0
        for c in curve:
            running += float(c["pnl"] or 0.0)
            peak = max(peak, running)
            mdd = min(mdd, running - peak)
            ev.equity_curve.append({"day_index": int(c["day"]), "cumulative_pnl": running})
        ev.max_drawdown = mdd if ev.equity_curve else Unavailable("no settled days")
    else:
        ev.max_drawdown = Unavailable("net realized PnL unavailable")

    # -- return on deployed capital -------------------------------------
    # Only computed when a real denominator exists. Deployed capital is not
    # the same as balance, and a bot that has never deployed capital has no
    # denominator at all — 0/0 is not 0%.
    if _is_value(ev.net_realized_pnl) and _is_value(ev.turnover_cents) and ev.turnover_cents:
        ev.return_on_deployed_capital = ev.net_realized_pnl / ev.turnover_cents
    else:
        ev.return_on_deployed_capital = Unavailable(
            "no deployed-capital denominator — nothing was filled")

    ev.open_marked_exposure = Unavailable(
        "open exposure is exchange state, not ledger state; this report is "
        "read-only and never contacts Kalshi")
    ev.open_unmarked_exposure = ev.open_marked_exposure
    return ev


# ---------------------------------------------------------------------------
# counterfactual
# ---------------------------------------------------------------------------

_PAPER_DISQUALIFIERS = [
    "Approved but not executed on the exchange, or executed with zero fill.",
    "PnL is computed from the decision-time quote, not from a fill.",
    "Modelled fees are an estimate; FEE_RATE is documented as unverified "
    "against a published Kalshi schedule (see docs/SAFETY.md).",
    "No slippage, queue position or adverse selection is represented.",
]

_REFUSED_DISQUALIFIERS = [
    "The Checker or a risk gate declined this trade. It never existed.",
    "PnL is what the trade would have returned had every gate passed and "
    "the decision-time quote been available in full size.",
    "Useful only for the question 'are the gates refusing winners'. It is "
    "not performance, and must never be summed with live results.",
    "Modelled fees are an estimate; FEE_RATE is documented as unverified.",
    "During the 2026-08 window every refusal was upstream zero-balance "
    "rejection, so these rows measure the model, not the gates.",
]


def counterfactual_evidence(conn: sqlite3.Connection, mode: Mode, *,
                            since: float = None,
                            until: float = None) -> CounterfactualEvidence:
    """Aggregate paper or refused rows, permanently labelled as such."""
    out = CounterfactualEvidence(mode=mode)
    out.disqualifiers = list(
        _PAPER_DISQUALIFIERS if mode is Mode.PAPER else _REFUSED_DISQUALIFIERS)
    out.sizing_assumption = (
        "decision-time requested size; no partial-fill or queue model applied")

    if "edges" not in _table_names(conn):
        out.disqualifiers.append("edges table absent — nothing to report")
        return out

    where, params = ["maker_probability IS NOT NULL"], []
    if since is not None:
        where.append("created_at >= ?")
        params.append(float(since))
    if until is not None:
        where.append("created_at < ?")
        params.append(float(until))
    w = " WHERE " + " AND ".join(where)

    rows = conn.execute(
        f"""SELECT action_taken, settled, pnl, counterfactual_price_cents, category
            FROM edges{w}""", params).fetchall()

    kept = [r for r in rows if mode_for_action(r["action_taken"]) is mode]
    out.n = len(kept)
    settled = [r for r in kept if r["settled"]]
    out.settled_n = len(settled)

    if not settled:
        out.modelled_pnl = Unavailable(
            "no settled rows in this mode — nothing has resolved yet")
    else:
        graded = [r for r in settled if r["pnl"] is not None]
        if len(graded) != len(settled):
            out.modelled_pnl = Unavailable(
                f"{len(settled) - len(graded)} of {len(settled)} settled rows "
                "have NULL pnl — refusing to treat missing as zero")
        else:
            out.modelled_pnl = sum(float(r["pnl"]) for r in graded)

    prices = [float(r["counterfactual_price_cents"]) for r in kept
              if r["counterfactual_price_cents"] is not None]
    out.avg_decision_price_cents = (
        sum(prices) / len(prices) if prices
        else Unavailable("no decision-time price recorded on these rows"))

    by_cat: dict = {}
    for r in kept:
        c = by_cat.setdefault(r["category"] or "(uncategorised)",
                              {"category": r["category"] or "(uncategorised)",
                               "n": 0, "settled": 0, "pnl": 0.0, "pnl_known": True})
        c["n"] += 1
        if r["settled"]:
            c["settled"] += 1
            if r["pnl"] is None:
                c["pnl_known"] = False
            else:
                c["pnl"] += float(r["pnl"])
    for c in by_cat.values():
        # A category with nothing settled has no PnL, and 0.0 would be a
        # claim that it broke even. This is the same manufactured-zero the
        # module docstring warns about, one level down in the per-category
        # breakdown, and it is easy to reintroduce: the accumulator starts
        # at 0.0 and simply never gets added to.
        if not c["settled"]:
            c["pnl"] = Unavailable("no settled rows in this category")
        elif not c["pnl_known"]:
            c["pnl"] = Unavailable("some settled rows have NULL pnl")
    out.by_category = sorted(by_cat.values(), key=lambda c: -c["n"])
    return out


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------

def calibration(conn: sqlite3.Connection, *, since: float = None,
                until: float = None,
                min_sample: int = DEFAULT_MIN_CALIBRATION_SAMPLE) -> list:
    """Calibration per (category, source, mode), with a reliability table.

    Reported separately from PnL and never used as an approval criterion.
    A Brier score measures whether stated probabilities track observed
    frequencies; it says nothing about whether acting on them earns money
    after fees. The 2026-08 data is the standing counter-example — the
    best-calibrated cell on the board (crypto/quant, said 44% against 45%
    observed) carries the most negative counterfactual PnL.
    """
    if "edges" not in _table_names(conn):
        return []

    where = ["settled = 1", "maker_probability IS NOT NULL"]
    params: list = []
    if since is not None:
        where.append("created_at >= ?")
        params.append(float(since))
    if until is not None:
        where.append("created_at < ?")
        params.append(float(until))
    w = " WHERE " + " AND ".join(where)

    rows = conn.execute(
        f"""SELECT category, source, action_taken, maker_probability, outcome, pnl
            FROM edges{w}""", params).fetchall()

    grouped: dict = {}
    for r in rows:
        key = (r["category"], r["source"], mode_for_action(r["action_taken"]))
        grouped.setdefault(key, []).append(r)

    cells = []
    for (cat, src, mode), rs in sorted(
            grouped.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        probs = [float(r["maker_probability"]) for r in rs]
        outcomes = [1.0 if r["outcome"] == "yes" else 0.0 for r in rs]
        brier = sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / n
        pnls = [r["pnl"] for r in rs]
        cf_pnl = (sum(float(p) for p in pnls) if all(p is not None for p in pnls)
                  else Unavailable("some rows have NULL pnl"))

        reliability = []
        for lo, hi in _BUCKETS:
            members = [(p, o) for p, o in zip(probs, outcomes) if lo <= p < hi]
            if not members:
                continue
            reliability.append({
                "bucket": f"{lo:.0%}-{min(hi, 1.0):.0%}",
                "n": len(members),
                "avg_stated": sum(p for p, _ in members) / len(members),
                "observed": sum(o for _, o in members) / len(members),
            })

        warnings = []
        if n < min_sample:
            warnings.append(
                f"n={n} is below the {min_sample}-row minimum for this report; "
                "treat the Brier score as indicative only")
        if mode is not Mode.LIVE:
            warnings.append(
                f"mode={mode.value}: PnL here is counterfactual, not realized")

        cells.append(CalibrationCell(
            category=cat, source=src, mode=mode, n=n, brier=brier,
            avg_stated_probability=sum(probs) / n,
            observed_event_rate=sum(outcomes) / n,
            counterfactual_pnl=cf_pnl, reliability=reliability,
            warnings=warnings,
        ))
    return cells


def mode_contamination(conn: sqlite3.Connection) -> list:
    """Rows whose mode cannot be trusted, which is a data-integrity failure.

    Two conditions, both of which would corrupt a live total if ignored:

    1. A row marked ``executed`` that carries a counterfactual price. Those
       columns are documented as mutually exclusive — counterfactual pricing
       is set only on rows that never traded.
    2. A row marked ``executed`` with no ``client_order_id``, so it cannot be
       joined to a fill and its claim to be live cannot be verified.
    """
    if "edges" not in _table_names(conn):
        return []
    findings = []
    n = _scalar(conn, """SELECT COUNT(*) FROM edges
                         WHERE action_taken='executed'
                           AND counterfactual_price_cents IS NOT NULL""")
    if n:
        findings.append({
            "issue": "executed_row_has_counterfactual_price",
            "count": int(n),
            "detail": "rows claim to be live but carry counterfactual pricing; "
                      "these columns are mutually exclusive by design",
        })
    n = _scalar(conn, """SELECT COUNT(*) FROM edges
                         WHERE action_taken='executed' AND
                               (client_order_id IS NULL OR client_order_id='')""")
    if n:
        findings.append({
            "issue": "executed_row_not_joinable_to_fill",
            "count": int(n),
            "detail": "rows claim to be live but have no client_order_id, so the "
                      "claim cannot be verified against fills",
        })
    return findings


def duplicate_accounting(conn: sqlite3.Connection) -> list:
    """Double-counted client order IDs or fills.

    The schema already enforces uniqueness on ``orders.client_order_id`` and
    ``fills.fill_id``. This checks the invariant from the outside anyway: a
    constraint that was added after data existed does not retroactively clean
    it, and a report that assumes its inputs are sound is the wrong place to
    find out they were not.
    """
    tables = _table_names(conn)
    findings = []
    if "fills" in tables:
        n = _scalar(conn, """SELECT COUNT(*) FROM (
                                SELECT fill_id FROM fills
                                GROUP BY fill_id HAVING COUNT(*) > 1)""")
        if n:
            findings.append({"issue": "duplicate_fill_id", "count": int(n)})
    if "settlements" in tables:
        n = _scalar(conn, """SELECT COUNT(*) FROM (
                                SELECT settlement_key FROM settlements
                                GROUP BY settlement_key HAVING COUNT(*) > 1)""")
        if n:
            findings.append({"issue": "duplicate_settlement_key", "count": int(n)})
    return findings
