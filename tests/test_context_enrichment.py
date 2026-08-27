"""
Tests for grounding-data routing: ESPN, NOAA and FRED.

These exist because the routing was silently dead. `enrich()` dispatched on
category names ("climate", "economics", "golf") that the ported taxonomy does
not emit — it emits "Weather", "Finance" and "Sports". Both clients were
constructed at startup and neither was ever called, so the Maker priced New
York temperature markets with no forecast in front of it and looked, from the
logs, like it was working fine.

Every test below asserts that a real client actually gets called for a
realistically-shaped candidate. A test that only checks `enrich()` returns a
string would have passed against the broken version too.
"""
from __future__ import annotations

import pytest

from core.kalshi_categories import classify_ticker
from tests.conftest import make_candidate
from workers.context import ContextEnricher, resolve_weather_city


class _RecordingESPN:
    def __init__(self, payload=None):
        self.golf_calls = []
        self.scoreboard_calls = []
        self._payload = payload or {"events": []}

    def golf_leaderboard(self, tour):
        self.golf_calls.append(tour)
        return self._payload

    def scoreboard(self, sport, league):
        self.scoreboard_calls.append((sport, league))
        return self._payload


class _RecordingSlashGolf:
    """Golf grounding left ESPN on 2026-08-21 for Slash Golf's live feed.
    ESPN's ``golf_leaderboard`` never carried PGA leaderboards at all."""

    available = True

    def __init__(self, rows=None):
        self.calls = 0
        self._rows = rows if rows is not None else [
            {"firstName": "Rory", "lastName": "McIlroy",
             "position": "1", "total": -7},
        ]

    def leaderboard(self, **kwargs):
        self.calls += 1
        return {"tournId": "1", "year": 2026, "name": "US Open",
                "rows": self._rows}


class _RecordingNOAA:
    def __init__(self, forecast=None):
        self.calls = []
        self._forecast = forecast

    def get_city_forecast(self, city, target_date=None):
        # Records the date asked for as well as the city: which day the
        # forecast is for is now part of the contract, not an afterthought.
        self.calls.append(city)
        self.dates = getattr(self, "dates", [])
        self.dates.append(target_date)
        return self._forecast


class _RecordingFRED:
    def __init__(self, result=None):
        self.calls = []
        self._result = result

    def get_series_for_keyword(self, title):
        self.calls.append(title)
        return self._result


def _candidate_from_ticker(ticker, title):
    """Build a candidate the way Scout does — taxonomy fields included."""
    group, category, subcategory = classify_ticker(ticker)
    return make_candidate(
        ticker=ticker,
        title=title,
        category=group,
        taxonomy_category=category,
        taxonomy_subcategory=subcategory,
    )


# --------------------------------------------------------------------------
# NOAA
# --------------------------------------------------------------------------

def test_weather_market_actually_reaches_noaa():
    """The regression: group is "Weather", and the old code tested "climate"."""
    candidate = _candidate_from_ticker("KXHIGHNY-26AUG15-B90", "Will NYC hit 90 degrees?")
    assert candidate.category == "Weather"          # not "climate"

    noaa = _RecordingNOAA(forecast={
        "station": "KNYC", "forecast_high_f": 91,
        "current_temp_f": 84.2, "forecast_today": "Sunny and hot",
    })
    enricher = ContextEnricher(espn=_RecordingESPN(), weather=noaa, fred=None)

    out = enricher.enrich(candidate)

    assert noaa.calls == ["new york"]
    assert "KNYC" in out and "91" in out


@pytest.mark.parametrize(
    "subcategory, expected",
    [
        ("New York", "new york"),
        ("Chicago", "chicago"),
        ("Los Angeles", "los angeles"),
        ("Miami", "miami"),
        ("NYC Rain", "new york"),            # city as a leading token
        ("NYC Snow Monthly", "new york"),
        ("Philadelphia", "philadelphia"),
    ],
)
def test_taxonomy_subcategory_resolves_the_station(subcategory, expected):
    candidate = make_candidate(
        title="no city named here at all",
        category="Weather",
        taxonomy_category="High Temp",
        taxonomy_subcategory=subcategory,
    )
    assert resolve_weather_city(candidate) == expected


def test_weather_city_falls_back_to_the_title():
    candidate = make_candidate(
        title="Will the high in Denver exceed 95F?",
        category="Weather",
        taxonomy_subcategory="Other",
    )
    assert resolve_weather_city(candidate) == "denver"


def test_unmatched_weather_city_returns_none_rather_than_a_wrong_station():
    candidate = make_candidate(
        title="Will Arctic sea ice extent fall below 4M km2?",
        category="Weather",
        taxonomy_category="Climate",
        taxonomy_subcategory="Arctic Ice",
    )
    assert resolve_weather_city(candidate) is None

    noaa = _RecordingNOAA(forecast={"station": "KNYC"})
    assert ContextEnricher(espn=_RecordingESPN(), weather=noaa).enrich(candidate) is None
    assert noaa.calls == []


def test_la_alias_does_not_match_inside_atlanta():
    candidate = make_candidate(title="Will Atlanta stay below 80F?", category="Weather")
    assert resolve_weather_city(candidate) == "atlanta"


# --------------------------------------------------------------------------
# ESPN
# --------------------------------------------------------------------------

def test_golf_market_routes_to_the_leaderboard_without_golf_in_the_title():
    """KXUSOPEN's title never says "golf"; the taxonomy says Sports/Golf."""
    candidate = _candidate_from_ticker("KXUSOPEN-26", "Will Rory McIlroy win the US Open?")
    assert (candidate.category, candidate.taxonomy_category) == ("Sports", "Golf")

    espn = _RecordingESPN()
    golf = _RecordingSlashGolf()
    out = ContextEnricher(espn=espn, slash_golf=golf).enrich(candidate)

    # The routing claim, now against the source that actually serves golf.
    assert golf.calls == 1
    assert espn.scoreboard_calls == [], "a golf market must not hit a scoreboard"
    assert espn.golf_calls == [], "ESPN is no longer on the golf path at all"
    assert "McIlroy" in out


@pytest.mark.parametrize(
    "ticker, expected",
    [
        ("KXNFLGAME-26SEP07-KC", ("football", "nfl")),
        ("KXNBA-26", ("basketball", "nba")),
        ("KXMLBGAME-26", ("baseball", "mlb")),
        ("KXNHLGAME-26", ("hockey", "nhl")),
    ],
)
def test_league_comes_from_the_ticker_not_the_title(ticker, expected):
    """Titles like "Will the Chiefs win?" name neither sport nor league."""
    candidate = _candidate_from_ticker(ticker, "Will the home team win?")
    espn = _RecordingESPN()
    ContextEnricher(espn=espn).enrich(candidate)
    assert espn.scoreboard_calls == [expected]


def test_unmappable_sport_fetches_nothing_rather_than_the_wrong_league():
    """Soccer needs a league slug we cannot infer; a wrong one is worse."""
    candidate = make_candidate(
        title="Will Arsenal win?", category="Sports",
        taxonomy_category="Soccer", taxonomy_subcategory="Other",
    )
    espn = _RecordingESPN()
    assert ContextEnricher(espn=espn).enrich(candidate) is None
    assert espn.scoreboard_calls == []


# --------------------------------------------------------------------------
# FRED
# --------------------------------------------------------------------------

def test_finance_market_actually_reaches_fred():
    """The old code tested for "economics"; the taxonomy emits "Finance"."""
    candidate = _candidate_from_ticker("KXCPI-26", "Will CPI come in above 3%?")
    assert candidate.category == "Finance"

    fred = _RecordingFRED(result={"series_id": "CPIAUCSL", "value": "3.1", "date": "2026-07-01"})
    out = ContextEnricher(espn=_RecordingESPN(), fred=fred).enrich(candidate)

    assert fred.calls == ["Will CPI come in above 3%?"]
    assert "CPIAUCSL" in out


def test_missing_client_is_skipped_rather_than_crashing():
    candidate = _candidate_from_ticker("KXCPI-26", "Will CPI come in above 3%?")
    assert ContextEnricher(espn=_RecordingESPN(), fred=None).enrich(candidate) is None


# --------------------------------------------------------------------------
# failure containment
# --------------------------------------------------------------------------

def test_a_failing_data_source_never_blocks_the_proposal():
    """Grounding data is a nice-to-have; losing it must not lose the trade."""
    class _Broken:
        def scoreboard(self, sport, league):
            raise RuntimeError("ESPN is down")

        def golf_leaderboard(self, tour):
            raise RuntimeError("ESPN is down")

    candidate = _candidate_from_ticker("KXNFLGAME-26SEP07-KC", "Will KC win?")
    assert ContextEnricher(espn=_Broken()).enrich(candidate) is None
