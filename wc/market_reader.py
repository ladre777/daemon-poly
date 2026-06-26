"""Polymarket reader (Gamma API), corrected and generalized across sports.

Key fixes vs. the original World-Cup-only version:
  1. Futures live under EVENT slugs, not market slugs — query /events?slug=, then
     read the markets array inside the event.
  2. Odds live in `outcomes` / `outcomePrices` (JSON-encoded strings), NOT a
     `tokens` array — the old parser always returned empty.
  3. Free-text discovery uses /public-search (the plain ?search= param ignores
     the query and returns junk).
"""

import requests
import os
import json
from typing import Optional

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

HEADERS = {
    "Authorization": f"Bearer {os.environ.get('POLYMARKET_API_KEY', '')}",
    "Content-Type":  "application/json",
}

# Teams whose World-Cup Stage-of-Elimination ladders are worth scanning (Edge LADDER).
STAGE_ELIMINATION_TEAMS = [
    "france", "argentina", "spain", "england",
    "brazil", "germany", "portugal", "netherlands",
    "norway", "usa", "morocco", "colombia",
]


def _parse_market_prices(market: dict) -> list:
    """Return [(outcome_label, price_fraction), ...] from a Gamma market."""
    try:
        outcomes = market.get("outcomes")
        prices   = market.get("outcomePrices")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        return list(zip(outcomes or [], [float(p) for p in (prices or [])]))
    except Exception:
        return []


def get_event_odds(slug: str) -> dict:
    """Resolve a Polymarket EVENT by slug -> {outcome_label: implied_pct}.

    Handles grouped futures (each sub-market is '<Team> / Yes-No' -> take Yes)
    and plain two-sided markets (store both sides as 'Question: Outcome')."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"slug": slug},
            headers=HEADERS,
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        if not (isinstance(data, list) and data):
            return {}

        odds = {}
        for m in data[0].get("markets", []):
            if m.get("closed") or m.get("archived") or m.get("active") is False:
                continue
            label = m.get("groupItemTitle") or m.get("question") or ""
            pairs = _parse_market_prices(m)
            if not pairs or not label:
                continue
            yes = next((p for (o, p) in pairs if str(o).strip().lower() == "yes"), None)
            if yes is not None:
                odds[label] = round(yes * 100, 1)
            else:
                for o, p in pairs:
                    odds[f"{label}: {o}"] = round(p * 100, 1)
        return dict(sorted(odds.items(), key=lambda x: x[1], reverse=True))
    except Exception as e:
        return {"error": str(e)}


def get_sport_futures(sport_cfg: dict) -> dict:
    """All configured futures for a sport -> {market_label: {outcome: pct}}.
    Each market is capped to its top 12 outcomes to bound the prompt size."""
    out = {}
    for label, slug in sport_cfg.get("futures", {}).items():
        odds = get_event_odds(slug)
        if odds and "error" not in odds:
            out[label] = dict(list(odds.items())[:12])
    return out


def public_search(query: str, limit: int = 6) -> list:
    """Polymarket text search -> [{'slug','title'}, ...]."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/public-search",
            params={"q": query, "limit_per_type": limit},
            headers=HEADERS,
            timeout=12,
        )
        resp.raise_for_status()
        return [
            {"slug": e.get("slug"), "title": e.get("title")}
            for e in resp.json().get("events", [])
        ]
    except Exception as e:
        return [{"error": str(e)}]


def search_markets(query: str, limit: int = 6) -> list:
    return public_search(query, limit)


def get_market_by_slug(slug: str) -> Optional[dict]:
    odds = get_event_odds(slug)
    if not odds or "error" in odds:
        return None
    return {"slug": slug, "odds": odds}


def _name_token(name: str) -> str:
    """Distinctive token for matching an event title — last 'real' word of a team
    or player name (e.g. 'New York Yankees' -> 'yankees', 'Carlos Alcaraz' -> 'alcaraz')."""
    parts = [p for p in str(name).replace(".", " ").split() if len(p) > 2]
    return parts[-1].lower() if parts else str(name).strip().lower()


def find_game_market_odds(home: str, away: str, sport_label: str = "") -> dict:
    """Best-effort resolution of a per-game market via search -> {'slug','title','odds'}.
    Both competitors must appear in the matched event's title or slug, otherwise the
    hit is rejected (prevents feeding odds from an unrelated market into the prompt).
    Returns {} when no confident market is found (caller treats that as no odds)."""
    ht, at = _name_token(home), _name_token(away)
    queries = [f"{home} vs {away}", f"{away} vs {home}"]
    if sport_label:
        queries.append(f"{sport_label} {home} {away}")
    for q in queries:
        for hit in public_search(q, limit=4):
            slug = hit.get("slug")
            if not slug:
                continue
            title_l, slug_l = (hit.get("title") or "").lower(), slug.lower()
            in_title = ht in title_l and at in title_l
            in_slug  = ht in slug_l and at in slug_l
            if ht and at and not (in_title or in_slug):
                continue
            odds = get_event_odds(slug)
            if odds and "error" not in odds:
                return {"slug": slug, "title": hit.get("title"), "odds": odds}
    return {}


# ── World Cup Edge LADDER (Stage-of-Elimination) ────────────────────────────

def get_stage_elimination_odds(team: str) -> dict:
    slug = f"world-cup-{team.lower().replace(' ', '-')}-stage-of-elimination"
    odds = get_event_odds(slug)
    if not odds or "error" in odds:
        return {"error": f"No market found for {team}"}
    odds["_sum_check"] = round(
        sum(v for k, v in odds.items() if not k.startswith("_")), 1
    )
    return odds


def scan_stage_elimination_ladders() -> dict:
    anomalies = {}
    for team in STAGE_ELIMINATION_TEAMS:
        odds = get_stage_elimination_odds(team)
        if "error" in odds:
            continue
        total = odds.get("_sum_check", 100)
        if abs(total - 100) > 5:
            anomalies[team] = {
                "rounds":    {k: v for k, v in odds.items() if not k.startswith("_")},
                "sum":       total,
                "deviation": round(total - 100, 1),
            }
    return anomalies


def get_token_price(token_id: str, side: str = "BUY") -> Optional[float]:
    try:
        resp = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        book   = resp.json()
        key    = "asks" if side == "BUY" else "bids"
        levels = book.get(key, [])
        if levels:
            return float(levels[0].get("price", 0))
        return None
    except Exception:
        return None
