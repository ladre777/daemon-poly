#!/usr/bin/env python3
"""Phase 2: 1.5-second book snapshotter for the live 15m BTC Up/Down window.

Persistent-loop process (cron is far too coarse for 1-2s polling):
  cd ~/workspace/polymarket/btc15m && \
  nohup ~/workspace/polymarket-env/bin/python snapshot.py >> snapshot.log 2>&1 &

Logs every poll to snapshots.jsonl:
  ts, window T, event slug, ask_up/bid_up/ask_down/bid_down (+sizes),
  empty_book flag, market state, BRTI last close (refreshed ~10s).
Also logs empty-book episodes (the "NO ONE TO TRADE AGAINST" condition)
with start/end and duration.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import append_jsonl, now_s
from markets import window_start, get_book, parse_book, event_slug
from brti import latest_close

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "snapshots.jsonl")
POLL_SEC = 1.5
BRTI_REFRESH_SEC = 10


def snap_record(T, book, brti_px):
    r = {"ts": now_s(), "T": T, "slug": event_slug(T),
         "state": book.get("state"),
         "empty_book": book.get("empty_book"),
         "empty_bids": book.get("empty_bids"),
         "empty_offers": book.get("empty_offers"),
         "brti_last": brti_px}
    for k in ("ask_up", "bid_up", "ask_down", "bid_down"):
        v = book.get(k)
        r[k] = {"px": round(v[0], 4), "size": round(v[1], 2)} if v else None
    return r


def main():
    print(f"snapshotter starting, poll={POLL_SEC}s -> {LOG}", flush=True)
    last_brti = None
    last_brti_t = 0
    ep_start = None   # empty-book episode start ts
    prev_T = None
    while True:
        t0 = time.time()
        try:
            T = window_start(now_s())
            if prev_T is not None and T != prev_T:
                append_jsonl(LOG, {"ts": now_s(), "type": "window_boundary",
                                   "old_T": prev_T, "new_T": T})
            prev_T = T
            if now_s() - last_brti_t >= BRTI_REFRESH_SEC:
                try:
                    last_brti = latest_close()
                except Exception:
                    pass
                last_brti_t = now_s()
            md = get_book(T)
            book = parse_book(md)
            rec = snap_record(T, book, last_brti)
            append_jsonl(LOG, rec)
            if book["empty_book"] and ep_start is None:
                ep_start = rec["ts"]
                append_jsonl(LOG, {"ts": rec["ts"], "type": "empty_book_start",
                                   "T": T, "slug": rec["slug"]})
            elif not book["empty_book"] and ep_start is not None:
                append_jsonl(LOG, {"ts": rec["ts"], "type": "empty_book_end",
                                   "T": T, "duration_s": rec["ts"] - ep_start})
                ep_start = None
        except Exception as e:
            append_jsonl(LOG, {"ts": now_s(), "type": "poll_error",
                               "error": str(e)[:300]})
        dt = time.time() - t0
        time.sleep(max(0.1, POLL_SEC - dt))


if __name__ == "__main__":
    main()
