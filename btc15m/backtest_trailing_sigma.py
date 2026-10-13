#!/usr/bin/env python3
"""Task A: back-test the fair-value model on the 548 settled windows with
trailing-only sigma.

Replicates fairvalue.py exactly (phi, p_up, sigma = max(2h, 15m) realized
vol per sqrt-second from 1s log returns, floor 1e-5), but from the cached
Phase-3 candles (cells_candles.json) so the experiment is fully offline and
reproducible.

Decision points: tau=300 (t_now=Te-300) and tau=120 (t_now=Te-120).
  S   = latest complete 1s BRTI close at t_now (tau > 59, matches select_S).
  sig = max(realized_sigma([t_now-7200, t_now]),
            realized_sigma([t_now-900,  t_now]), 1e-5).
  p   = phi(ln(S/O) / (sig * sqrt(tau))).
Outcome: 1 if venue winner == Up else 0 (from cells_markets.json).

Look-ahead audit (be honest):
  * S and sigma use ONLY candles with ts <= t_now. No future prints.
  * BRTI gap-fill: the API carries forward closes on tickCount==0 buckets;
    the cached candles include them exactly as published, same as live.
  * UNAVOIDABLE SELECTION BIAS: the 548-window set and the tau choices
    were made with knowledge of this data (Phase-3 cell work). This is a
    reliability table of the fair-value model, not an out-of-sample proof.
  * The engine's live sigma comes from the same estimator, so the sigma
    discipline ports 1:1. The one live difference: real ticks evaluate at
    arbitrary taus, not exactly 300/120.
  * No asks are recorded in cells_markets.json, so per-bucket edge is
    reported as (a) realized frequency with Wilson 95% CI, and (b) the
    breakeven ask at which fee-aware EV = 0, solved numerically.

Usage: python3 backtest_trailing_sigma.py
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CANDLES = os.path.join(HERE, "cells_candles.json")
MARKETS = os.path.join(HERE, "cells_markets.json")

SIGMA_FLOOR = 1e-5


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_sigma(by_ts, t_end, lookback):
    """Per-sqrt-second vol from 1s log returns in (t_end-lookback, t_end].
    Returns None if <60 returns (mirrors fairvalue.py)."""
    rets = []
    prev = None
    for t in range(t_end - lookback + 1, t_end + 1):
        c = by_ts.get(t)
        if c is None or c <= 0:
            prev = None
            continue
        if prev is not None and prev > 0:
            rets.append(math.log(c / prev))
        prev = c
    if len(rets) < 60:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def estimate_sigma(by_ts, t_now):
    s_long = realized_sigma(by_ts, t_now, 7200)
    s_short = realized_sigma(by_ts, t_now, 900)
    cands = [s for s in (s_long, s_short) if s]
    if not cands:
        return None
    return max(max(cands), SIGMA_FLOOR)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def taker_fee(a):
    return 0.07 * a * (1 - a)


def breakeven_ask(freq):
    """Ask a where EV(freq, a) = 0, i.e. freq*(1-a)-(1-freq)*a-fee(a)=0."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        ev = freq * (1 - mid) - (1 - freq) * mid - taker_fee(mid)
        if ev > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def main():
    candles = {int(k): v for k, v in json.load(open(CANDLES)).items()}
    mkts = json.load(open(MARKETS))
    t_min = min(candles)
    rows = []
    skipped = 0
    for slug, v in mkts.items():
        if not v.get("resolved") or v["open"] is None \
                or v["winner"] not in ("Up", "Down"):
            skipped += 1
            continue
        # recover T from slug suffix "YYYY-MM-DD-HHMMz"
        try:
            import re
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})z$", slug)
            y, mo, d, hh, mm = map(int, m.groups())
            from datetime import datetime, timezone
            T = int(datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp())
        except Exception:
            skipped += 1
            continue
        Te, O = T + 900, v["open"]
        oc = 1.0 if v["winner"] == "Up" else 0.0
        for tau in (300, 120):
            t_now = Te - tau
            if t_now - 7200 < t_min:
                skipped += 1
                continue
            S = candles.get(t_now) or candles.get(t_now - 1)
            if not S:
                skipped += 1
                continue
            sig = estimate_sigma(candles, t_now)
            if not sig or tau <= 0:
                skipped += 1
                continue
            p = phi(math.log(S / O) / (sig * math.sqrt(tau)))
            rows.append({"tau": tau, "p": p, "oc": oc, "slug": slug})

    print(f"scored rows: {len(rows)} (tau300 + tau120), skipped: {skipped}")

    buckets = [("<0.50", 0.0, 0.50), ("0.50-0.60", 0.50, 0.60),
               ("0.60-0.70", 0.60, 0.70), ("0.70-0.80", 0.70, 0.80),
               ("0.80-0.90", 0.80, 0.90), ("0.90-1.00", 0.90, 1.001)]
    out = {"windows_scored": len(rows) // 2, "tables": {}}
    for tau in (300, 120):
        print(f"\n=== reliability table, tau={tau} ===")
        print(f"{'bucket':>10} {'n':>5} {'mean_p':>7} {'realized':>9} "
              f"{'95% CI':>17} {'breakeven_ask':>13}")
        tbl = []
        for name, lo, hi in buckets:
            sub = [r for r in rows if r["tau"] == tau and lo <= r["p"] < hi]
            n = len(sub)
            if not n:
                print(f"{name:>10} {0:>5} {'-':>7} {'-':>9}")
                tbl.append({"bucket": name, "n": 0})
                continue
            mp = sum(r["p"] for r in sub) / n
            k = sum(1 for r in sub if r["oc"] == 1.0)
            freq = k / n
            w0, w1 = wilson(k, n)
            be = breakeven_ask(freq)
            print(f"{name:>10} {n:>5} {mp:>7.3f} {freq:>8.1%} "
                  f"[{w0:.1%}-{w1:.1%}] {be:>12.2f}")
            tbl.append({"bucket": name, "n": n, "mean_p": round(mp, 4),
                        "realized": round(freq, 4),
                        "ci95": [round(w0, 4), round(w1, 4)],
                        "breakeven_ask": round(be, 4)})
        out["tables"][str(tau)] = tbl

    with open(os.path.join(HERE, "backtest_results.json"), "w") as f:
        json.dump(out, f, indent=1)
    print("\nwrote backtest_results.json")


if __name__ == "__main__":
    main()
