#!/usr/bin/env python3
"""BRTI 1-second index series from the Polymarket US Asset Prices API.

INDEX DISCIPLINE: the settlement and fair-value index is CF Benchmarks BRTI
ONLY. Nothing in this module reads Binance, Coinbase, or Chainlink. Those
are diagnostics-only inputs elsewhere and can never become S or O.

Official statistic (docs.polymarket.us/api-reference/asset-prices/overview):
  priceToBeat     = mean of the 60 one-second closes in [Tstart-60, Tstart-1]
  settlementPrice = mean of the 60 one-second closes in [Tend-60, Tend-1]
both rounded to 2 decimals. The reading AT the boundary does NOT count; the
reading 60s before it does. NOTE: docs.polymarket.us claims [T-59, T] and
gives a worked example for it, but live published values match [T-60, T-1]
and MISS [T-59, T] (verified 2026-09-27, 2 windows, exact 2-decimal
matches). Trust the venue, not the docs page.

CORRECTION (Claude v4, 2026-10-13): the values THIS MODULE reconstructs
from the US Asset Prices API are NOT verified -- median $4.20 off the
venue's published settlementPrice. Do not use this module's output for
settlement or fair value. (The index itself IS BRTI: P1 verified 491/491
exact at $0.00 against Kalshi KXBTC15M expiration_value. It is this
module's locally reconstructed 1s series -- subject to venue revisions
and feed lag -- that runs off.) If D7 lands (Kalshi authenticated
cfbenchmarks_value feed), replace this module's averages with the feed's
own averages.
"""
from common import get_json, GATEWAY

BASE = GATEWAY + "/v1/asset-prices/history"
# Docs cap: 3600 candles per call -> 1 hour max at 1S buckets.
MAX_1S_SPAN = 3600


def fetch_1s(t_from, t_to):
    """Return sorted [(ts:int, close:float)] of 1s BRTI candles in [t_from, t_to].

    Raises RuntimeError if the API is unreachable (never silently substitutes
    another index).
    """
    t_from, t_to = int(t_from), int(t_to)
    out = []
    cur = t_from
    # Note: the API rejects from==to, so the loop keeps chunk_end > cur.
    while cur < t_to:
        chunk_end = min(cur + MAX_1S_SPAN - 1, t_to)
        url = (f"{BASE}?assets=crypto:btc&from={cur}&to={chunk_end}"
               f"&bucket=ASSET_PRICE_BUCKET_1S")
        d = get_json(url, timeout=45)
        for s in d.get("series", []):
            if s.get("asset", {}).get("symbol") == "btc":
                for c in s.get("candles", []):
                    # tickCount==0 buckets are gap-filled (close carried
                    # forward); the venue's own average includes them, so
                    # we keep them exactly as published.
                    out.append((int(c["timestamp"]), float(c["close"])))
        cur = chunk_end + 1
    # Guard: a degenerate single-point range [x, x] can never be fetched
    # (API requires from < to); callers needing one print should widen.
    out.sort()
    return out


def fetch_1s_with_tickcount(t_from, t_to, timeout=45, retries=2):
    """Like fetch_1s but returns [(ts:int, close:float, tickCount:int|None)].

    Additive (2026-10-08): fetch_1s and avg60 are untouched. Used only for
    research diagnostics (gap-filled-candle share); never for signals.

    timeout/retries are keyword-only tuning knobs (2026-10-08, per
    Claude's third review) so the diagnostics path can bound its own
    fetch without moving the defaults for anyone else: the defaults
    stay timeout=45, retries=2 exactly as before.
    """
    t_from, t_to = int(t_from), int(t_to)
    out = []
    cur = t_from
    while cur < t_to:
        chunk_end = min(cur + MAX_1S_SPAN - 1, t_to)
        url = (f"{BASE}?assets=crypto:btc&from={cur}&to={chunk_end}"
               f"&bucket=ASSET_PRICE_BUCKET_1S")
        d = get_json(url, timeout=timeout, retries=retries)
        for s in d.get("series", []):
            if s.get("asset", {}).get("symbol") == "btc":
                for c in s.get("candles", []):
                    # tickCount==0 buckets are gap-filled (close carried
                    # forward); the venue's own average includes them.
                    out.append((int(c["timestamp"]), float(c["close"]),
                                c.get("tickCount")))
        cur = chunk_end + 1
    out.sort()
    return out


def avg60(candles, T):
    """Official 60-print average for window start T.

    VERIFIED AGAINST LIVE VENUE (2026-09-27, 2 windows, exact 2-decimal
    matches): the venue averages the 60 prints in [T-60, T-1] -- i.e. the
    half-open interval [T-60, T). NOTE: docs.polymarket.us claims [T-59, T]
    and gives a worked example for it, but live published priceToBeat values
    match [T-60, T-1] and MISS [T-59, T] (off by ~$0.06-$0.28). Trust the
    venue, not the docs page.

    Returns float, or None if any of the 60 prints is missing.
    """
    by_ts = {ts: c for ts, c in candles}
    vals = [by_ts[t] for t in range(T - 60, T) if t in by_ts]
    if len(vals) < 60:
        return None
    return sum(vals) / 60.0


def avg60_live(T):
    """Fetch-then-average convenience wrapper. Returns float or None."""
    return avg60(fetch_1s(T - 60, T - 1), T)


def latest_close(t_to=None):
    """Latest complete 1s BRTI close at/before t_to (default: now)."""
    import time
    t_to = int(t_to or time.time())
    rows = fetch_1s(t_to - 5, t_to)
    return rows[-1][1] if rows else None
