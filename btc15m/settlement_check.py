#!/usr/bin/env python3
"""Task C: extend the settlement-rule validation to 30+ windows, including
near-zero-lead windows, and test whether BRTI mismatches cluster near the
strike (where trades happen).

Offline: uses cells_markets.json (venue truth: open/close/winner) and
cells_candles.json (BRTI 1s). No network.

Per window: o60 = avg60(T) [half-open T-60,T-1], c60 = avg60(Te).
  close match : round(c60,2) == round(venue_close,2)
  winner      : ours = Up iff round(c60,2) >= round(o60,2)  (tie -> Up)
  lead        : |c60 - venue_open|  ("near the strike" = small lead)
"""
import json
import math
import os
import re
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CANDLES = os.path.join(HERE, "cells_candles.json")
MARKETS = os.path.join(HERE, "cells_markets.json")


def avg60(by_ts, T):
    vals = [by_ts.get(t) for t in range(T - 60, T)]
    if any(v is None for v in vals):
        return None
    return sum(vals) / 60.0


def slug_to_T(slug):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})z$", slug)
    y, mo, d, hh, mm = map(int, m.groups())
    return int(datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp())


def main():
    candles = {int(k): v for k, v in json.load(open(CANDLES)).items()}
    mkts = json.load(open(MARKETS))
    n = n_close = n_win = n_tie = tie_venue_up = 0
    mismatches, matches_lead = [], []
    near = {"< $1": [], "< $5": [], ">= $5": []}
    for slug, v in sorted(mkts.items()):
        if not v.get("resolved") or v["open"] is None or v["close"] is None \
                or v["winner"] not in ("Up", "Down"):
            continue
        T = slug_to_T(slug)
        Te = T + 900
        o60, c60 = avg60(candles, T), avg60(candles, Te)
        if o60 is None or c60 is None:
            continue
        n += 1
        vo, vc, vw = v["open"], v["close"], v["winner"]
        close_ok = round(c60, 2) == round(vc, 2)
        open_ok = round(o60, 2) == round(vo, 2)
        ours = "Up" if round(c60, 2) >= round(o60, 2) else "Down"
        win_ok = (ours == vw)
        if close_ok:
            n_close += 1
        if win_ok:
            n_win += 1
        lead = abs(c60 - vo)  # dollars from the strike
        rec = {"slug": slug, "lead": lead, "close_ok": close_ok,
               "open_ok": open_ok, "win_ok": win_ok,
               "ours": (round(o60, 2), round(c60, 2)),
               "venue": (vo, vc, vw)}
        if close_ok and win_ok and open_ok:
            matches_lead.append(lead)
        else:
            mismatches.append(rec)
        key = "< $1" if lead < 1 else ("< $5" if lead < 5 else ">= $5")
        near[key].append(close_ok and win_ok and open_ok)
        if round(c60, 2) == round(o60, 2):
            n_tie += 1
            if vw == "Up":
                tie_venue_up += 1

    print(f"scored: {n}")
    print(f"open match : {sum(1 for r in mismatches if r['open_ok']) + len(matches_lead)}/{n}")
    print(f"close match: {n_close}/{n} = {n_close/n:.2%}")
    print(f"winner agree: {n_win}/{n} = {n_win/n:.2%}")
    full = n - len(mismatches)
    print(f"full match (open+close+winner): {full}/{n} = {full/n:.2%}")
    print(f"near-ties (|round(c60)-round(o60)|<0.01): {n_tie}, "
          f"venue called Up on {tie_venue_up}/{n_tie}")
    print("\n--- mismatch rate by lead from strike ---")
    for key, lst in near.items():
        if lst:
            mr = 1 - sum(lst) / len(lst)
            print(f"  lead {key:>6}: n={len(lst):>4}  mismatch rate {mr:.2%}")
    print("\n--- lead distribution: matches vs mismatches ---")
    mm = sorted(m["lead"] for m in mismatches)
    ma = sorted(matches_lead)
    def pct(v, q):
        return v[int(q * (len(v) - 1))] if v else float("nan")
    print(f"  matches   (n={len(ma)}): median ${pct(ma,.5):.2f}  "
          f"p10 ${pct(ma,.1):.2f}  p90 ${pct(ma,.9):.2f}")
    print(f"  mismatches(n={len(mm)}): median ${pct(mm,.5):.2f}  "
          f"p10 ${pct(mm,.1):.2f}  p90 ${pct(mm,.9):.2f}")
    print("\n--- first 15 mismatches ---")
    for r in mismatches[:15]:
        print(f"  {r['slug']}: lead ${r['lead']:.2f} ours {r['ours']} "
              f"venue {r['venue']} open_ok={r['open_ok']} "
              f"close_ok={r['close_ok']} win_ok={r['win_ok']}")

    out = {"n": n, "close_match": n_close, "winner_agree": n_win,
           "full_match": full, "near_ties": n_tie,
           "tie_venue_up": tie_venue_up,
           "by_lead": {k: {"n": len(v), "match_rate": sum(v) / len(v)}
                       for k, v in near.items()},
           "lead_median_match": pct(ma, .5), "lead_median_mismatch": pct(mm, .5),
           "mismatches": mismatches[:50]}
    json.dump(out, open(os.path.join(HERE, "settlement_results.json"), "w"),
              indent=1)
    print("\nwrote settlement_results.json")


if __name__ == "__main__":
    main()
