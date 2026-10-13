#!/usr/bin/env python3
"""Window discovery + order-book parsing for Polymarket US btc-updown-15m.

Venue rule: discover via the US search API with deterministic event slugs
(btc-updown-15m-YYYY-MM-DD-HHMMz). NEVER Gamma unix slugs
(btc-updown-15m-{epoch}) -- those are the global CLOB product.

Book rule: the /v1/markets/{slug}/book endpoint quotes the YES (Up) side as
`bids`/`offers`. NO (Down) prices are the 1-Yes complement:
  ask_down = 1 - best_yes_bid   (size = that bid's qty)
  bid_down = 1 - best_yes_offer  (size = that offer's qty)
"""
import datetime
import time
import urllib.parse

from common import get_json, GATEWAY, WINDOW_SEC


def window_start(ts=None):
    """Floor a unix timestamp to its 15m window start."""
    ts = int(ts or time.time())
    return ts - (ts % WINDOW_SEC)


def event_slug(T):
    dt = datetime.datetime.fromtimestamp(T, tz=datetime.timezone.utc)
    return f"btc-updown-15m-{dt:%Y-%m-%d-%H%M}z"


def market_slug(T):
    return "cpc-" + event_slug(T)


def get_event(T, retries=2, timeout=30):
    """Fetch the event record for window start T. None if not listed."""
    slug = event_slug(T)
    q = urllib.parse.quote(slug)
    d = get_json(f"{GATEWAY}/v1/search?query={q}&limit=10",
                 retries=retries, timeout=timeout)
    for e in d.get("events", []):
        if e.get("slug") == slug:
            return e
    return None


def parse_terms(market):
    """Extract assetPriceTerms. Returns dict with floats or None."""
    apt = market.get("assetPriceTerms") or {}

    def val(x):
        v = (x or {}).get("value")
        return float(v) if v not in (None, "") else None

    return {
        "priceToBeat": val(apt.get("priceToBeat")),
        "settlementPrice": val(apt.get("settlementPrice")),
        "windowStart": apt.get("windowStart"),
        "windowEnd": apt.get("windowEnd"),
        "index": apt.get("indexSymbol"),
        "horizon": apt.get("horizon"),
    }


def get_book(T):
    d = get_json(f"{GATEWAY}/v1/markets/{market_slug(T)}/book")
    return d.get("marketData", {})


def parse_book(md):
    """Parse book into tradeable prices.

    Returns dict:
      ask_up/bid_up: (price, size) on Yes, or None
      ask_down/bid_down: (price, size) on No (1-Yes complement), or None
      empty_book: True when either side has no quotes (the
                  "NO ONE TO TRADE AGAINST" condition)
    Sizes are in contracts; dollar notional = size * price.
    """
    def px(q):
        return float(q["px"]["value"])

    def qty(q):
        return float(q["qty"])

    bids = md.get("bids", []) or []
    offers = md.get("offers", []) or []
    out = {"state": md.get("state"),
           "empty_bids": not bids,
           "empty_offers": not offers}
    if offers:
        out["ask_up"] = (px(offers[0]), qty(offers[0]))
        out["bid_down"] = (1.0 - px(offers[0]), qty(offers[0]))
    else:
        out["ask_up"] = None
        out["bid_down"] = None
    if bids:
        out["bid_up"] = (px(bids[0]), qty(bids[0]))
        out["ask_down"] = (1.0 - px(bids[0]), qty(bids[0]))
    else:
        out["bid_up"] = None
        out["ask_down"] = None
    out["empty_book"] = out["empty_bids"] or out["empty_offers"]
    return out


def mid_up(book):
    """Mid price of Up; the market's implied P(Up). None if not two-sided."""
    if book.get("bid_up") and book.get("ask_up"):
        return (book["bid_up"][0] + book["ask_up"][0]) / 2.0
    return None
