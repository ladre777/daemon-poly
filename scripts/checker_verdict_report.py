#!/usr/bin/env python3
"""
Read-only report on what the Checker actually decided.

Answers the question ``FINDINGS.md`` Q2 could not reach: **does the Checker
approve anything at all?**

That question is unanswerable from logs, and it is worth being precise about
why, because the reason is structural rather than an oversight:

- The pass funnel line prints ``checked`` and ``approved`` but neither
  ``checker_rejected`` nor ``risk_refused``.
- ``approved`` is incremented *after* the risk gate, and while the exchange
  balance is zero the bankroll check inside ``risk.evaluate`` short-circuits
  before the verdict branch. So every proposal that reaches risk is refused
  with the identical string regardless of what the Checker said.
- A Checker rejection does not ``continue`` — it falls through to risk and is
  refused on bankroll first. An approved verdict and a rejected verdict
  therefore produce *byte-identical* ledger log lines.

The verdicts survive in ``edges.checker_verdict`` and
``edges.checker_confidence``, written by ``ledger.log_decision``. This script
reads them and nothing else.

Usage::

    python -m scripts.checker_verdict_report --db ./ledger-copy.db
    python -m scripts.checker_verdict_report --db ./ledger-copy.db \\
        --since 2026-08-26T00:00:00Z
    python -m scripts.checker_verdict_report --db ./ledger-copy.db --json

Take a copy of the database first. Never point this at the file a running
daemon is writing.

Posture, copied deliberately from ``scripts/production_readiness_report.py``:
opened through a ``file:...?mode=ro`` URI so SQLite itself refuses writes; no
migration, no table creation, no network client, no contact with Kalshi.

Exit codes: 0 on success, 2 if the database cannot be read. A missing
database exits non-zero with a message rather than printing an empty report,
because an empty report reads like a finding — "the Checker approved
nothing" and "we could not look" must never render the same way.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tickers import family_of  # noqa: E402  (needs the path above)

#: Free-text columns that carry model prose. Never selected, never printed.
#: Mirrors the same list in scripts/weekend_export.py — model output can
#: contain anything, including text shaped like instructions to a reader.
REDACTED_COLUMNS = frozenset({
    "maker_reasoning", "checker_reasoning", "kill_switch_reason", "last_error",
})


def open_readonly(path: str) -> sqlite3.Connection:
    """Open strictly read-only, so this cannot alter or lock what it reports."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_iso(s: str) -> float:
    """ISO8601 to epoch. Accepts a trailing Z, which operators type and
    ``fromisoformat`` rejects on older interpreters."""
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso(ts) -> str:
    if ts is None:
        return "unbounded"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _stats(values: list) -> dict:
    """Mean and median, or an explicit absence. Never a zero standing in for
    'no observations' — that is the same lie as a defaulted fee."""
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"n_with_confidence": 0, "mean": None, "median": None}
    return {
        "n_with_confidence": len(clean),
        "mean": round(statistics.mean(clean), 4),
        "median": round(statistics.median(clean), 4),
    }


def collect(conn: sqlite3.Connection, *, since: float = None,
            until: float = None) -> dict:
    """Read every checked row in the window and group it four ways.

    A row counts as "checked" when ``checker_verdict`` is non-NULL — that is
    exactly the set that reached ``checker.check()`` and came back with an
    answer. Rows refused earlier (coherence gate, per-event cap, ladder
    dedup) never got a verdict and are correctly absent.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "edges" not in tables:
        raise sqlite3.DatabaseError(
            "no 'edges' table in this database — wrong file?")

    where = ["checker_verdict IS NOT NULL"]
    params: list = []
    if since is not None:
        where.append("created_at >= ?")
        params.append(float(since))
    if until is not None:
        where.append("created_at < ?")
        params.append(float(until))

    rows = conn.execute(
        f"""SELECT ticker, category, source, created_at,
                   checker_verdict, checker_confidence, action_taken
            FROM edges WHERE {' AND '.join(where)}
            ORDER BY created_at""",
        params,
    ).fetchall()

    overall: dict = {}
    by_family: dict = {}
    by_day: dict = {}
    by_category: dict = {}
    conf_by_verdict: dict = {}
    actions: dict = {}

    for r in rows:
        v = (r["checker_verdict"] or "(null)").strip().lower()
        fam = family_of(r["ticker"])
        day = datetime.fromtimestamp(
            float(r["created_at"]), tz=timezone.utc).strftime("%Y-%m-%d")
        cat = r["category"] or "(uncategorised)"

        overall[v] = overall.get(v, 0) + 1
        by_family.setdefault(fam, {})[v] = by_family.setdefault(fam, {}).get(v, 0) + 1
        by_day.setdefault(day, {})[v] = by_day.setdefault(day, {}).get(v, 0) + 1
        by_category.setdefault(cat, {})[v] = by_category.setdefault(cat, {}).get(v, 0) + 1
        conf_by_verdict.setdefault(v, []).append(r["checker_confidence"])
        a = r["action_taken"] or "(null)"
        actions[a] = actions.get(a, 0) + 1

    verdicts = sorted(overall)
    return {
        "window": {"since": _iso(since), "until": _iso(until)},
        "total_checked": len(rows),
        "verdicts": verdicts,
        "overall": overall,
        "confidence": {v: _stats(conf_by_verdict.get(v, [])) for v in verdicts},
        "by_family": by_family,
        "by_day": by_day,
        "by_category": by_category,
        "action_taken": actions,
        "first_row": _iso(rows[0]["created_at"]) if rows else None,
        "last_row": _iso(rows[-1]["created_at"]) if rows else None,
    }


#: Printed with every report. The numbers below are real judgements about
#: real quotes; the outcomes attached to them are not.
INTERPRETATION = """\
HOW TO READ THIS

  The verdicts are real. The Checker ran on live quotes and returned an
  answer for every row counted here.

  The outcomes are not. Throughout this window the exchange balance was
  $0.00, so every one of these rows was refused downstream by the bankroll
  check inside risk.evaluate — before the verdict branch and before any
  sizing rule. A Checker approval here never became a trade, and never
  could have.

  Therefore: do NOT present approval rate and counterfactual P&L as if one
  caused the other. Nothing in this window tests whether acting on these
  verdicts would have made money. It tests only what the Checker decided.

  A family showing 0 approvals is a family the Checker never approved in
  this window. That is a finding. A family absent from the table entirely
  never reached the Checker at all — it was stopped earlier, by the
  coherence gate, a per-event cap, or ladder dedup — which is a different
  finding with a different cause.
"""


def render(d: dict) -> str:
    L: list = []
    add = L.append
    add("=" * 72)
    add("CHECKER VERDICT REPORT")
    add("=" * 72)
    add(f"Window   : {d['window']['since']}  ->  {d['window']['until']}")
    if d["first_row"]:
        add(f"Rows span: {d['first_row']}  ->  {d['last_row']}")
    add(f"Checked  : {d['total_checked']} rows with a Checker verdict")
    add("")

    if not d["total_checked"]:
        add("NO ROWS WITH A CHECKER VERDICT IN THIS WINDOW.")
        add("")
        add("This is not the same as 'the Checker approved nothing'. It means")
        add("no row in the window carries a verdict at all — check the window")
        add("bounds and that this is the right database before concluding.")
        add("")
        add(INTERPRETATION)
        return "\n".join(L)

    total = d["total_checked"]
    add("-" * 72)
    add("VERDICT DISTRIBUTION")
    add("-" * 72)
    add(f"{'verdict':<16}{'count':>10}{'share':>10}{'mean conf':>12}{'median':>10}")
    for v in d["verdicts"]:
        n = d["overall"][v]
        c = d["confidence"][v]
        mean = f"{c['mean']:.3f}" if c["mean"] is not None else "n/a"
        med = f"{c['median']:.3f}" if c["median"] is not None else "n/a"
        add(f"{v:<16}{n:>10}{100*n/total:>9.1f}%{mean:>12}{med:>10}")
    add("")

    approve = d["overall"].get("approve", 0)
    add(f"APPROVAL RATE: {approve}/{total} = {100*approve/total:.1f}%")
    add("")

    def table(title, mapping, keycol):
        add("-" * 72)
        add(title)
        add("-" * 72)
        verdicts = d["verdicts"]
        head = f"{keycol:<22}" + "".join(f"{v:>12}" for v in verdicts) + f"{'total':>9}{'appr%':>8}"
        add(head)
        for k in sorted(mapping, key=lambda x: -sum(mapping[x].values())):
            counts = mapping[k]
            tot = sum(counts.values())
            ap = counts.get("approve", 0)
            row = f"{k[:21]:<22}" + "".join(f"{counts.get(v, 0):>12}" for v in verdicts)
            row += f"{tot:>9}{100*ap/tot:>7.1f}%"
            add(row)
        add("")

    table("BY MARKET FAMILY", d["by_family"], "family")
    table("BY DAY (UTC)", d["by_day"], "day")
    table("BY CATEGORY", d["by_category"], "category")

    add("-" * 72)
    add("WHAT HAPPENED TO THESE ROWS DOWNSTREAM")
    add("-" * 72)
    for a in sorted(d["action_taken"], key=lambda x: -d["action_taken"][x]):
        n = d["action_taken"][a]
        add(f"  {a:<24}{n:>8}{100*n/total:>8.1f}%")
    add("")
    add(INTERPRETATION)
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only report on what the Checker decided.")
    ap.add_argument("--db", required=True,
                    help="path to a COPY of the ledger database")
    ap.add_argument("--since", default=None, help="ISO8601 window start")
    ap.add_argument("--until", default=None, help="ISO8601 window end")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--out", default=None, help="write here instead of stdout")
    args = ap.parse_args(argv)

    try:
        conn = open_readonly(args.db)
    except FileNotFoundError:
        print(f"error: no such database: {args.db}", file=sys.stderr)
        print("       This report needs the ledger file. It will not guess.",
              file=sys.stderr)
        return 2
    except sqlite3.Error as e:
        print(f"error: cannot open database read-only: {e}", file=sys.stderr)
        return 2

    try:
        data = collect(conn,
                       since=parse_iso(args.since) if args.since else None,
                       until=parse_iso(args.until) if args.until else None)
    except sqlite3.Error as e:
        print(f"error: cannot read database: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    text = json.dumps(data, indent=2, sort_keys=True) if args.json else render(data)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
