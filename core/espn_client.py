"""
ESPN client — wraps the free, keyless, unofficial endpoints documented at
https://github.com/pseudo-r/Public-ESPN-API. No API key exists for these;
ESPN's old official Developer Center (and its apikey param) was retired
years ago. This is exactly what Maker needs for independent grounding on
sports/golf markets instead of anchoring on Kalshi's own price.

Caveat baked into the design: these are unofficial endpoints ESPN can change
without notice. Every method raises on non-200 rather than silently
returning stale/empty data, so a broken endpoint fails loudly in your logs
instead of quietly feeding Maker garbage.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("daemon_kalshi.espn")

SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports"
SITE_WEB_BASE = "https://site.web.api.espn.com/apis/site/v2/sports"
COMMON_V3_BASE = "https://site.web.api.espn.com/apis/common/v3/sports"

# Golf and tennis take a tour SLUG, not a numeric league id.
GOLF_TOURS = {"pga", "lpga", "champions-tour", "korn-ferry-tour"}


class ESPNClient:
    def __init__(self, timeout: float = 10.0):
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})

    def close(self):
        self._http.close()

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        resp = self._http.get(url, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"ESPN request failed [{resp.status_code}]: {url}")
        return resp.json()

    # -- general scoreboard / standings (any sport/league) -------------------

    def scoreboard(self, sport: str, league: str, dates: Optional[str] = None) -> dict:
        """dates format: YYYYMMDD, or a range YYYYMMDD-YYYYMMDD. Omit for 'today'."""
        params = {"dates": dates} if dates else None
        return self._get(f"{SITE_BASE}/{sport}/{league}/scoreboard", params=params)

    def standings(self, sport: str, league: str) -> dict:
        return self._get(f"https://site.api.espn.com/apis/v2/sports/{sport}/{league}/standings")

    def game_summary(self, sport: str, league: str, event_id: str) -> dict:
        return self._get(f"{SITE_BASE}/{sport}/{league}/summary", params={"event": event_id})

    def athlete_overview(self, sport: str, league: str, athlete_id: str) -> dict:
        return self._get(f"{COMMON_V3_BASE}/{sport}/{league}/athletes/{athlete_id}/overview")

    # -- golf specifically ----------------------------------------------------

    def golf_leaderboard(self, tour: str = "pga") -> dict:
        """Current/active tournament leaderboard. tour: pga, lpga, champions-tour,
        korn-ferry-tour — a slug, not a numeric id."""
        if tour not in GOLF_TOURS:
            log.warning("Unrecognized golf tour slug '%s' — passing through anyway", tour)
        return self._get(f"{SITE_BASE}/golf/{tour}/scoreboard")

    def golf_player_round(
        self, tour: str, event_id: str, player_id: str, season: int
    ) -> dict:
        """Hole-by-hole scoring for one player in one event — this is the
        granular data DÆMON-POLY's partial-round parsing worked against.
        Returns profile, rounds[] (each with linescores[]: per-hole
        strokes/par/scoreType), and stats[]."""
        url = f"{SITE_WEB_BASE}/golf/{tour}/leaderboard/{event_id}/playersummary"
        return self._get(url, params={"season": season, "player": player_id})

    # -- search -----------------------------------------------------------

    def search(self, query: str, sport: Optional[str] = None, limit: int = 10) -> dict:
        params = {"query": query, "limit": limit}
        if sport:
            params["sport"] = sport
        return self._get("https://site.api.espn.com/apis/search/v2", params=params)
