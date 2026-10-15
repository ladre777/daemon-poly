#!/usr/bin/env python3
"""Registered SECONDARY analysis for the sigma shadow: venue labels.

Reported only. Never gates anything, never writes engine state.

Primary (research/shadow_decision.py, sha256 b15e3e79...) scores the
grid-300 briers against the s_avg label (record field "outcome").
This script re-scores the SAME evaluation windows against the venue
label and runs the SAME decision logic on them.

  venue label  lv(T) = 1.0 if next_pb(T) >= pb(T) else 0.0   (tie -> Up)
    next_pb(T) = published priceToBeat of window T+900  (captures.jsonl)
    pb(T)      = published priceToBeat of window T      (captures.jsonl);
                 if missing, the record's O (flagged "our_O_proxy").

  Probabilities are recovered exactly from the stored briers:
    for a 0/1 label o, brier b = (p - o)^2, so p = |o - sqrt(b)|.
  Rule (b) z uses next_pb as the settle reference (the venue's
  settlement price) instead of the record's settle_price.

Modes
  --check   counts only, safe at any time (no Brier means, CIs or z):
            evaluation-set size, labelled windows, label disagreements,
            and the Check-2 recompute: grid-300 brier_h60 / brier_f3
            recomputed from S, O, sigma_<cand>, tau_actual and "outcome"
            via p = Phi(ln(S/O) / (sigma*sqrt(tau))).
  (default) the secondary result. Refuses unless the PRIMARY verdict is
            already latched (research/shadow_verdict.json), so the
            secondary can never be read before the primary. Writes
            research/shadow_secondary_venue.json once; refuses to
            overwrite.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import shadow_decision as sd  # noqa: E402

PRIMARY_SHA256 = ("b15e3e797036327e1f632739094e64f14724e2336f1efb6fb88798af"
                  "a1ffc0df")
OUT_PATH = os.path.join(HERE, "shadow_secondary_venue.json")
G = "300"
TOL = 1e-9


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def load_captures(path):
    """T -> published priceToBeat (last record wins for a repeated T)."""
    pb = {}
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            v = r.get("published_priceToBeat")
            if r.get("T") is None or v is None:
                continue
            pb[int(r["T"])] = float(v)
    return pb


def load_scored(path):
    out = []
    with open(path) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    # same filter as sd.load_shadow(hours=0)
    return [r for r in out
            if (r.get("shadow") or {}).get("p_h60") is not None
            and (r.get("shadow") or {}).get("p_f3") is not None]


def venue_label(r, pb):
    """(label, next_pb, proxy_flag) or (None, None, None) if unlabelled."""
    T = int(r["T"])
    nxt = pb.get(T + 900)
    if nxt is None:
        return None, None, None
    start = pb.get(T)
    proxy = False
    if start is None:
        if r.get("O") is None:
            return None, None, None
        start, proxy = float(r["O"]), True
    return (1.0 if round(nxt, 2) >= round(start, 2) else 0.0), nxt, proxy


def relabel(r, lv, nxt):
    """Copy of r with grid-300 briers re-scored on lv, settle = next_pb."""
    o = float(r["outcome"])
    g = dict((r.get("tau_grid_scores") or {})[G])
    for k in ("brier_model", "brier_h60", "brier_f3"):
        p = abs(o - math.sqrt(g[k]))
        g[k] = (p - lv) ** 2
    q = dict(r)
    q["tau_grid_scores"] = dict(r["tau_grid_scores"])
    q["tau_grid_scores"][G] = g
    q["settle_price"] = nxt
    q["outcome"] = lv
    return q


def decide_on(ev):
    """sd.decide2's body on a given evaluation set, without the
    MIN_COMPLETE gate (the secondary set may be smaller than 300 if some
    windows have no next_pb; n is reported next to the result).
    Kept line-for-line with sd.decide2; test_shadow_secondary checks
    equality with sd.decide2 on a 300-window set."""
    g = sd._grid_rows(ev)
    mean = {c: sum(r["brier_" + c] for r in g) / len(g)
            for c in sd.CANDIDATES}
    ci = {c: sd._block_ci([r["brier_" + c] - r["brier_model"] for r in g])
          for c in sd.CANDIDATES}
    best = min(sd.CANDIDATES, key=lambda c: mean[c])
    lo, hi = sd._block_ci([r["brier_h60"] - r["brier_f3"] for r in g])
    if lo <= 0 <= hi:
        best = "f3"
    detail = {"n": len(g), "mean": mean, "ci_vs_live": ci,
              "ci_h60_minus_f3": (lo, hi), "best": best}
    if not ci[best][1] < 0:
        return f"HOLD ({best}: CI not below 0)", "rule (a) failed", detail
    zscale = sd._robust_zscale(sd._grid_zs(ev, best))
    detail["zscale"] = zscale
    if not 0.8 <= zscale <= 1.25:
        return (f"HOLD ({best}: z-scale {zscale:.2f})", "rule (b) failed",
                detail)
    return f"ADOPT {best}", "passed (a) and (b)", detail


def check_counts(ev, pb):
    """Counts only. No Brier means, CIs or z values."""
    c = {"eval_n": len(ev), "distinct_T": len({r["T"] for r in ev}),
         "labelled": 0, "unlabelled": 0, "pb_proxy": 0,
         "label_disagree": 0, "h60_match": 0, "f3_match": 0,
         "model_match_at_floor": 0, "outcome_not_01": 0}
    for r in ev:
        o = r.get("outcome")
        if o not in (0.0, 1.0):
            c["outcome_not_01"] += 1
            continue
        g = r["tau_grid_scores"][G]
        x = math.log(g["S"] / r["O"])
        st = math.sqrt(g["tau_actual"])
        for cand in ("h60", "f3"):
            p = _phi(x / (g[f"sigma_{cand}"] * st))
            if abs((p - o) ** 2 - g[f"brier_{cand}"]) <= TOL:
                c[f"{cand}_match"] += 1
        p = _phi(x / (1e-5 * st))
        if abs((p - o) ** 2 - g["brier_model"]) <= TOL:
            c["model_match_at_floor"] += 1
        lv, _, proxy = venue_label(r, pb)
        if lv is None:
            c["unlabelled"] += 1
            continue
        c["labelled"] += 1
        c["pb_proxy"] += int(proxy)
        c["label_disagree"] += int(lv != o)
    return c


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scored", required=True, help="scored_windows.jsonl")
    ap.add_argument("--captures", required=True, help="captures.jsonl")
    ap.add_argument("--check", action="store_true",
                    help="counts only (safe at any time)")
    a = ap.parse_args(argv)

    got = _sha256(sd.__file__)
    if got != PRIMARY_SHA256:
        print(f"REFUSE: shadow_decision.py sha256 {got} != pinned "
              f"{PRIMARY_SHA256}")
        return 2

    pb = load_captures(a.captures)
    ev = sd._eval_recs(load_scored(a.scored))
    counts = check_counts(ev, pb)
    print("secondary (venue labels) counts:", json.dumps(counts))

    if a.check:
        print("(check mode: counts only)")
        return 0
    if not sd._load_latched():
        print("REFUSE: primary verdict not latched yet; the secondary is "
              "read only after the primary (run --check for counts)")
        return 3
    if len(ev) < sd.MIN_COMPLETE:
        print(f"REFUSE: primary evaluation set {len(ev)} < "
              f"{sd.MIN_COMPLETE}")
        return 3
    if os.path.exists(OUT_PATH):
        print(f"REFUSE: {OUT_PATH} exists; the secondary is run once")
        return 4

    rel = []
    for r in ev:
        lv, nxt, _ = venue_label(r, pb)
        if lv is None or r.get("outcome") not in (0.0, 1.0):
            continue
        rel.append(relabel(r, lv, nxt))
    verdict, why, detail = decide_on(rel)
    print(f"SECONDARY (venue labels, reported only, n={detail['n']}): "
          f"{verdict} ({why})")
    print(json.dumps(detail, default=str))
    payload = {"verdict": verdict, "why": why, "detail": detail,
               "counts": counts, "primary_sha256": PRIMARY_SHA256,
               "evaluated_at": int(time.time())}
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, default=str)
    os.replace(tmp, OUT_PATH)
    print(f"(written once to {OUT_PATH})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
