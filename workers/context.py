"""
Context enrichment: gives Maker something to reason from besides the market
price itself — pulling live ESPN state for golf/sports candidates before the
Kimi call.

Honest limitation: matching a Kalshi ticker/title to the *right* ESPN
tournament or game is a real problem, not a solved one. Kalshi titles are
plain English ("Will Scheffler win the [tournament]?") and ESPN's leaderboard
doesn't carry Kalshi's ticker — there's no shared ID to join on. What's below
is a keyword heuristic (tour/league guessed from the title) good enough to
get a leaderboard or scoreboard snapshot in front of Maker; it is NOT
guaranteed to pull the specific event the market is about when multiple
tournaments/games are live at once. Tighten this once you're looking at real
concurrent Kalshi golf/sports tickers side by side with ESPN's event IDs —
that's a tuning pass that needs live data, not something to guess right from
here.
"""
from __future__ import annotations

import logging
import re

from core.espn_client import ESPNClient
from core.weather_client import NOAAClient, WEATHER_STATIONS, CITY_ALIASES
from core.fred_client import FredClient
from workers.scout import Candidate

log = logging.getLogger("daemon_kalshi.context")

_SPORT_LEAGUE_KEYWORDS = {
    "nfl": ("football", "nfl"),
    "nba": ("basketball", "nba"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
    "wnba": ("basketball", "wnba"),
}


def _guess_golf_tour(title: str) -> str:
    t = title.lower()
    if "lpga" in t:
        return "lpga"
    if "champions tour" in t or "senior" in t:
        return "champions-tour"
    if "korn ferry" in t:
        return "korn-ferry-tour"
    return "pga"  # default assumption — tune once you see real market titles


def _guess_sport_league(title: str) -> tuple[str, str] | None:
    t = title.lower()
    for kw, sl in _SPORT_LEAGUE_KEYWORDS.items():
        if kw in t:
            return sl
    return None


class ContextEnricher:
    def __init__(self, espn: ESPNClient = None, weather: NOAAClient = None, fred: FredClient = None):
        self.espn = espn or ESPNClient()
        self.weather = weather
        self.fred = fred

    def enrich(self, candidate: Candidate) -> str | None:
        """Returns a short text block to prepend to Maker's prompt, or None
        if no relevant/reachable data for this candidate's category."""
        category = candidate.category.lower()
        try:
            if category == "golf" or "golf" in candidate.title.lower():
                return self._golf_context(candidate)
            if category == "sports":
                return self._sports_context(candidate)
            if category == "climate" and self.weather:
                return self._weather_context(candidate)
            if category == "economics" and self.fred:
                return self._economics_context(candidate)
        except Exception:
            log.exception("Context fetch failed for %s — proceeding without it", candidate.ticker)
        return None

    def _weather_context(self, candidate: Candidate) -> str | None:
        title_lower = candidate.title.lower()
        # Check full station names first, then abbreviations (both with word
        # boundaries — "la" as a bare token, not as a substring of "atlanta").
        city = next((c for c in WEATHER_STATIONS if re.search(rf"\b{re.escape(c)}\b", title_lower)), None)
        if not city:
            alias = next((a for a in CITY_ALIASES if re.search(rf"\b{re.escape(a)}\b", title_lower)), None)
            if alias:
                city = CITY_ALIASES[alias]
        if not city:
            return None
        data = self.weather.get_city_forecast(city)
        if not data:
            return None
        lines = [f"NWS data for {city.title()} (station {data['station']}) — this is the "
                 f"same station Kalshi's market rules should name for settlement, verify against "
                 f"the specific market's rules:"]
        if data["forecast_high_f"] is not None:
            lines.append(f"  NWS forecast high today: {data['forecast_high_f']}\u00b0F")
        if data["current_temp_f"] is not None:
            lines.append(f"  Current observed temp: {data['current_temp_f']:.1f}\u00b0F")
        if data["forecast_today"]:
            lines.append(f"  Forecast detail: {data['forecast_today']}")
        return "\n".join(lines)

    def _economics_context(self, candidate: Candidate) -> str | None:
        result = self.fred.get_series_for_keyword(candidate.title)
        if not result:
            return None
        return (
            f"FRED official data — series {result['series_id']}: "
            f"latest reading {result['value']} as of {result['date']} "
            f"(this is the last *published* figure, not a forecast of an unreleased number)."
        )

    def _golf_context(self, candidate: Candidate) -> str | None:
        tour = _guess_golf_tour(candidate.title)
        data = self.espn.golf_leaderboard(tour)
        events = data.get("events", [])
        if not events:
            return None
        event = events[0]
        competitors = (
            event.get("competitions", [{}])[0].get("competitors", [])
        )
        lines = [f"ESPN {tour.upper()} leaderboard — {event.get('name', 'current event')}:"]
        for c in competitors[:10]:
            name = c.get("athlete", {}).get("displayName", "?")
            score = c.get("score", "?")
            status = c.get("status", {}).get("position", {}).get("displayName", "")
            lines.append(f"  {status} {name}: {score}")
        return "\n".join(lines)

    def _sports_context(self, candidate: Candidate) -> str | None:
        guess = _guess_sport_league(candidate.title)
        if not guess:
            return None
        sport, league = guess
        data = self.espn.scoreboard(sport, league)
        events = data.get("events", [])
        if not events:
            return None
        lines = [f"ESPN {league.upper()} scoreboard:"]
        for e in events[:8]:
            comp = e.get("competitions", [{}])[0]
            competitors = comp.get("competitors", [])
            desc = " vs ".join(
                f"{c.get('team', {}).get('displayName', '?')} {c.get('score', '')}"
                for c in competitors
            )
            status = comp.get("status", {}).get("type", {}).get("shortDetail", "")
            lines.append(f"  {desc} ({status})")
        return "\n".join(lines)
