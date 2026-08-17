#!/usr/bin/env python3
"""
Sanitized read-only summary of a demo data-collection run.

Written for the Monday review of a weekend spent in KALSHI_ENV=demo with
DRY_RUN=true. It answers "what did the bot actually see and decide" without
handing anyone a file full of model prose or credentials.

Three properties it is built to guarantee, because an export utility that
quietly does otherwise is worse than none:

1. **Read-only.** The database is opened through a `file:...?mode=ro` URI, so
   SQLite itself refuses writes. It never opens the live path by default —
   copy the file first. A summariser that takes a write lock on the ledger
   the daemon is using can stall the very collection it is reporting on.
2. **Sanitized.** Free-text columns (maker_reasoning, checker_reasoning,
   kill_switch_reason, last_error) are *never* emitted. They contain model
   output, and model output can contain anything. Only their presence is
   counted. Nothing here reads the environment, so no key can leak.
3. **Honest about absence.** A missing table or an empty run reports as zero
   with a note, not as a silent omission. "No trades" and "no data" look
   identical in a summary that only prints what it found.

Usage::

    # take a copy first — never point this at the file the daemon is writing
    python scripts/weekend_export.py --db ./ledger-copy.db
    python scripts/weekend_export.py --db ./ledger-copy.db --json > summary.json

Exit codes: 0 on success, 2 if the database cannot be read.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

#: Columns that carry model prose or operator-facing error text. Counted,
#: never printed. Listed by name rather than filtered by heuristic so that
#: adding a text column to the schema is a deliberate decision here.
REDACTED_COLUMNS = frozenset({
    "maker_reasoning", "checker_reasoning", "kill_switch_reason", "last_error",
})


def _ts(value) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        )
    except (ValueError, OSError, OverflowError):
        return "—"


def open_readonly(path: str) -> sqlite3.Connection:
    """Open strictly read-only, so this cannot alter or lock what it reports."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _one(conn, sql: str, default=0):
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return default
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def collect(conn) -> dict:
    """Build the summary. Every query is a SELECT."""
    present = _tables(conn)
    out: dict = {"tables_present": sorted(present), "missing_tables": []}

    for expected in ("edges", "orders", "fills", "settlements", "bot_state"):
        if expected not in present:
            out["missing_tables"].append(expected)

    # -- decision funnel ---------------------------------------------------
    if "edges" in present:
        out["edges"] = {
            "total": _one(conn, "SELECT COUNT(*) FROM edges"),
            "first_at": _ts(_one(conn, "SELECT MIN(created_at) FROM edges", None)),
            "last_at": _ts(_one(conn, "SELECT MAX(created_at) FROM edges", None)),
            "by_verdict": {
                r["checker_verdict"] or "<none>": r["n"]
                for r in conn.execute(
                    "SELECT checker_verdict, COUNT(*) AS n FROM edges "
                    "GROUP BY checker_verdict ORDER BY n DESC"
                )
            },
            "by_action": {
                r["action_taken"] or "<none>": r["n"]
                for r in conn.execute(
                    "SELECT action_taken, COUNT(*) AS n FROM edges "
                    "GROUP BY action_taken ORDER BY n DESC"
                )
            },
            "by_source": {
                r["source"] or "<none>": r["n"]
                for r in conn.execute(
                    "SELECT source, COUNT(*) AS n FROM edges GROUP BY source"
                )
            },
            "top_categories": {
                (r["category"] or "<none>"): r["n"]
                for r in conn.execute(
                    "SELECT category, COUNT(*) AS n FROM edges "
                    "GROUP BY category ORDER BY n DESC LIMIT 10"
                )
            },
            "edge_size": {
                "min": _one(conn, "SELECT MIN(edge_size) FROM edges", None),
                "avg": _one(conn, "SELECT AVG(edge_size) FROM edges", None),
                "max": _one(conn, "SELECT MAX(edge_size) FROM edges", None),
            },
            "checker_confidence_avg": _one(
                conn, "SELECT AVG(checker_confidence) FROM edges", None
            ),
            # Counted, never shown — see REDACTED_COLUMNS.
            "rows_with_maker_reasoning": _one(
                conn, "SELECT COUNT(*) FROM edges WHERE maker_reasoning IS NOT NULL"
            ),
            "rows_with_checker_reasoning": _one(
                conn, "SELECT COUNT(*) FROM edges "
                      "WHERE checker_reasoning IS NOT NULL"
            ),
            "settled": _one(conn, "SELECT COUNT(*) FROM edges WHERE settled = 1"),
        }

    # -- orders / fills ----------------------------------------------------
    if "orders" in present:
        out["orders"] = {
            "total": _one(conn, "SELECT COUNT(*) FROM orders"),
            "dry_run": _one(conn, "SELECT COUNT(*) FROM orders WHERE dry_run = 1"),
            "live": _one(conn, "SELECT COUNT(*) FROM orders WHERE dry_run = 0"),
            "by_state": {
                r["state"]: r["n"]
                for r in conn.execute(
                    "SELECT state, COUNT(*) AS n FROM orders GROUP BY state"
                )
            },
            # The safety-critical number: anything here means the exchange may
            # hold an order the bot cannot see.
            "unresolved_unknown": _one(
                conn, "SELECT COUNT(*) FROM orders WHERE state = 'unknown'"
            ),
            "with_errors": _one(
                conn, "SELECT COUNT(*) FROM orders WHERE last_error IS NOT NULL"
            ),
        }
    if "fills" in present:
        out["fills"] = {"total": _one(conn, "SELECT COUNT(*) FROM fills")}
    if "settlements" in present:
        out["settlements"] = {
            "total": _one(conn, "SELECT COUNT(*) FROM settlements"),
            "realized_pnl_total": _one(
                conn, "SELECT SUM(realized_pnl) FROM settlements", None
            ),
        }

    # -- kill switch -------------------------------------------------------
    if "bot_state" in present:
        out["kill_switch"] = {
            "tripped": bool(_one(
                conn, "SELECT kill_switch_tripped FROM bot_state WHERE id = 1"
            )),
            "tripped_at": _ts(_one(
                conn, "SELECT kill_switch_tripped_at FROM bot_state WHERE id = 1",
                None,
            )),
            "reason_present": bool(_one(
                conn,
                "SELECT kill_switch_reason IS NOT NULL FROM bot_state WHERE id = 1",
            )),
        }

    # -- reconciliation ----------------------------------------------------
    if "account_snapshot" in present:
        out["account_snapshot"] = {
            "rows": _one(conn, "SELECT COUNT(*) FROM account_snapshot"),
            "last_reconciled_at": _ts(_one(
                conn, "SELECT MAX(reconciled_at) FROM account_snapshot", None
            )),
        }
    if "signal_alerts" in present:
        out["signal_alerts"] = {
            "distinct_signals": _one(conn, "SELECT COUNT(*) FROM signal_alerts"),
            "total_alerts": _one(
                conn, "SELECT SUM(alert_count) FROM signal_alerts", 0
            ),
        }
    if "schema_version" in present:
        out["schema_version"] = _one(conn, "SELECT MAX(version) FROM schema_version",
                                     None)
    return out


def render(summary: dict) -> str:
    lines = ["DÆMON-KALSHI — sanitized run summary",
             "=" * 44, ""]

    if summary.get("missing_tables"):
        lines += [f"MISSING TABLES: {', '.join(summary['missing_tables'])}",
                  "(reported rather than skipped — an absent table and an "
                  "empty one are different findings)", ""]

    e = summary.get("edges")
    if e:
        lines += [
            "DECISIONS",
            f"  edges logged        : {e['total']}",
            f"  window              : {e['first_at']} -> {e['last_at']}",
            f"  checker verdicts    : {e['by_verdict'] or '—'}",
            f"  action taken        : {e['by_action'] or '—'}",
            f"  by maker source     : {e['by_source'] or '—'}",
            f"  settled             : {e['settled']}",
        ]
        avg = e["checker_confidence_avg"]
        lines.append(f"  avg checker conf    : "
                     f"{avg:.3f}" if isinstance(avg, float) else
                     "  avg checker conf    : —")
        es = e["edge_size"]
        if isinstance(es.get("avg"), float):
            lines.append(f"  edge size min/avg/max: {es['min']:.4f} / "
                         f"{es['avg']:.4f} / {es['max']:.4f}")
        lines += ["  top categories      :"]
        for cat, n in (e["top_categories"] or {}).items():
            lines.append(f"      {cat:<28} {n}")
        lines += [
            f"  (reasoning text withheld: {e['rows_with_maker_reasoning']} maker, "
            f"{e['rows_with_checker_reasoning']} checker rows carry prose)",
            "",
        ]

    o = summary.get("orders")
    if o is not None:
        lines += [
            "ORDERS",
            f"  total               : {o['total']}  (dry_run {o['dry_run']}, "
            f"live {o['live']})",
            f"  by state            : {o['by_state'] or '—'}",
            f"  UNRESOLVED UNKNOWN  : {o['unresolved_unknown']}"
            + ("   <-- INVESTIGATE" if o["unresolved_unknown"] else ""),
            f"  rows with an error  : {o['with_errors']}",
            "",
        ]
        if o["live"]:
            lines += ["  NOTE: live (non-dry-run) orders exist in this ledger. "
                      "Expected to be 0 for a demo/dry-run collection run.", ""]

    for key, label in (("fills", "FILLS"), ("settlements", "SETTLEMENTS")):
        block = summary.get(key)
        if block is not None:
            lines.append(f"{label}: {block}")
    if summary.get("fills") is not None:
        lines.append("")

    ks = summary.get("kill_switch")
    if ks:
        lines += [
            "KILL SWITCH",
            f"  tripped             : {ks['tripped']}",
            f"  tripped at          : {ks['tripped_at']}",
            f"  reason recorded     : {ks['reason_present']} (text withheld)",
            "",
        ]

    acct = summary.get("account_snapshot")
    if acct:
        lines += ["RECONCILIATION",
                  f"  snapshot rows       : {acct['rows']}",
                  f"  last reconciled     : {acct['last_reconciled_at']}",
                  ""]

    sa = summary.get("signal_alerts")
    if sa:
        lines += ["ALERTS",
                  f"  distinct signals    : {sa['distinct_signals']}",
                  f"  alerts sent         : {sa['total_alerts']}",
                  ""]

    if summary.get("schema_version") is not None:
        lines.append(f"schema_version: {summary['schema_version']}")

    lines += ["",
              "No credentials, environment variables, or model text are read "
              "or emitted by this script."]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Sanitized read-only summary of a DÆMON-KALSHI ledger.",
    )
    parser.add_argument(
        "--db", required=True,
        help="path to a COPY of the ledger database (never the live file)",
    )
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of the text report")
    args = parser.parse_args(argv)

    try:
        conn = open_readonly(args.db)
    except FileNotFoundError:
        print(f"No such database: {args.db}", file=sys.stderr)
        return 2
    except sqlite3.Error as e:
        print(f"Could not open {args.db} read-only: {e}", file=sys.stderr)
        return 2

    try:
        summary = collect(conn)
    finally:
        conn.close()

    print(json.dumps(summary, indent=2, default=str) if args.json
          else render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
