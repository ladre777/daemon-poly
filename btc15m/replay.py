#!/usr/bin/env python3
"""Phase 1 gate: score BRTI reproduction against captured published values.

For each window in captures.jsonl (recorded live by capture.py), recompute
avg60(T) from BRTI 1s history and compare to the published priceToBeat.
A window MATCHES when round(recomputed,2) == round(published,2).

GATE: >=50 scored windows AND >=98% match rate.
NOTE: the venue publishes no archive of settlementPrice values, so the
close-average is validated by the identical statistic (same avg60 code path);
only the open-average has a published reference per window.

Usage: python replay.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from brti import fetch_1s, avg60
from markets import event_slug

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.join(HERE, "captures.jsonl")


def main():
    caps = []
    try:
        with open(CAPTURES) as f:
            for line in f:
                line = line.strip()
                if line:
                    caps.append(json.loads(line))
    except FileNotFoundError:
        pass
    # de-dupe by T, keep first
    seen, uniq = set(), []
    for c in caps:
        if c["T"] not in seen:
            seen.add(c["T"])
            uniq.append(c)
    print(f"captured windows: {len(uniq)}")
    if not uniq:
        print("no captures yet -- run capture.py while windows are live")
        sys.exit(2)

    t_min = min(c["T"] for c in uniq) - 60
    t_max = max(c["T"] for c in uniq)
    print(f"fetching 1s BRTI [{t_min} .. {t_max}] ...")
    candles = fetch_1s(t_min, t_max)
    print(f"candles: {len(candles)}")

    n = m = 0
    misses = []
    for c in uniq:
        T, pub = c["T"], c["published_priceToBeat"]
        v = avg60(candles, T)
        n += 1
        if v is None:
            misses.append((c["slug"], "missing prints"))
            continue
        if round(v, 2) == round(pub, 2):
            m += 1
        else:
            misses.append((c["slug"], f"recomputed {v:.2f} vs published {pub:.2f}"))

    rate = m / n if n else 0
    print(f"\nwindows scored : {n}")
    print(f"exact matches  : {m}/{n} ({100.0*rate:.1f}%)")
    for slug, why in misses[:10]:
        print(f"  MISS {slug}: {why}")

    gate = (n >= 50) and (rate >= 0.98)
    print(f"\nGATE (n>=50 and rate>=98%): {'PASS' if gate else 'FAIL'}")
    if n < 50:
        print(f"  -> {50-n} more captured windows needed (capture.py accumulates ~4/hour)")
    sys.exit(0 if gate else 1)


if __name__ == "__main__":
    main()
