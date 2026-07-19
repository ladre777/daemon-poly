"""Polymarket US venue layer — the SINGLE source of truth for live trading.

Unlike market_reader.py (which reads INTERNATIONAL Polymarket via the Gamma API
for informational / alert context only), every real order is priced and placed
on Polymarket US through the official `polymarket-us` SDK — the same proven path
the golf bot (bot.py) uses. The operator's funds live on this venue.

Verified facts about the US venue (do not assume Gamma semantics here):
  * Each Winner/Champion outcome is its OWN market with a unique slug
    (e.g. 'tec-mlb-champ-2026-09-27-tor' = Toronto to win the World Series).
  * `outcomes` / `outcomePrices` are JSON-encoded strings whose order varies and
    whose length is unreliable, so implied prices are taken from the live book
    (bestBid / bestAsk), never positionally.
  * Execution price for a YES buy = bestAsk. There is NO stale-price fallback —
    if there is no live ask, the market is treated as untradeable.
"""

import os
import json
from typing import Optional

_pm_client = None


def _load_pm_creds():
    """Polymarket US creds: prefer valid env secrets, fall back to .pm_creds.json.
    A valid key id is a UUID (4 dashes, no 0x prefix); the base64 Ed25519 secret
    never starts with 0x. Mirrors the golf bot so both bots authenticate the same
    way (env secrets can hold stale self-custody 0x wallet values)."""
    kid = os.environ.get("POLYMARKET_KEY_ID", "")
    sec = os.environ.get("POLYMARKET_SECRET_KEY", "")
    env_valid = (kid.count("-") == 4 and not kid.startswith("0x")
                 and sec and not sec.startswith("0x"))
    if not env_valid:
        # The WC bot runs from wc/, golf from repo root — search both (plus the
        # module dir) so the gitignored local fallback is found regardless of CWD.
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(here, ".pm_creds.json"),
            os.path.join(here, "..", ".pm_creds.json"),
            ".pm_creds.json",
        ]
        for path in candidates:
            try:
                with open(path) as f:
                    d = json.load(f)
                kid = d.get("key_id", "") or kid
                sec = d.get("secret_key", "") or sec
                break
            except Exception:
                continue
    return kid, sec


def get_pm_client():
    """Lazy-init the Polymarket US API client (Ed25519 key auth). None on failure."""
    global _pm_client
    if _pm_client is not None:
        return _pm_client
    try:
        from polymarket_us import PolymarketUS
        kid, sec = _load_pm_creds()
        _pm_client = PolymarketUS(key_id=kid, secret_key=sec)
        print("[PM-US] client initialised (Ed25519 auth)")
        return _pm_client
    except Exception as e:
        print(f"[PM-US] client init failed: {e}")
        _pm_client = None
        return None


def get_buying_power() -> float:
    """Current Polymarket US USD buying power (0.0 on failure)."""
    try:
        client = get_pm_client()
        if not client:
            return 0.0
        bal = client.account.balances()
        for b in bal.get("balances", []):
            if b.get("currency", "USD") == "USD":
                return float(b.get("buyingPower", b.get("currentBalance", 0)) or 0)
    except Exception as e:
        print(f"[PM-US] balance error: {e}")
    return 0.0


def _amount(val) -> float:
    """Coerce an Amount ({'value': '0.029'}) or scalar to float (0.0 on failure)."""
    try:
        if isinstance(val, dict):
            return float(val.get("value", 0) or 0)
        return float(val or 0)
    except Exception:
        return 0.0


def _outcome_label(m: dict) -> str:
    """Human label for a single-outcome futures market (the team / player name)."""
    for k in ("title", "groupItemTitle", "outcome", "titleShort"):
        v = m.get(k)
        if v and str(v).strip():
            return str(v).strip()
    slug = m.get("slug", "")
    return slug.rsplit("-", 1)[-1].upper() if slug else "?"


def _implied_pct(m: dict) -> Optional[float]:
    """YES implied probability (%) from the live book: mid of bid/ask when both
    exist, else whichever side exists, else None (untradeable — no book)."""
    ask = _amount(m.get("bestAskQuote"))
    bid = _amount(m.get("bestBidQuote"))
    if ask > 0 and bid > 0:
        return round((ask + bid) / 2 * 100, 1)
    if ask > 0:
        return round(ask * 100, 1)
    if bid > 0:
        return round(bid * 100, 1)
    return None


def get_sport_futures_us(sport_cfg: dict) -> dict:
    """Build the US futures catalog for a sport.

    Returns {market_question: {outcome_label: {"implied_pct", "slug", "tick"}}},
    restricted to OPEN, tradeable single-outcome markets inside events whose title
    matches the sport's `us_title_match` keywords. Every slug here is directly
    executable via executor.place_order — this set IS the auto-trade whitelist."""
    client = get_pm_client()
    if not client:
        return {}
    catalog = {}
    for query in sport_cfg.get("us_search", []):
        try:
            resp = client.search.query({"query": query})
        except Exception as e:
            print(f"[PM-US] search '{query}' error: {e}")
            continue
        events = resp.get("events", []) if isinstance(resp, dict) else []
        for ev in events:
            title = ev.get("title") or ""
            tl = title.lower()
            if not any(kw.lower() in tl for kw in sport_cfg.get("us_title_match", [])):
                continue
            book = catalog.setdefault(title, {})
            for m in ev.get("markets", []):
                if m.get("closed") or m.get("active") is False:
                    continue
                pct = _implied_pct(m)
                slug = m.get("slug")
                if pct is None or not slug:
                    continue
                book[_outcome_label(m)] = {
                    "implied_pct": pct,
                    "slug":        slug,
                    "tick":        str(m.get("orderPriceMinTickSize", "0.001")),
                }
    return {k: v for k, v in catalog.items() if v}


def catalog_index(catalog: dict) -> dict:
    """Flatten a catalog to {slug: {"tick","outcome","market","implied_pct"}}.
    Defensive: silently skips any entry that is not an executable outcome dict."""
    idx = {}
    for market, book in (catalog or {}).items():
        if not isinstance(book, dict):
            continue
        for outcome, meta in book.items():
            if not isinstance(meta, dict) or not meta.get("slug"):
                continue
            idx[meta["slug"]] = {
                "tick":        meta.get("tick", "0.001"),
                "outcome":     outcome,
                "market":      market,
                "implied_pct": meta.get("implied_pct"),
            }
    return idx


def catalog_slugs(catalog: dict) -> set:
    """Flat set of every executable slug in a catalog (the auto-trade whitelist)."""
    return set(catalog_index(catalog).keys())


def round_to_tick(price: float, tick: str = "0.001") -> float:
    """Round a price to the market's tick and clamp into (tick, 1-tick)."""
    try:
        t = float(tick)
    except Exception:
        t = 0.001
    if t <= 0:
        t = 0.001
    decimals = len(str(tick).split(".")[1]) if "." in str(tick) else 0
    p = round(round(price / t) * t, max(decimals, 0))
    return min(max(p, t), 1 - t)


def live_ask(market_slug: str) -> Optional[float]:
    """Live best ask (what you pay to BUY YES) as a 0-1 fraction. None if no book."""
    client = get_pm_client()
    if not client:
        return None
    try:
        md = client.markets.bbo(market_slug)
        md = md.get("marketData", md) if isinstance(md, dict) else {}
        ask = _amount(md.get("bestAsk"))
        return ask if ask > 0 else None
    except Exception as e:
        print(f"[PM-US] bbo error {market_slug}: {e}")
        return None


# ── MARKET DISCOVERY ────────────────────────────────────────────────────────

DISCOVERY_QUERIES = [
    "NFL Super Bowl",
    "NBA championship",
    "Premier League winner",
    "UEFA Champions League",
    "UFC fight",
    "Formula 1 championship",
    "US Open tennis",
    "boxing",
    "NHL Stanley Cup",
    "college football championship",
    "NASCAR championship",
    "golf major",
]

# Title keywords covered by configured sports — skip to avoid double-analysis.
DISCOVERY_SKIP_KEYWORDS = [
    "world cup", "mlb world series", "wnba champion",
    "open championship", "the open", "wimbledon",
]


def find_us_game_market(home: str, away: str) -> Optional[dict]:
    """Search Polymarket US for a live per-game market (e.g. 'Argentina vs Spain').
    Returns {slug, title, implied_pct} or None if not found."""
    client = get_pm_client()
    if not client:
        return None
    queries = [f"{home} vs {away}", f"{away} vs {home}", f"{home} {away}"]
    for q in queries:
        try:
            resp = client.search.query({"query": q})
        except Exception:
            continue
        events = resp.get("events", []) if isinstance(resp, dict) else []
        for ev in events:
            title = ev.get("title", "")
            tl = title.lower()
            ht = str(home).lower().split()[-1] if home else ""
            at = str(away).lower().split()[-1] if away else ""
            if ht and at and (ht in tl and at in tl):
                for m in ev.get("markets", []):
                    if m.get("closed") or m.get("active") is False:
                        continue
                    slug = m.get("slug")
                    pct  = _implied_pct(m)
                    if slug and pct is not None:
                        return {"slug": slug, "title": title, "implied_pct": pct}
    return None


def discover_us_markets(max_per_query: int = 4) -> dict:
    """Broad Polymarket US market discovery across all sports categories.
    Returns {event_title: {outcome: {implied_pct, slug, tick}}} — same
    format as get_sport_futures_us so it feeds the standard signal pipeline."""
    client = get_pm_client()
    if not client:
        return {}
    catalog: dict = {}
    seen_slugs: set = set()
    for query in DISCOVERY_QUERIES:
        try:
            resp = client.search.query({"query": query})
        except Exception as e:
            print(f"[DISCOVERY] '{query}' error: {e}")
            continue
        events = resp.get("events", []) if isinstance(resp, dict) else []
        count = 0
        for ev in events:
            if count >= max_per_query:
                break
            title = ev.get("title") or ""
            tl = title.lower()
            if any(kw in tl for kw in DISCOVERY_SKIP_KEYWORDS):
                continue
            book: dict = {}
            for m in ev.get("markets", []):
                if m.get("closed") or m.get("active") is False:
                    continue
                pct  = _implied_pct(m)
                slug = m.get("slug")
                if pct is None or not slug or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                book[_outcome_label(m)] = {
                    "implied_pct": pct,
                    "slug":        slug,
                    "tick":        str(m.get("orderPriceMinTickSize", "0.001")),
                }
            if book:
                if title in catalog:
                    catalog[title].update(book)
                else:
                    catalog[title] = book
                count += 1
    return catalog
