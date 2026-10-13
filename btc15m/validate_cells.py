#!/usr/bin/env python3
"""Phase 3 pilot: re-measure the edge-note lead-persistence cells on BRTI.

Published cells (Chainlink TWAP prints, Binance 1s proxy) to port:
  1. 5-10 bp lead, 5 min left, calm hour    -> 90.6% hold
  2. 5-10 bp lead, 5 min left, volatile hr  -> 80.1% hold (negative EV)
  3. 10-15 bp lead, 2 min left              -> 97.8% hold

Method per window T (Te = T + 900):
  - venue truth from the settled market record: priceToBeat, winner
    (Up/Down). Requires Phase 2's market-record path to work.
  - lead_bp(t) = (twap60(t) - priceToBeat) / priceToBeat * 1e4, where
    twap60 is the same 60-print statistic as settlement (brti.avg60).
    Measured at Te-300 (5 min left) and Te-120 (2 min left).
  - hold = the leading side wins: (lead>0 and winner==Up) or
    (lead<0 and winner==Down).
  - regime: std of 1-min log returns over [Te-3660, Te-60) in bp;
    calm if < sample median (reported; notes cite 3.9 bp on Chainlink).

For each cell: n, holds, hold rate, Wilson 95% CI, fee-aware EV/share at
the cell's typical ask (80c cells 1-2, 90c cell 3) with taker_fee_per_share,
and the breakeven hold rate (ask + fee).

Verdict per cell: PORTS (|rate-published|<=5pp and EV>0), RECALIBRATE
(shape holds, level differs), REJECT (rate<=breakeven or shape breaks).

Caches: cells_candles.json (BRTI 1s), cells_markets.json (venue truth) --
re-runs are free.

Usage: python3 validate_cells.py [--windows N]   (default 150)
"""
import json
import math
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import get_json, GATEWAY, taker_fee_per_share
from brti import fetch_1s, avg60
from markets import event_slug, market_slug

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.join(HERE, "captures.jsonl")
CANDLE_CACHE = os.path.join(HERE, "cells_candles.json")
MARKET_CACHE = os.path.join(HERE, "cells_markets.json")

PUBLISHED = {
    "cell1": {"desc": "5-10bp lead, 5min left, calm hr", "rate": 0.906,
              "ask": 0.80},
    "cell2": {"desc": "5-10bp lead, 5min left, volatile hr", "rate": 0.801,
              "ask": 0.80},
    "cell3": {"desc": "10-15bp lead, 2min left", "rate": 0.978, "ask": 0.90},
}


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def ev_per_share(hold_rate, ask):
    fee = taker_fee_per_share(ask)
    return hold_rate * (1 - ask) - (1 - hold_rate) * ask - fee


def breakeven(ask):
    return ask + taker_fee_per_share(ask)


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def get_market_cached(slug, cache):
    if slug in cache:
        return cache[slug]
    q = urllib.parse.quote("cpc-" + slug)
    try:
        d = get_json(f"{GATEWAY}/v1/markets?slug={q}", timeout=30)
    except Exception:
        return None
    ms = d.get("markets", [])
    if not ms:
        return None
    m = ms[0]
    apt = m.get("assetPriceTerms") or {}

    def val(x):
        v = (x or {}).get("value")
        return float(v) if v not in (None, "") else None

    def jload(x):
        try:
            v = json.loads(x or "[]")
            return v if isinstance(v, list) else []
        except Exception:
            return []

    outcomes = jload(m.get("outcomes"))
    op = jload(m.get("outcomePrices"))
    winner = None
    if outcomes and op and len(outcomes) == len(op):
        try:
            winner = outcomes[op.index(max(op, key=float))]
        except Exception:
            winner = None
    rec = {
        "open": val(apt.get("priceToBeat")),
        "close": val(apt.get("settlementPrice")),
        "winner": {"Yes": "Up", "No": "Down"}.get(winner, winner),
        "resolved": m.get("status") == "MARKET_STATUS_RESOLVED",
    }
    cache[slug] = rec
    return rec


def main(argv):
    n_win = int(argv[0]) if argv else 150
    caps, seen = [], set()
    with open(CAPTURES) as f:
        for line in f:
            line = line.strip()
            if line:
                c = json.loads(line)
                if c["T"] not in seen:
                    seen.add(c["T"])
                    caps.append(c)
    caps.sort(key=lambda c: c["T"])
    caps = caps[-n_win:]
    Ts = [c["T"] for c in caps]
    print(f"pilot windows: {len(Ts)}")

    # --- BRTI candles (cached) ---
    candles = {int(k): v for k, v in load_json(CANDLE_CACHE).items()}
    need_lo = min(Ts) - 3660
    need_hi = max(Ts) + 899
    missing = need_lo < min(candles, default=need_lo + 1) or \
        need_hi > max(candles, default=need_hi - 1)
    if missing or not candles:
        print("fetching BRTI 1s history ...")
        rows = fetch_1s(need_lo, need_hi)
        for ts, cl in rows:
            candles[ts] = cl
        with open(CANDLE_CACHE, "w") as f:
            json.dump({str(k): v for k, v in candles.items()}, f)
        print(f"candles cached: {len(candles)}")
    else:
        print(f"candles from cache: {len(candles)}")

    # --- venue truth per window (cached) ---
    mcache = load_json(MARKET_CACHE)
    clist = sorted(candles.items())  # avg60 wants [(ts, close)]
    dirty = False
    rows = []
    for i, c in enumerate(caps):
        T, Te = c["T"], c["T"] + 900
        slug = c["slug"]
        v = get_market_cached(slug, mcache)
        dirty = True
        if i % 25 == 0:
            time.sleep(0.2)
        if not v or not v["resolved"] or v["open"] is None \
                or v["winner"] not in ("Up", "Down"):
            continue
        # lead via twap60 at Te-300 and Te-120
        l5t, l2t = Te - 300, Te - 120
        t5 = avg60(clist, l5t)
        t2 = avg60(clist, l2t)
        if t5 is None or t2 is None:
            continue
        lead5 = (t5 - v["open"]) / v["open"] * 1e4
        lead2 = (t2 - v["open"]) / v["open"] * 1e4
        # regime: 1-min closes over [Te-3660, Te-60)
        closes = [candles.get(t) for t in range(Te - 3660, Te - 59, 60)]
        if any(x is None for x in closes) or len(closes) < 61:
            continue
        rets = [math.log(closes[j + 1] / closes[j]) for j in range(60)]
        mu = sum(rets) / 60
        vol_bp = math.sqrt(sum((r - mu) ** 2 for r in rets) / 60) * 1e4
        rows.append({"T": T, "slug": slug, "lead5": lead5, "lead2": lead2,
                     "vol_bp": vol_bp, "winner": v["winner"]})
    if dirty:
        with open(MARKET_CACHE, "w") as f:
            json.dump(mcache, f)
    print(f"scored windows: {len(rows)}")
    if len(rows) < 30:
        print("too few scored windows for a read; aborting")
        return

    vols = sorted(r["vol_bp"] for r in rows)
    med = vols[len(vols) // 2]
    print(f"BRTI trailing-1h vol median: {med:.2f} bp "
          f"(notes cite 3.9 bp on Chainlink)")
    for r in rows:
        r["calm"] = r["vol_bp"] < med

    def hold(lead, winner):
        return (lead > 0 and winner == "Up") or (lead < 0 and winner == "Down")

    def cell(sub, leadkey):
        n = len(sub)
        k = sum(1 for r in sub if hold(r[leadkey], r["winner"]))
        return n, k

    b1 = [r for r in rows if 5 <= abs(r["lead5"]) <= 10 and r["calm"]]
    b2 = [r for r in rows if 5 <= abs(r["lead5"]) <= 10 and not r["calm"]]
    b3 = [r for r in rows if 10 <= abs(r["lead2"]) <= 15]
    # adjacent buckets for shape context
    adj = {
        "0-5bp @5min ": ([r for r in rows if 0 < abs(r["lead5"]) < 5], "lead5"),
        "15-20bp @2m ": ([r for r in rows if 15 < abs(r["lead2"]) <= 20], "lead2"),
    }
    buckets = {"cell1": (b1, "lead5"), "cell2": (b2, "lead5"),
               "cell3": (b3, "lead2")}

    print("\n=== cell re-measurement (BRTI) vs published (Chainlink) ===")
    results = {}
    for key, (bucket, leadkey) in buckets.items():
        pub = PUBLISHED[key]
        n, k = cell(bucket, leadkey)
        rate = k / n if n else 0.0
        lo, hi = wilson(k, n)
        ev = ev_per_share(rate, pub["ask"])
        be = breakeven(pub["ask"])
        results[key] = (rate, ev)
        print(f"\n{key} {pub['desc']}")
        print(f"  published hold : {pub['rate']:.1%}")
        print(f"  BRTI hold      : {k}/{n} = {rate:.1%}  (95% CI {lo:.1%}-{hi:.1%})")
        print(f"  fee-aware EV   : {ev:+.4f}/share @ {pub['ask']*100:.0f}c ask "
              f"(breakeven hold {be:.1%})")

    print("\n--- shape context (adjacent buckets) ---")
    for name, (bucket, leadkey) in adj.items():
        n, k = cell(bucket, leadkey)
        rate = k / n if n else 0.0
        lo, hi = wilson(k, n)
        print(f"  {name}: {k}/{n} = {rate:.1%}  (95% CI {lo:.1%}-{hi:.1%})")

    print("\n=== verdicts ===")
    for key, (bucket, _leadkey) in buckets.items():
        pub = PUBLISHED[key]
        rate, ev = results[key]
        n = len(bucket)
        be = breakeven(pub["ask"])
        if n < 10:
            v = "INSUFFICIENT DATA (n<10) -- widen the pilot"
        elif key == "cell2":
            # fade cell: the published finding IS negative EV
            if abs(rate - pub["rate"]) <= 0.05:
                v = "PORTS as a fade (confirms: do not trade this setup)"
            elif ev > 0:
                v = ("RECALIBRATE -- volatile cell shows positive EV on "
                     "BRTI, investigate before trusting")
            else:
                v = "REJECT (shape differs from published)"
        elif abs(rate - pub["rate"]) <= 0.05 and ev > 0:
            v = "PORTS"
        elif rate > be:
            v = "RECALIBRATE (holds edge but level differs -- refit on BRTI)"
        else:
            v = "REJECT"
        print(f"  {key}: {v}")


if __name__ == "__main__":
    main(sys.argv[1:])
