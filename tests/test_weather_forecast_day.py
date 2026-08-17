"""
The forecast has to be for the day the market settles on.

Weather is a stated main focus and, like golf, its grounding path has never
executed — the demo catalog contains no weather markets, so NOAA has been
called zero times in the whole deployment. These tests are the only thing
standing behind it.

``get_city_forecast`` read ``periods[0]`` unconditionally, which is wrong
twice over and silently:

1. NWS periods alternate day and night. Called in the evening, ``periods[0]``
   is "Tonight" with ``isDaytime`` false, so the forecast high came back
   None — the single number a "highest temperature" market turns on simply
   vanished, with no error anywhere.
2. ``periods[0]`` is always the *nearest* period. A market settling two days
   out was handed today's forecast, formatted exactly like a relevant one.

Both are one failure: supplying data about a different subject than the
contract, confidently. It is the same shape as the golf path handing the
model a leaderboard that never mentioned the player being priced.
"""
from __future__ import annotations


from core.weather_client import _daytime_period_for, _period_date
from workers.context import ContextEnricher, _settlement_date

from tests.conftest import make_candidate


def period(name, date, high=None, daytime=True, detail=""):
    return {
        "name": name,
        "startTime": f"{date}T06:00:00-04:00" if daytime else f"{date}T18:00:00-04:00",
        "isDaytime": daytime,
        "temperature": high,
        "detailedForecast": detail,
    }


#: A realistic NWS response: alternating day/night, a week ahead.
PERIODS = [
    period("Tonight", "2026-08-17", 74, daytime=False),
    period("Tuesday", "2026-08-18", 88, detail="Sunny"),
    period("Tuesday Night", "2026-08-18", 71, daytime=False),
    period("Wednesday", "2026-08-19", 93, detail="Hot"),
    period("Wednesday Night", "2026-08-19", 75, daytime=False),
    period("Thursday", "2026-08-20", 90, detail="Humid"),
]


# -- picking the right period ----------------------------------------------


def test_an_evening_scan_no_longer_loses_the_high():
    """periods[0] here is "Tonight". The old code returned None for the
    forecast high — on a market that turns entirely on the high."""
    found = _daytime_period_for(PERIODS)

    assert found["name"] == "Tuesday"
    assert found["temperature"] == 88


def test_a_named_date_gets_that_days_forecast():
    assert _daytime_period_for(PERIODS, "2026-08-19")["temperature"] == 93
    assert _daytime_period_for(PERIODS, "2026-08-20")["temperature"] == 90


def test_a_date_outside_the_forecast_returns_nothing_rather_than_the_nearest():
    """NWS publishes about a week. A market four weeks out has no forecast,
    and the closest available day would be indistinguishable from a real
    answer."""
    assert _daytime_period_for(PERIODS, "2026-09-30") is None


def test_night_periods_are_never_offered_as_a_high():
    for date in ("2026-08-17", "2026-08-18", "2026-08-19"):
        found = _daytime_period_for(PERIODS, date)
        assert found is None or found["isDaytime"]


def test_an_empty_forecast_yields_nothing():
    assert _daytime_period_for([]) is None
    assert _daytime_period_for([period("Tonight", "2026-08-17", daytime=False)]) is None


# -- the local date -------------------------------------------------------


def test_the_period_date_is_read_in_the_stations_own_offset():
    """startTime carries the station's UTC offset, so slicing the date off it
    gives the local day — converting to UTC first would push evening periods
    in western time zones onto the following day."""
    assert _period_date(period("Wednesday", "2026-08-19")) == "2026-08-19"
    assert _period_date({"startTime": "2026-08-19T18:00:00-07:00"}) == "2026-08-19"


def test_a_malformed_start_time_yields_no_date():
    assert _period_date({"startTime": "nonsense"}) is None
    assert _period_date({}) is None
    assert _period_date(None) is None


# -- deriving the settlement day from the market ---------------------------


def test_a_market_closing_late_utc_settles_on_the_previous_local_day():
    """The trap this exists for: a US weather market closes at the end of its
    local settlement day, which is the small hours of the NEXT day in UTC.
    Reading the UTC date directly would ask for tomorrow's forecast on every
    single weather market."""
    candidate = make_candidate(close_time="2026-08-19T03:00:00Z")

    assert _settlement_date(candidate) == "2026-08-18"


def test_a_midday_close_stays_on_its_own_day():
    candidate = make_candidate(close_time="2026-08-19T20:00:00Z")

    assert _settlement_date(candidate) == "2026-08-19"


def test_an_unparseable_close_time_yields_no_target():
    assert _settlement_date(make_candidate(close_time="not a time")) is None
    assert _settlement_date(make_candidate(close_time="")) is None


# -- what the Maker is told ------------------------------------------------


class FakeNOAA:
    def __init__(self, payload):
        self.payload = payload
        self.requested: list[str | None] = []

    def get_city_forecast(self, city, target_date=None):
        self.requested.append(target_date)
        return self.payload


def weather_candidate(close_time="2026-08-19T20:00:00Z"):
    candidate = make_candidate(
        ticker="KXHIGHNY-26AUG19-B90", title="Highest temperature in NYC today?",
        category="Weather", close_time=close_time,
    )
    candidate.taxonomy_category = "High Temp"
    candidate.taxonomy_subcategory = "New York"
    return candidate


def test_the_settlement_date_is_what_gets_requested():
    noaa = FakeNOAA({"station": "KNYC", "forecast_high_f": 93,
                     "forecast_date": "2026-08-19", "forecast_label": "Wednesday",
                     "current_temp_f": 84.0, "forecast_today": "Hot"})

    ContextEnricher(weather=noaa).enrich(weather_candidate())

    assert noaa.requested == ["2026-08-19"]


def test_the_context_states_which_day_the_forecast_covers():
    """So a mismatch between the market's day and the forecast's day is
    visible to the model rather than assumed away."""
    noaa = FakeNOAA({"station": "KNYC", "forecast_high_f": 93,
                     "forecast_date": "2026-08-19", "forecast_label": "Wednesday",
                     "current_temp_f": 84.0, "forecast_today": "Hot"})

    text = ContextEnricher(weather=noaa).enrich(weather_candidate())

    assert "settles on 2026-08-19" in text
    assert "forecast high for 2026-08-19" in text


def test_a_missing_high_is_stated_not_omitted():
    """The most important thing to know about this context, on a market that
    turns on the high. Omitting it reads like the forecast said nothing
    rather than like we could not find the right day."""
    noaa = FakeNOAA({"station": "KNYC", "forecast_high_f": None,
                     "forecast_date": None, "forecast_label": None,
                     "current_temp_f": 84.0, "forecast_today": ""})

    text = ContextEnricher(weather=noaa).enrich(weather_candidate())

    assert "NO NWS daytime forecast is available" in text
    assert "Do NOT infer a high" in text


def test_the_observed_temperature_still_comes_through():
    noaa = FakeNOAA({"station": "KNYC", "forecast_high_f": 93,
                     "forecast_date": "2026-08-19", "forecast_label": "Wednesday",
                     "current_temp_f": 84.2, "forecast_today": "Hot"})

    text = ContextEnricher(weather=noaa).enrich(weather_candidate())

    assert "84.2" in text
    assert "KNYC" in text
