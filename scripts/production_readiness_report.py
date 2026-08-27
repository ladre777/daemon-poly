#!/usr/bin/env python3
"""
Read-only production-readiness and evidence report.

Consumes the local ledger and emits an operator-readable Markdown report plus
machine-readable JSON. It never contacts Kalshi, never reads a credential,
and cannot write to the database — SQLite itself refuses, because the file is
opened through a ``file:...?mode=ro`` URI.

Usage::

    python -m scripts.production_readiness_report --db ./ledger-copy.db
    python -m scripts.production_readiness_report --db ./ledger-copy.db --json
    python -m scripts.production_readiness_report --db ./ledger-copy.db \\
        --as-of 2026-08-27T21:00:00Z --include-refused

``--include-refused`` is opt-in on purpose. Refused rows are the most
quotable numbers in the ledger and describe trades that never happened; the
2026-08 window's headline ``+$87.79`` is one of them. Making the section
require a flag means nobody produces it by accident and then pastes the
total somewhere it will be read as profit.

As with ``scripts/weekend_export.py``: take a copy of the database first.
Never point this at the file a running daemon is writing.

Exit codes: 0 on success, 2 if the database cannot be read.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

# Import path fix for direct execution (``python scripts/x.py``) as well as
# module execution (``python -m scripts.x``). The repo's other scripts assume
# the latter; supporting both costs three lines and removes a papercut.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.reasons import Mode  # noqa: E402
from reporting import evidence as ev  # noqa: E402
from reporting import readiness as rd  # noqa: E402
from reporting import render  # noqa: E402


def open_readonly(path: str) -> sqlite3.Connection:
    """Open strictly read-only, so this cannot alter or lock what it reports."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_iso(s: str) -> float:
    """ISO8601 to epoch. Accepts a trailing Z, which ``fromisoformat`` does
    not on older interpreters and which every operator types."""
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


def _providers_seen(conn) -> str:
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "provider_calls" not in names:
        return ""
    rows = conn.execute(
        """SELECT role, provider, model, COUNT(*) AS n
           FROM provider_calls GROUP BY role, provider, model
           ORDER BY n DESC LIMIT 8""").fetchall()
    return ", ".join(
        f"{r['role']}:{r['provider']}/{r['model'] or '?'}×{r['n']}" for r in rows)


def build_payload(conn, *, as_of: float, since: float = None,
                  until: float = None, include_refused: bool = False,
                  min_fill_sample: int = ev.DEFAULT_MIN_FILL_SAMPLE,
                  min_calibration_sample: int = ev.DEFAULT_MIN_CALIBRATION_SAMPLE,
                  dry_run: bool = None) -> dict:
    """Assemble every section. Pure: reads the connection, mutates nothing."""
    live = ev.live_evidence(conn, since=since, until=until)
    paper = ev.counterfactual_evidence(conn, Mode.PAPER, since=since, until=until)
    cells = ev.calibration(conn, since=since, until=until,
                           min_sample=min_calibration_sample)
    report = rd.assess(conn, live_ev=live, calibration_cells=cells,
                       dry_run=dry_run, min_fill_sample=min_fill_sample,
                       now=as_of)

    meta = {
        "as_of_iso": _iso(as_of),
        "cutoff_iso": _iso(until if until is not None else as_of),
        "since_iso": _iso(since),
        "until_iso": _iso(until),
        "db": conn.execute("PRAGMA database_list").fetchone()[2] or "(memory)",
        "providers": _providers_seen(conn),
        "min_fill_sample": min_fill_sample,
        "min_calibration_sample": min_calibration_sample,
        "generated_by": "scripts/production_readiness_report.py",
        "contacts_network": False,
        "writes_database": False,
    }
    for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"):
        if row[0] == "pass_telemetry":
            p = conn.execute(
                "SELECT dry_run, order_strategy FROM pass_telemetry "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
            if p:
                meta["dry_run"] = (None if p["dry_run"] is None
                                   else bool(p["dry_run"]))
                meta["order_strategy"] = p["order_strategy"]
            break
    if dry_run is not None:
        meta["dry_run"] = dry_run

    payload = {
        "meta": meta,
        "live": ev.to_dict(live),
        "paper": ev.to_dict(paper),
        "calibration": [ev.to_dict(c) for c in cells],
        "readiness": report.as_dict(),
    }
    if include_refused:
        payload["refused"] = ev.to_dict(
            ev.counterfactual_evidence(conn, Mode.REFUSED, since=since, until=until))
    else:
        payload["refused_omitted"] = (
            "Aggregate refused counterfactuals are opt-in behind "
            "--include-refused. They describe trades that never happened.")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only DÆMON-KALSHI evidence and readiness report.")
    parser.add_argument("--db", required=True,
                        help="path to a COPY of the ledger database")
    parser.add_argument("--as-of", default=None,
                        help="ISO8601 report timestamp (default: now)")
    parser.add_argument("--since", default=None,
                        help="ISO8601 window start (default: unbounded)")
    parser.add_argument("--until", default=None,
                        help="ISO8601 window end (default: unbounded)")
    parser.add_argument("--include-refused", action="store_true",
                        help="include the aggregate refused counterfactual "
                             "section (opt-in; these are not results)")
    parser.add_argument("--min-fill-sample", type=int,
                        default=ev.DEFAULT_MIN_FILL_SAMPLE,
                        help="fill-verified observations required before any "
                             "fee-net conclusion (report parameter only)")
    parser.add_argument("--min-calibration-sample", type=int,
                        default=ev.DEFAULT_MIN_CALIBRATION_SAMPLE,
                        help="calibration cells below this get a warning")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of Markdown")
    parser.add_argument("--out", default=None,
                        help="write to this path instead of stdout")
    args = parser.parse_args(argv)

    try:
        conn = open_readonly(args.db)
    except FileNotFoundError:
        print(f"error: no such database: {args.db}", file=sys.stderr)
        return 2
    except sqlite3.Error as e:
        print(f"error: cannot open database read-only: {e}", file=sys.stderr)
        return 2

    try:
        payload = build_payload(
            conn,
            as_of=_parse_iso(args.as_of) if args.as_of else time.time(),
            since=_parse_iso(args.since) if args.since else None,
            until=_parse_iso(args.until) if args.until else None,
            include_refused=args.include_refused,
            min_fill_sample=args.min_fill_sample,
            min_calibration_sample=args.min_calibration_sample,
        )
    except sqlite3.Error as e:
        print(f"error: cannot read database: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    text = (render.to_json(payload) if args.json
            else render.to_markdown(payload))
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
