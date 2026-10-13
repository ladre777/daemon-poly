#!/usr/bin/env python3
"""Phase 2: validate BRTI settlement reproduction against venue truth.

For each captured window T (from captures.jsonl):
  - venue open   = market.assetPriceTerms.priceToBeat.value
  - venue close  = market.assetPriceTerms.settlementPrice.value
  - venue winner = outcomePrices mapped through outcomes ([Up, Down])
  - ours: avg60(T) and avg60(Te) from BRTI 1s history (the [T-60, T-1]
    statistic Phase 1 already validated on the open)
  - our winner: round(close,2) >= round(open,2) -> Up (tie -> Up)

A window MATCHES when both 2-decimal prices agree and the winner agrees.
Near-ties (|close-open| < $0.01 after rounding) are reported separately --
they are the critical test of the tie rule.

GATE: >=100 windows scored, >=98% close-price match, 100% winner agreement
      outside near-ties.

Usage: python3 validate_settlement.py [--limit N]
"""
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import get_json, GATEWAY
from brti import fetch_1s, avg60
from markets import event_slug, market_slug

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.join(HERE, "captures.jsonl")


def get_market(T):
    slug = urllib.parse.quote(market_slug(T))
    try:
        d = get_json(f"{GATEWAY}/v1/markets?slug={slug}", timeout=30)
    except Exception:
        return None
    ms = d.get("markets", [])
    return ms[0] if ms else None


def parse_market(m):
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
    # venue labels Yes/No map to Up/Down on this series
    if winner == "Yes":
        winner = "Up"
    elif winner == "No":
        winner = "Down"
    return {
        "open": val(apt.get("priceToBeat")),
        "close": val(apt.get("settlementPrice")),
        "winner": winner,
        "outcomes": outcomes,
        "resolved": m.get("status") == "MARKET_STATUS_RESOLVED",
    }


def main(argv):
    limit = int(argv[0]) if len(argv) > 0 else 0
    caps, seen = [], set()
    with open(CAPTURES) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            if c["T"] not in seen:
                seen.add(c["T"])
                caps.append(c)
    caps.sort(key=lambda c: c["T"])
    if limit:
        caps = caps[-limit:]
    print(f"windows: {len(caps)} (oldest {caps[0]['slug']})")

    t_min = min(c["T"] for c in caps) - 60
    t_max = max(c["T"] for c in caps) + 900
    print("fetching BRTI 1s history ...")
    candles = fetch_1s(t_min, t_max)
    print(f"candles: {len(candles)}")

    n = n_close = n_res = n_win = 0
    near_ties = []
    misses = []
    for i, c in enumerate(caps):
        T, Te = c["T"], c["T"] + 900
        m = get_market(T)
        if i % 25 == 0:
            time.sleep(0.2)
        if not m:
            misses.append((c["slug"], "no market record"))
            continue
        v = parse_market(m)
        if v["open"] is None or v["close"] is None:
            misses.append((c["slug"], "market not settled yet"))
            continue
        o60, c60 = avg60(candles, T), avg60(candles, Te)
        if o60 is None or c60 is None:
            misses.append((c["slug"], "missing BRTI prints"))
            continue
        n += 1
        open_ok = round(o60, 2) == round(v["open"], 2)
        close_ok = round(c60, 2) == round(v["close"], 2)
        if close_ok:
            n_close += 1
        our_winner = "Up" if round(c60, 2) >= round(o60, 2) else "Down"
        # winner agreement only meaningful on resolved markets
        if v["resolved"] and v["winner"]:
            n_res += 1
            win_ok = v["winner"] == our_winner
            if win_ok:
                n_win += 1
        else:
            win_ok = True  # not scored
        if abs(round(c60, 2) - round(o60, 2)) < 0.01:
            near_ties.append((c["slug"], o60, c60, v["open"], v["close"],
                              our_winner, v["winner"]))
        if not (open_ok and close_ok and win_ok):
            misses.append((c["slug"],
                           f"open {o60:.2f}/{v['open']:.2f} "
                           f"close {c60:.2f}/{v['close']:.2f} "
                           f"winner {our_winner}/{v['winner']}"))

    print(f"\nscored           : {n}")
    print(f"close-price match: {n_close}/{n} ({100.0*n_close/n:.1f}%)" if n else "")
    print(f"winner agreement : {n_win}/{n_res} ({100.0*n_win/n_res:.1f}%)"
          if n_res else "winner agreement : n/a (no resolved markets)")
    print(f"near-ties        : {len(near_ties)}")
    for slug, o60, c60, vo, vc, ow, vw in near_ties[:10]:
        print(f"  TIE {slug}: ours {o60:.2f}->{c60:.2f} "
              f"venue {vo:.2f}->{vc:.2f} winner {ow}/{vw}")
    print(f"misses           : {len(misses)}")
    for slug, why in misses[:10]:
        print(f"  MISS {slug}: {why}")

    gate = n >= 100 and (n_close / n >= 0.98) and n_res > 0 and (n_win == n_res)
    print(f"\nGATE (n>=100, close>=98%, winners 100% of resolved): "
          f"{'PASS' if gate else 'FAIL'}")


if __name__ == "__main__":
    main(sys.argv[1:])
