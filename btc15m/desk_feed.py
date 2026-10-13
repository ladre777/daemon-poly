"""Satoshi Desk feed: compact JSON snapshot of the real BTC 15m engine state.

Read-only. No network except the state files on disk. Output is a small
dict Grok's desk page polls to render a LIVE desk (instead of a static
snapshot).

Usage:
    python3 desk_feed.py            # print snapshot JSON to stdout
    python3 desk_feed.py --write out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import readers  # noqa: E402

WINDOW_SECS = 900


def _slug_for(topen: int) -> str:
    dt = datetime.fromtimestamp(topen, tz=timezone.utc)
    return "btc-updown-15m-" + dt.strftime("%Y-%m-%d-%H%Mz").lower()


def _last_trade():
    """Most recent settled trade, for the desk's 'last print' line."""
    recs = readers._read_jsonl(readers.PAPER_BLOTTER)
    settles = [r for r in recs if r.get("type") == "settle"]
    if not settles:
        return None
    s = settles[-1]
    # find matching open for side/price/edge
    side = price = edge = None
    for r in reversed(recs):
        if r.get("type") == "open" and r.get("event_id") == s.get("event_id"):
            legs = r.get("filled_legs") or r.get("placed_legs") or []
            if legs:
                side = legs[0].get("side")
                price = legs[0].get("filled", legs[0].get("qty")) and legs[0].get("price")
            edge = r.get("edge")
            break
    return {
        "event": s.get("event_id"),
        "sleeve": s.get("sleeve"),
        "side": side,
        "price": price,
        "edge": round(edge, 4) if isinstance(edge, (int, float)) else edge,
        "outcome": s.get("outcome"),
        "pnl": s.get("pnl_after_fees"),
        "settled_utc": datetime.fromtimestamp(
            s.get("ts", 0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _btc_last():
    rows = readers._read_jsonl(os.path.join(HERE, "captures.jsonl"))
    if not rows:
        return {"last_price_to_beat": None, "as_of_utc": None}
    r = rows[-1]
    ts = r.get("captured_at") or r.get("T")
    return {
        "last_price_to_beat": r.get("published_priceToBeat"),
        "as_of_utc": datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ") if ts else None,
    }


def _gate_v2_summary():
    """Gate v2 read (no live import): current-era Sleeve B count + labeled history.

    Headline is the current gate (0/150). The 10 pre-freeze Sleeve B trades
    are labeled history and never counted toward the gate.
    """
    st = readers._load_json(readers.PAPER_STATE, {})
    cur_b = st.get("sig_B")
    # Gate join (LaDre 2026-10-06 review fix, STAGED): count settle records
    # stamped with the CURRENT sig_B, exactly like paper.py --report.
    # Decision records always carry sleeve=None, so the old join
    # (type=="decision" and sleeve=="B") could never match -- the headline
    # would sit at 0/150 forever and real trades would land in history.
    era_b = hist_n = 0
    hist_net = 0.0
    for r in readers._read_jsonl(readers.PAPER_BLOTTER):
        if r.get("type") != "settle" or r.get("sleeve") != "B":
            continue
        pnl = r.get("pnl_after_fees", 0.0) or 0.0
        if cur_b and r.get("sig_B") == cur_b:
            era_b += 1
        else:
            hist_n += 1
            hist_net += pnl
    return {
        "gate": "v2",
        "mode": "PAPER",  # stays paper until LaDre flips it explicitly
        "headline": f"{era_b}/150",
        "sleeve_b_settled_current_era": era_b,
        "sleeve_b_needed": 150,
        "pre_freeze_history": {
            "label": "pre-freeze history — not counted toward gate v2",
            "settled_trades": hist_n,
            "net": round(hist_net, 2),
        },
    }


def build_snapshot() -> dict:
    now = int(time.time())
    cur_open = (now // WINDOW_SECS) * WINDOW_SECS
    next_open = cur_open + WINDOW_SECS

    status = readers.get_status()
    paper = status["paper"]
    live = status["live"]
    pnl = readers.get_pnl()
    recent = readers.get_activity(5)

    mode = "LIVE" if live["live_ok"] and live["open_positions"] else "PAPER"

    return {
        "agent": "satoshi",
        "mode": mode,
        "as_of_utc": datetime.fromtimestamp(
            now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {
            "current": _slug_for(cur_open),
            "current_topen_utc": datetime.fromtimestamp(
                cur_open, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "next_window_utc": datetime.fromtimestamp(
                next_open, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seconds_to_next": next_open - now,
        },
        "btc": _btc_last(),
        "paper": {
            "day": paper["day"],
            "day_pnl": paper["day_pnl"],
            "open_positions": paper["open_positions"],
            "consec_losses": paper["consec_losses"],
            "disabled_sleeves": paper["disabled_sleeves"],
            "decided_windows": paper["decided_windows"],
            "sleeve_pnl": pnl["by_sleeve"],
            "settled_trades": pnl["settled_trades"],
            "net": pnl["net"]["paper"],
        },
        "live": {
            "live_ok": live["live_ok"],
            "day_pnl": live["day_pnl"],
            "open_positions": live["open_positions"],
            "consec_losses": live["consec_losses"],
            "disabled_sleeves": live["disabled_sleeves"],
            "net": pnl["net"]["live"],
        },
        "kill_switches": "armed",
        "kill_triggered": bool(paper["disabled_sleeves"]
                               or paper["consec_losses"] >= 3),
        "last_trade": _last_trade(),
        "recent": recent,
        "gate": _gate_v2_summary(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", default=None, help="write snapshot to file")
    args = ap.parse_args(argv)
    snap = build_snapshot()
    text = json.dumps(snap, separators=(",", ":"))
    if args.write:
        with open(args.write, "w") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
