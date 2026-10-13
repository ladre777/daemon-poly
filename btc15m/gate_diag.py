#!/usr/bin/env python3
"""Gate diagnostic: walk every sleeve sub-gate against the LIVE book.

Read-only. Prints exactly which sub-gate blocks, with values.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import taker_fee_per_share, SLIPPAGE_PER_LEG, now_s
from markets import window_start, get_event, parse_terms, get_book, parse_book, mid_up, event_slug
from fairvalue import fair_snapshot
from paper import (B_ASK_LO, B_ASK_HI, B_EDGE_MIN, B_TAU_LO, B_TAU_HI,
                   B_VETO_EDGE, C_P_HI, C_P_LO, C_ASK_LO, C_ASK_HI, C_TAU_MAX)

now = now_s()
T = window_start(now)
tau = T + 900 - now
slug = event_slug(T)
print(f"window {slug}  tau={tau}s")

ev = get_event(T)
terms = parse_terms(ev["markets"][0])
O = terms["priceToBeat"]
print(f"priceToBeat O={O}")
book = parse_book(get_book(T))
au, ad = book.get("ask_up"), book.get("ask_down")
print(f"ask_up={au}  ask_down={ad}  mid_up={mid_up(book)}")
snap = fair_snapshot(T, O, now)
p = snap["p_up"]
print(f"p_fair={p:.4f}  S={snap['S']:.2f}  sigma={snap['sigma']:.6f}" if p else "p_fair=None")

print("\n--- Sleeve A: pair capture ---")
if au and ad:
    fee_pair = taker_fee_per_share(au[0]) + taker_fee_per_share(ad[0])
    total = au[0] + ad[0] + fee_pair + 2 * SLIPPAGE_PER_LEG
    print(f"ask_up+ask_down+costs = {au[0]:.4f}+{ad[0]:.4f}+{fee_pair+2*SLIPPAGE_PER_LEG:.4f} = {total:.4f}  (need < 1.00)")
    print("BLOCKED: pair sum >= 1.00" if total >= 1.00 else "WOULD PASS")
else:
    print("BLOCKED: missing book side")

print("\n--- Sleeve B: favorite stale quote ---")
print(f"tau {tau}s in [{B_TAU_LO},{B_TAU_HI}]: {'PASS' if B_TAU_LO <= tau <= B_TAU_HI else 'BLOCKED'}")
if au and ad and p is not None:
    fav = "up" if au[0] > ad[0] else "down"  # favorite = pricier ask (mirrors paper.py)
    ask_fav = au[0] if fav == "up" else ad[0]
    print(f"favorite={fav} ask_fav={ask_fav:.4f} in [{B_ASK_LO},{B_ASK_HI}]: {'PASS' if B_ASK_LO <= ask_fav <= B_ASK_HI else 'BLOCKED'}")
    model_up = p > 0.5
    agree = model_up == (fav == "up")
    p_fav = p if fav == "up" else 1.0 - p
    print(f"model p_up={p:.4f} agrees with book fav: {'PASS' if agree else 'BLOCKED (sign disagreement)'}")
    fee = taker_fee_per_share(ask_fav)
    edge = p_fav - ask_fav - fee
    print(f"edge = {p_fav:.4f}-{ask_fav:.4f}-{fee:.4f} = {edge:.4f} (need >= {B_EDGE_MIN}): {'PASS' if edge >= B_EDGE_MIN else 'BLOCKED'}")
    if edge >= B_VETO_EDGE:
        print(f"NOTE: edge >= {B_VETO_EDGE} -> winner's curse check")

print("\n--- Sleeve C: late certainty (currently OFF) ---")
print(f"tau {tau}s <= {C_TAU_MAX}: {'PASS' if tau <= C_TAU_MAX else 'BLOCKED'}")
if p is not None and au and ad:
    if p >= C_P_HI:
        print(f"p_fair {p:.4f} >= {C_P_HI}: side=up ask={au[0]:.4f} in [{C_ASK_LO},{C_ASK_HI}]: {'PASS' if C_ASK_LO <= au[0] <= C_ASK_HI else 'BLOCKED'}")
    elif p <= C_P_LO:
        print(f"p_fair {p:.4f} <= {C_P_LO}: side=down ask={ad[0]:.4f} in [{C_ASK_LO},{C_ASK_HI}]: {'PASS' if C_ASK_LO <= ad[0] <= C_ASK_HI else 'BLOCKED'}")
    else:
        print(f"BLOCKED: p_fair {p:.4f} not at certainty extremes")
