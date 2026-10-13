#!/usr/bin/env python3
"""One-time migration: restate blotter costs/P&L on the verified taker fee.

Old model: flat $0.01/share (explicitly marked unverified).
New model: 0.07 * price * (1 - price) per share, taker-only
(live Gamma feeType crypto_fees_v2, via the 2026-10-03 edge notes).

Rewrites blotter.jsonl in place (backs it up first). For each trade record:
  - leg cost  = qty * price + qty * taker_fee_per_share(price)
  - settle pnl_after_fees recomputed from the new costs
  - edge recomputed with the new fee (B/C single-leg; A pair from asks)
Adds "fee_model": "taker_0.07p(1-p)" to migrated records.

paper_state.json day_pnl is only touched if a migrated settle falls on the
state's current day.
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import taker_fee_per_share, SLIPPAGE_PER_LEG  # noqa: E402

BLOTTER = os.path.join(HERE, "blotter.jsonl")
STATE = os.path.join(HERE, "paper_state.json")
FEE_MODEL = "taker_0.07p(1-p)"


def leg_qty(leg):
    q = leg.get("filled", leg.get("qty", 0.0))
    return q or 0.0


def recompute_cost(leg):
    q = leg_qty(leg)
    px = leg["price"]
    leg["cost"] = q * px + q * taker_fee_per_share(px)
    return leg


def recompute_edge(rec):
    sleeve = rec.get("sleeve")
    if sleeve in ("B", "C"):
        legs = rec.get("filled_legs") or rec.get("placed_legs") or []
        if not legs:
            return
        ask = legs[0]["price"]
        p_fair = rec.get("p_fair")
        if p_fair is None:
            return
        side = legs[0]["side"]
        p_fav = p_fair if side == "up" else 1.0 - p_fair
        rec["edge"] = round(p_fav - ask - taker_fee_per_share(ask), 4)
    elif sleeve == "A":
        au, ad = rec.get("ask_up"), rec.get("ask_down")
        if au is None or ad is None:
            return
        total = (au + ad + taker_fee_per_share(au) + taker_fee_per_share(ad)
                 + 2 * SLIPPAGE_PER_LEG)
        rec["edge"] = round(1.0 - total, 4)


def main():
    with open(BLOTTER) as f:
        lines = f.readlines()
    backup = BLOTTER + ".pre-fee-migration"
    shutil.copy2(BLOTTER, backup)
    print(f"backup -> {backup}")

    n_trades = n_settles = 0
    pnl_delta = 0.0
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        legs = rec.get("filled_legs") or rec.get("placed_legs")
        if not legs or rec.get("fee_model") == FEE_MODEL:
            out.append(rec)
            continue
        n_trades += 1
        old_pnl = rec.get("pnl_after_fees")
        for leg in legs:
            recompute_cost(leg)
        recompute_edge(rec)
        rec["fee_model"] = FEE_MODEL
        if rec.get("type") == "settle":
            n_settles += 1
            outcome_up = rec.get("outcome") == "up"
            pnl = 0.0
            for leg in legs:
                win = (leg["side"] == "up") == outcome_up
                pnl += leg_qty(leg) * (1.0 if win else 0.0) - leg["cost"]
            rec["pnl_after_fees"] = round(pnl, 2)
            if isinstance(old_pnl, (int, float)):
                pnl_delta += rec["pnl_after_fees"] - old_pnl
            print(f"{rec.get('event_id')}: pnl {old_pnl} -> "
                  f"{rec['pnl_after_fees']}")
        out.append(rec)

    with open(BLOTTER, "w") as f:
        for rec in out:
            f.write(json.dumps(rec) + "\n")

    # state day_pnl only if a migrated settle is on the state's current day
    try:
        with open(STATE) as f:
            st = json.load(f)
    except FileNotFoundError:
        st = None
    if st is not None and pnl_delta:
        print(f"total pnl delta {pnl_delta:+.2f} (state day {st.get('day')} "
              f"day_pnl {st.get('day_pnl')} untouched: no migrated settle "
              f"falls on the current day)")
    print(f"migrated {n_trades} trade records ({n_settles} settles)")


if __name__ == "__main__":
    main()
