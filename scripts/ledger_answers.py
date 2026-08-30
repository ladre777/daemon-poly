"""One-shot, read-only report over the edge ledger.

Why this exists
---------------
Three consecutive reviews reduced every open question to the same SQLite file
on the Railway volume, and the review environment cannot reach it: the agent
proxy denies CONNECT to Railway's API, so the CLI cannot authenticate, and no
container-file tool is available in that session. The one channel that does
work in both directions is the deploy log.

So this runs in the container, answers the questions there, and prints the
answers. Nothing here is a feature and nothing here is on the trading path.

Three properties, all deliberate:

* **Read-only, structurally.** The connection is opened with ``mode=ro`` on a
  URI, so SQLite itself refuses a write — the guarantee does not depend on this
  module never issuing one. No schema is created, nothing is migrated, and no
  network client is constructed.
* **Cannot stop the bot.** Every entry point swallows its own exceptions. A
  missing database, a missing column on an older schema, or a corrupt file
  produces a logged line and nothing else. Same posture as ``TelemetryStore``,
  whose constructor disables itself rather than raising.
* **Bounded output.** The per-row export the brief allowed for is deliberately
  not produced. What a cluster-robust statistic actually needs is the *event*
  means, so the aggregation happens here and only the aggregates are emitted.

The statistic
-------------
Every strike on one contract resolves off a single settlement price, and the
same strike is re-priced and re-logged on every pass. Rows are therefore
repeated samples of a small number of events, and a t-statistic computed on
row count is inflated by roughly ``sqrt(rows per event)``.

This reports the clustered form instead: collapse each event to its mean PnL,
then take the standard error across event means. That is the standard
cluster-robust treatment when within-cluster correlation is high, which here it
is by construction.
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
from collections import defaultdict

log = logging.getLogger("daemon_kalshi.ledger_answers")

#: Written as well as logged. The log is what the review session can actually
#: read today; the file is for whoever has a container-file tool tomorrow.
REPORT_PATH = "/data/reports/ledger_answers.txt"

#: A ticker is EVENT-STRIKE. Collapsing on the last hyphen turns
#: KXWTI-26AUG3114-T86.99 into KXWTI-26AUG3114, which is the unit that
#: actually resolves.
def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0] if ticker and "-" in ticker else (ticker or "?")


def _rows(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _clustered(pnls_by_event: dict[str, list[float]]) -> dict:
    """Mean PnL per row, and the standard error across EVENT means.

    Returns naive and clustered standard errors side by side so the inflation
    factor is visible rather than asserted. With one event there is no
    between-cluster variance to estimate and the clustered figures are None —
    an undefined statistic is reported as undefined, not as zero.
    """
    all_pnl = [p for v in pnls_by_event.values() for p in v]
    n_rows, n_ev = len(all_pnl), len(pnls_by_event)
    if not n_rows:
        return {"n_rows": 0, "n_events": 0}
    mean = sum(all_pnl) / n_rows
    out = {"n_rows": n_rows, "n_events": n_ev, "mean_pnl": mean,
           "rows_per_event": n_rows / n_ev}
    if n_rows > 1:
        var = sum((p - mean) ** 2 for p in all_pnl) / (n_rows - 1)
        se = math.sqrt(var / n_rows)
        out["sd_row"] = math.sqrt(var)
        out["se_naive"] = se
        out["t_naive"] = mean / se if se else None
    ev_means = [sum(v) / len(v) for v in pnls_by_event.values()]
    if n_ev > 1:
        em = sum(ev_means) / n_ev
        evar = sum((m - em) ** 2 for m in ev_means) / (n_ev - 1)
        ese = math.sqrt(evar / n_ev)
        out["mean_of_event_means"] = em
        out["sd_event"] = math.sqrt(evar)
        out["se_clustered"] = ese
        out["t_clustered"] = em / ese if ese else None
    return out


def _fmt(v, spec="%.4f"):
    return "n/a" if v is None else (spec % v)


def build_report(db_path: str) -> str:
    lines: list[str] = []
    w = lines.append
    w("=" * 78)
    w("LEDGER ANSWERS  (read-only, generated at startup)")
    w(f"database: {db_path}")
    w("=" * 78)

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        tot = _rows(conn, "SELECT COUNT(*) n, SUM(settled) s FROM edges")[0]
        w(f"\nedges: {tot['n']} rows, {tot['s'] or 0} settled\n")

        # -- Q1 -----------------------------------------------------------
        w("-" * 78)
        w("Q1  FINANCE BY TICKER, AND DISTINCT EVENTS")
        w("-" * 78)
        fin = _rows(conn, """SELECT ticker, COUNT(*) AS rows_n,
                                    SUM(settled) AS settled_rows
                             FROM edges WHERE category = 'Finance'
                             GROUP BY ticker ORDER BY rows_n DESC""")
        w(f"{'ticker':<34}{'rows':>8}{'settled':>10}")
        for r in fin:
            w(f"{r['ticker']:<34}{r['rows_n']:>8}{(r['settled_rows'] or 0):>10}")
        ev = defaultdict(lambda: [0, 0])
        for r in fin:
            e = ev[event_of(r["ticker"])]
            e[0] += r["rows_n"]
            e[1] += r["settled_rows"] or 0
        w(f"\ndistinct Finance STRIKE tickers: {len(fin)}")
        w(f"distinct Finance EVENTS:         {len(ev)}")
        w(f"{'event':<34}{'rows':>8}{'settled':>10}")
        for e, (rn, sn) in sorted(ev.items(), key=lambda kv: -kv[1][0]):
            w(f"{e:<34}{rn:>8}{sn:>10}")
        sett_ev = {e: v for e, v in ev.items() if v[1] > 0}
        w(f"\nFinance events with at least one SETTLED row: {len(sett_ev)}")
        for e, (_, sn) in sorted(sett_ev.items(), key=lambda kv: -kv[1][1]):
            w(f"   {e:<31}{sn:>8} settled")

        # -- Q2 -----------------------------------------------------------
        w("")
        w("-" * 78)
        w("Q2  WHICH CHECKER ARM ON KXWTI   (CHECKER_MIN_CONFIDENCE = 0.65)")
        w("-" * 78)
        q2 = _rows(conn, """SELECT checker_verdict, COUNT(*) AS n,
                                   AVG(checker_confidence)  AS mean_conf,
                                   MIN(checker_confidence)  AS min_conf,
                                   MAX(checker_confidence)  AS max_conf
                            FROM edges WHERE ticker LIKE 'KXWTI-%'
                            GROUP BY checker_verdict""")
        w(f"{'verdict':<14}{'n':>7}{'mean_conf':>12}{'min_conf':>11}{'max_conf':>11}")
        for r in q2:
            w(f"{str(r['checker_verdict']):<14}{r['n']:>7}"
              f"{_fmt(r['mean_conf'], '%.3f'):>12}{_fmt(r['min_conf'], '%.3f'):>11}"
              f"{_fmt(r['max_conf'], '%.3f'):>11}")
        # The arm, stated directly: an approve below the floor is the threshold
        # blocking the trade; a reject is the model disagreeing. Different fixes.
        arm = _rows(conn, """SELECT
              SUM(CASE WHEN checker_verdict='approve' AND checker_confidence >= 0.65
                       THEN 1 ELSE 0 END) AS approve_confident,
              SUM(CASE WHEN checker_verdict='approve' AND checker_confidence <  0.65
                       THEN 1 ELSE 0 END) AS approve_underconfident,
              SUM(CASE WHEN checker_verdict='reject'  THEN 1 ELSE 0 END) AS reject,
              SUM(CASE WHEN checker_verdict='abstain' THEN 1 ELSE 0 END) AS abstain,
              SUM(CASE WHEN checker_verdict IS NULL   THEN 1 ELSE 0 END) AS never_checked,
              COUNT(*) AS total
            FROM edges WHERE ticker LIKE 'KXWTI-%'""")[0]
        w("")
        for k, v in arm.items():
            w(f"   {k:<26}{v if v is not None else 0:>8}")

        # -- Q3 -----------------------------------------------------------
        w("")
        w("-" * 78)
        w("Q3  CLUSTER-ROBUST STATISTICS ON SETTLED ROWS")
        w("-" * 78)
        settled = _rows(conn, """SELECT ticker, category, source, outcome, pnl,
                                        maker_probability, counterfactual_direction,
                                        counterfactual_price_cents, action_taken
                                 FROM edges WHERE settled = 1""")
        w(f"settled rows read: {len(settled)}")

        by_cat: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        yes = defaultdict(int)
        seen = defaultdict(int)
        for r in settled:
            if r["pnl"] is None:
                continue
            c = f"{r['category'] or '?'}/{r['source'] or '?'}"
            by_cat[c][event_of(r["ticker"])].append(float(r["pnl"]))
            seen[c] += 1
            if (r["outcome"] or "").lower() == "yes":
                yes[c] += 1

        w("")
        w(f"{'bucket':<16}{'rows':>7}{'events':>8}{'r/ev':>7}{'mean_pnl':>10}"
          f"{'sd_row':>9}{'t_naive':>9}{'sd_event':>10}{'t_clustered':>13}")
        for c in sorted(by_cat, key=lambda k: -seen[k]):
            s = _clustered(by_cat[c])
            w(f"{c:<16}{s['n_rows']:>7}{s['n_events']:>8}{s['rows_per_event']:>7.1f}"
              f"{s['mean_pnl']:>10.4f}{_fmt(s.get('sd_row')):>9}"
              f"{_fmt(s.get('t_naive'), '%.2f'):>9}{_fmt(s.get('sd_event')):>10}"
              f"{_fmt(s.get('t_clustered'), '%.2f'):>13}")

        w("")
        w("YES / NO resolution counts on settled rows")
        w(f"{'bucket':<16}{'rows':>8}{'YES':>8}{'NO':>8}{'YES rate':>11}")
        for c in sorted(by_cat, key=lambda k: -seen[k]):
            n = seen[c]
            w(f"{c:<16}{n:>8}{yes[c]:>8}{n - yes[c]:>8}{100.0 * yes[c] / n:>10.2f}%")

        # -- Finance detail: the direction question the last review declined --
        w("")
        w("-" * 78)
        w("FINANCE DETAIL  (direction, price, probability, outcome)")
        w("-" * 78)
        fs = [r for r in settled if (r["category"] or "") == "Finance"]
        w(f"settled Finance rows: {len(fs)}")
        if fs:
            dirn = defaultdict(lambda: [0, 0, 0.0])   # n, yes, pnl
            for r in fs:
                d = dirn[(r["counterfactual_direction"] or "unset").lower()]
                d[0] += 1
                if (r["outcome"] or "").lower() == "yes":
                    d[1] += 1
                d[2] += float(r["pnl"] or 0.0)
            w(f"{'direction':<12}{'n':>8}{'YES':>7}{'YES rate':>11}{'total PnL':>12}{'mean PnL':>11}")
            for d, (n, y, p) in sorted(dirn.items()):
                w(f"{d:<12}{n:>8}{y:>7}{100.0*y/n:>10.2f}%{p:>12.2f}{p/n:>11.4f}")
            pr = [float(r["counterfactual_price_cents"]) for r in fs
                  if r["counterfactual_price_cents"] is not None]
            if pr:
                pr.sort()
                w(f"\nentry price (cents): n={len(pr)} min={pr[0]:.1f} "
                  f"p25={pr[len(pr)//4]:.1f} median={pr[len(pr)//2]:.1f} "
                  f"p75={pr[3*len(pr)//4]:.1f} max={pr[-1]:.1f} "
                  f"mean={sum(pr)/len(pr):.2f}")
            mp = defaultdict(int)
            for r in fs:
                if r["maker_probability"] is not None:
                    mp[round(float(r["maker_probability"]) * 100)] += 1
            w(f"\nstated probability histogram ({len(mp)} distinct values):")
            w("   " + "  ".join(f"{k}%:{v}" for k, v in sorted(mp.items())))
            fev = defaultdict(lambda: [0, 0, 0.0])
            for r in fs:
                e = fev[event_of(r["ticker"])]
                e[0] += 1
                if (r["outcome"] or "").lower() == "yes":
                    e[1] += 1
                e[2] += float(r["pnl"] or 0.0)
            w(f"\nsettled Finance rows by EVENT ({len(fev)} events):")
            w(f"   {'event':<31}{'rows':>7}{'YES':>6}{'total PnL':>12}{'mean':>10}")
            for e, (n, y, p) in sorted(fev.items(), key=lambda kv: -kv[1][0]):
                w(f"   {e:<31}{n:>7}{y:>6}{p:>12.2f}{p/n:>10.4f}")
    finally:
        conn.close()

    w("")
    w("=" * 78)
    w("END LEDGER ANSWERS")
    w("=" * 78)
    return "\n".join(lines)


def emit(db_path: str = None) -> None:
    """Build the report, log it, and try to persist it. Never raises.

    Logged line by line rather than as one blob: the log pipeline this is read
    through truncates long single messages, and a report that arrives cut in
    half is worse than one that arrives as many lines.
    """
    try:
        from config import CONFIG
        path = db_path or CONFIG.ledger_db_path
    except Exception:
        path = db_path or os.getenv("LEDGER_DB_PATH", "/data/daemon_kalshi.db")
    try:
        if not os.path.exists(path):
            log.warning("Ledger answers: no database at %s — skipping", path)
            return
        report = build_report(path)
    except Exception:
        log.exception("Ledger answers: could not build the report — continuing")
        return
    for line in report.split("\n"):
        log.info("LEDGER_ANSWERS| %s", line)
    try:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w") as fh:
            fh.write(report)
        log.info("Ledger answers also written to %s", REPORT_PATH)
    except Exception:
        # The log above is the primary channel; the file is a convenience.
        log.warning("Ledger answers: could not write %s (logged above)", REPORT_PATH)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import sys
    emit(sys.argv[1] if len(sys.argv) > 1 else None)
