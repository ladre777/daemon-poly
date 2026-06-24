import requests
import os
from typing import Optional

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

HEADERS = {
    "Authorization": f"Bearer {os.environ.get('POLYMARKET_API_KEY', '')}",
    "Content-Type":  "application/json",
}

WORLD_CUP_MARKETS = {
    "winner":       "world-cup-winner",
    "golden_boot":  "world-cup-golden-boot-winner",
    "continental":  "world-cup-continent-winner-2026",
    "messi_ronaldo":"messi-vs-ronaldo-world-cup-contributions",
}

STAGE_ELIMINATION_TEAMS = [
    "france", "argentina", "spain", "england",
    "brazil", "germany", "portugal", "netherlands",
    "norway", "usa", "morocco", "colombia",
]


def get_market_by_slug(slug: str) -> Optional[dict]:
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        return None
    except Exception as e:
        return {"error": str(e)}


def get_winner_odds() -> dict:
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": WORLD_CUP_MARKETS["winner"], "limit": 1},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            return {}
        market = markets[0] if isinstance(markets, list) else markets
        tokens = market.get("tokens", [])
        odds = {}
        for token in tokens:
            outcome = token.get("outcome", "")
            price   = float(token.get("price", 0))
            odds[outcome] = round(price * 100, 1)
        return dict(sorted(odds.items(), key=lambda x: x[1], reverse=True))
    except Exception as e:
        return {"error": str(e)}


def get_stage_elimination_odds(team: str) -> dict:
    slug = f"world-cup-{team.lower().replace(' ', '-')}-stage-of-elimination"
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            return {"error": f"No market found for {team}"}
        market = markets[0] if isinstance(markets, list) else markets
        tokens = market.get("tokens", [])
        rounds = {}
        for token in tokens:
            outcome = token.get("outcome", "")
            price   = float(token.get("price", 0))
            rounds[outcome] = round(price * 100, 1)
        rounds["_sum_check"] = round(sum(v for k, v in rounds.items() if not k.startswith("_")), 1)
        return rounds
    except Exception as e:
        return {"error": str(e)}


def get_golden_boot_odds() -> dict:
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": WORLD_CUP_MARKETS["golden_boot"]},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            return {}
        market = markets[0] if isinstance(markets, list) else markets
        tokens = market.get("tokens", [])
        odds = {}
        for token in tokens:
            odds[token.get("outcome", "")] = round(float(token.get("price", 0)) * 100, 1)
        return dict(sorted(odds.items(), key=lambda x: x[1], reverse=True)[:10])
    except Exception as e:
        return {"error": str(e)}


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


def search_markets(query: str, limit: int = 10) -> list:
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"search": query, "limit": limit, "active": "true"},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return [{"error": str(e)}]


def get_token_price(token_id: str, side: str = "BUY") -> Optional[float]:
    try:
        resp = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        book = resp.json()
        key  = "asks" if side == "BUY" else "bids"
        levels = book.get(key, [])
        if levels:
            return float(levels[0].get("price", 0))
        return None
    except Exception:
        return None
