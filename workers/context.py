"""
Context enrichment: gives Maker something to reason from besides the market
price itself.

Golf uses Slash Golf (Live Golf Data on RapidAPI) because ESPN is permanently
blocked from Railway IPs. Other sports still attempt ESPN. Weather uses NOAA.
Finance uses FRED.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from core.espn_client import ESPNClient
from core.slash_golf_client import SlashGolfClient
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

_TAXONOMY_TO_ESPN: dict[str, tuple[str, str]] = {
    "nfl": ("football", "nfl"),
    "nba": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
    "ncaa football": ("football", "college-football"),
    "ncaa basketball": ("basketball", "mens-college-basketball"),
}

_slash_golf_schema_logged = False


def resolve_weather_city(candidate) -> str | None:
    sub = (getattr(candidate, "taxonomy_subcategory", "") or "").lower().strip()
    if sub:
        if sub in WEATHER_STATIONS:
            return sub
        if sub in CITY_ALIASES:
            return CITY_ALIASES[sub]
        for alias, city in CITY_ALIASES.items():
            if re.match(rf"^{re.escape(alias)}\b", sub):
                return city
        for city in WEATHER_STATIONS:
            if re.match(rf"^{re.escape(city)}\b", sub):
                return city

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


def _settlement_date(candidate) -> str | None:
    raw = getattr(candidate, "close_time", "") or ""
    if not raw:
        return None
    try:
        closed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if closed.tzinfo is None:
        closed = closed.replace(tzinfo=timezone.utc)
    return (closed.astimezone(timezone.utc) - timedelta(hours=8)).date().isoformat()


def _guess_sport_league(title: str) -> tuple[str, str] | None:
    t = title.lower()
    for kw, sl in _SPORT_LEAGUE_KEYWORDS.items():
        if kw in t:
            return sl
    return None


def _slash_player_name(row: dict) -> str:
    first = (row.get("firstName") or "").strip()
    last = (row.get("lastName") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return (row.get("displayName") or row.get("name") or "?").strip()


def _slash_line(row: dict) -> str:
    name = _slash_player_name(row)
    position = str(row.get("position") or "").strip()
    total = str(row.get("total") or row.get("score") or "").strip()
    hole = row.get("currentHole") or row.get("thru")
    status = str(row.get("status") or "").lower()

    parts = []
    if position:
        parts.append(position)
    parts.append(name)
    if total:
        parts.append(f": {total}")
    if status in ("cut", "wd", "dq"):
        parts.append(f" ({status.upper()})")
    elif isinstance(hole, (int, float)) and hole > 0:
        parts.append(f" (thru {hole:g})")
    elif status == "complete" or row.get("roundComplete") is True:
        parts.append(" (round complete)")
    return "".join(parts).strip()


def _find_slash_player(rows: list, player: str) -> dict | None:
    wanted = player.strip().lower()
    if not wanted:
        return None
    names = [_slash_player_name(r).lower() for r in rows]
    for row, name in zip(rows, names):
        if name and name == wanted:
            return row
    surname = wanted.rsplit(" ", 1)[-1]
    if len(surname) < 3:
        return None
    hits = [row for row, name in zip(rows, names) if name.rsplit(" ", 1)[-1] == surname]
    return hits[0] if len(hits) == 1 else None


def _slash_shots_back(rows: list, row: dict) -> str:
    def to_number(r):
        raw = r.get("total") or r.get("score")
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            text = raw.strip().upper()
            if text in ("E", "EVEN", "PAR"):
                return 0.0
            try:
                return float(text.replace("+", ""))
            except ValueError:
                return None
        return None

    mine = to_number(row)
    scores = [s for s in (to_number(r) for r in rows) if s is not None]
    if mine is None or not scores:
        return ""
    back = mine - min(scores)
    if back <= 0:
        return ", leading"
    return f", {back:g} shot(s) back"


class ContextEnricher:
    def __init__(
        self,
        espn: ESPNClient = None,
        slash_golf: SlashGolfClient = None,
        weather: NOAAClient = None,
        fred: FredClient = None,
    ):
        self.espn = espn or ESPNClient()
        self.slash_golf = slash_golf
        self.weather = weather
        self.fred = fred

    def enrich(self, candidate: Candidate) -> str | None:
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
        target = _settlement_date(candidate)
        data = self.weather.get_city_forecast(city, target_date=target)
        if not data:
            return None

        lines = [
            f"SOURCE: US National Weather Service, station {data['station']} "
            f"({city.title()}). This is the OFFICIAL forecast from the same "
            f"observing station Kalshi's temperature rules settle against — "
            f"not a third-party proxy. Treat it as the best available estimate "
            f"of the settling value, while confirming the station against this "
            f"specific market's rules:",
        ]
        if target:
            lines.append(f"  This market settles on {target}.")
        if data.get("forecast_high_f") is None:
            lines.append(
                f"  NO NWS daytime forecast is available for "
                f"{target or 'the requested day'} — NWS publishes about a week "
                f"ahead. Do NOT infer a high from the other figures here."
            )
        else:
            label = data.get("forecast_label") or "forecast"
            lines.append(
                f"  NWS forecast high for {data.get('forecast_date') or 'that day'} "
                f"({label}): {data['forecast_high_f']}\u00b0F"
            )
        if data.get("current_temp_f") is not None:
            lines.append(f"  Current observed temp: {data['current_temp_f']:.1f}\u00b0F")
        if data.get("forecast_today"):
            lines.append(f"  Forecast detail: {data['forecast_today']}")
        return "\n".join(lines)

    def _economics_context(self, candidate: Candidate) -> str | None:
        result = self.fred.get_series_for_keyword(candidate.title)
        if not result:
            return None
        return (
            f"SOURCE: FRED (Federal Reserve Economic Data), official series "
            f"{result['series_id']}: "
            f"latest reading {result['value']} as of {result['date']} "
            f"(this is the last *published* figure, not a forecast of an unreleased number)."
        )

    def _golf_context(self, candidate: Candidate) -> str | None:
        """Leaderboard context via Slash Golf, anchored on the named player."""
        if not self.slash_golf or not self.slash_golf.available:
            log.debug("Slash Golf unavailable — no golf context for %s", candidate.ticker)
            return None

        data = self.slash_golf.leaderboard()
        if not data:
            return None

        rows = data.get("rows") or []
        if not rows:
            return None

        global _slash_golf_schema_logged
        if not _slash_golf_schema_logged and rows:
            _slash_golf_schema_logged = True
            log.info("Slash Golf leaderboard schema keys: %s", ", ".join(sorted(rows[0].keys())))

        event_name = data.get("name") or "current PGA event"
        lines = [
            f"SOURCE: Slash Golf live leaderboard — {event_name}. "
            f"This is the official live scoring feed for the current PGA Tour event.",
        ]

        # Top of board
        for r in rows[:12]:
            lines.append(f"  {_slash_line(r)}")

        player = (candidate.yes_sub_title or "").strip()
        if player:
            match = _find_slash_player(rows, player)
            if match is None:
                lines.append(
                    f"  NOTE: {player} — the player this market is about — does "
                    f"not appear on the current leaderboard. Treat any "
                    f"leaderboard inference about them as unsupported "
                    f"(withdrawn, missed cut, or not in field)."
                )
            else:
                rank = rows.index(match) + 1
                lines.append(
                    f"  THIS MARKET IS ABOUT {player}: {_slash_line(match)} "
                    f"(position {rank} of {len(rows)} on the board"
                    f"{_slash_shots_back(rows, match)})"
                )
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
        lines = [
            f"SOURCE: ESPN {league.upper()} scoreboard. ESPN may not be "
            f"reporting the same fixture this market settles on; check before "
            f"weighting this heavily.",
        ]
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
