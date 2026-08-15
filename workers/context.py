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

# Taxonomy category (from the ticker prefix) to ESPN's sport/league slugs.
# Keyed off the ported taxonomy rather than the title because the ticker
# prefix is what actually identifies the league — a title like "Will the
# Chiefs beat the Bills?" names neither the sport nor the league.
#
# Deliberately partial. Soccer, Tennis, UFC/Boxing and Racing all exist in
# the taxonomy and all have ESPN endpoints, but each needs a league slug this
# mapping cannot infer (ESPN wants eng.1 / usa.1 / uefa.champions, not
# "soccer"). Fetching the wrong league is worse than fetching nothing: it
# hands Maker a scoreboard for real games that are not this market's game.
_TAXONOMY_TO_ESPN: dict[str, tuple[str, str]] = {
    "nfl": ("football", "nfl"),
    "nba": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
    "ncaa football": ("football", "college-football"),
    "ncaa basketball": ("basketball", "mens-college-basketball"),
}

# Golf subcategory to ESPN tour slug. Everything not listed falls through to
# the title heuristic, which defaults to the PGA tour.
_GOLF_SUBCATEGORY_TOURS: dict[str, str] = {
    "liv tour": "liv",
}


def resolve_weather_city(candidate) -> str | None:
    """Which WEATHER_STATIONS key this market settles against, if any.

    The taxonomy subcategory carries the city outright for temperature
    markets ("New York", "Chicago", "Miami"), and variants like "NYC Rain"
    and "NYC Snow Monthly" carry it as a prefix. That is a far stronger
    signal than scanning the title, which is why it is tried first.
    """
    sub = (getattr(candidate, "taxonomy_subcategory", "") or "").lower().strip()
    if sub:
        if sub in WEATHER_STATIONS:
            return sub
        if sub in CITY_ALIASES:
            return CITY_ALIASES[sub]
        # "NYC Rain", "NYC Snow Monthly" — city is the leading token(s).
        for alias, city in CITY_ALIASES.items():
            if re.match(rf"^{re.escape(alias)}\b", sub):
                return city
        for city in WEATHER_STATIONS:
            if re.match(rf"^{re.escape(city)}\b", sub):
                return city

    # Fall back to the title. Full station names first, then abbreviations,
    # both on word boundaries — "la" as a bare token, not inside "atlanta".
    title_lower = candidate.title.lower()
    city = next(
        (c for c in WEATHER_STATIONS if re.search(rf"\b{re.escape(c)}\b", title_lower)),
        None,
    )
    if city:
        return city
    alias = next(
        (a for a in CITY_ALIASES if re.search(rf"\b{re.escape(a)}\b", title_lower)),
        None,
    )
    return CITY_ALIASES[alias] if alias else None


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
        if no relevant/reachable data for this candidate's category.

        Routing is off the ported taxonomy (`category` = group,
        `taxonomy_category`, `taxonomy_subcategory`), not off free text.
        Those fields are derived from the ticker prefix, so they are stable
        in a way market titles are not.

        This dispatch was previously written against category names that no
        longer exist. It tested for "climate" and "economics"; the taxonomy
        emits "Weather" and "Finance". Neither branch could ever be taken, so
        NOAA and FRED were wired up, constructed at startup, and never once
        called in production — the Maker priced every temperature market with
        no forecast in front of it. Golf was reachable only by the
        `"golf" in title` fallback, which misses tickers like KXUSOPEN whose
        titles don't say "golf".
        """
        group = candidate.category.lower()
        sub = (candidate.taxonomy_category or "").lower()
        try:
            if group == "sports":
                if sub == "golf" or "golf" in candidate.title.lower():
                    return self._golf_context(candidate)
                return self._sports_context(candidate)
            if group == "weather" and self.weather:
                return self._weather_context(candidate)
            if group == "finance" and self.fred:
                return self._economics_context(candidate)
        except Exception:
            log.exception("Context fetch failed for %s — proceeding without it", candidate.ticker)
        return None

    def _weather_context(self, candidate: Candidate) -> str | None:
        city = resolve_weather_city(candidate)
        if not city:
            log.debug("No NWS station matched %s (%s / %s)", candidate.ticker,
                      candidate.taxonomy_category, candidate.taxonomy_subcategory)
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
        sub = (candidate.taxonomy_subcategory or "").lower().strip()
        tour = _GOLF_SUBCATEGORY_TOURS.get(sub) or _guess_golf_tour(candidate.title)
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
        sub = (candidate.taxonomy_category or "").lower().strip()
        guess = _TAXONOMY_TO_ESPN.get(sub) or _guess_sport_league(candidate.title)
        if not guess:
            log.debug("No ESPN league mapped for %s (%s)", candidate.ticker,
                      candidate.taxonomy_category)
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
