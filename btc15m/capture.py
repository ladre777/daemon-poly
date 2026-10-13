#!/usr/bin/env python3
"""Capture the live window's published priceToBeat.

The venue exposes no archive of settled windows (search only indexes live
events; /v1/markets/{old-slug} errors), so we record each window's published
priceToBeat while it is live and score our reproduction later in replay.py.

Cron-safe and idempotent: run every ~5-15 min; each window is captured once.
  */5 * * * * /home/hatch/workspace/polymarket-env/bin/python /home/hatch/workspace/polymarket/btc15m/capture.py >> /home/hatch/workspace/polymarket/btc15m/capture.log 2>&1
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import append_jsonl, now_s
from markets import window_start, get_event, parse_terms, event_slug

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.join(HERE, "captures.jsonl")


def main():
    T = window_start(now_s())
    seen = set()
    try:
        with open(CAPTURES) as f:
            for line in f:
                try:
                    seen.add(json.loads(line).get("T"))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    if T in seen:
        print(f"already captured {event_slug(T)}")
        return
    try:
        ev = get_event(T)
    except Exception as e:
        print(f"get_event failed: {e}")
        return
    if not ev or not ev.get("markets"):
        print(f"window {event_slug(T)} not listed yet")
        return
    terms = parse_terms(ev["markets"][0])
    if terms["index"] != "BRTI":
        print(f"window {event_slug(T)} index={terms['index']} (expected BRTI), skipping")
        return
    if terms["priceToBeat"] is None:
        print(f"window {event_slug(T)} listed but priceToBeat not yet published")
        return
    append_jsonl(CAPTURES, {"T": T, "slug": event_slug(T),
                            "published_priceToBeat": terms["priceToBeat"],
                            "captured_at": now_s()})
    print(f"captured {event_slug(T)} ptb={terms['priceToBeat']}")


if __name__ == "__main__":
    main()
