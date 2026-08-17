"""
Each grounding source says what it is and how far to trust it.

Every source was labelled "Live ESPN context (may or may not be the exact
event — verify it matches before trusting it)". So an NWS forecast — the
instrument Kalshi's own temperature rules settle against — reached the model
labelled as sports data it had just been told not to trust.

Live weather markets showed the damage on the first day they were ever
evaluated:

    KXHIGHNY-26AUG17-T84  -> Maker 48%  (market 85.5%)  reject conf 0.88
    KXHIGHCHI-26AUG17-T78 -> Maker 72%  (market 25.0%)  reject conf 0.90

Same-day temperature markets, where NWS publishes a forecast and the market
prices off it. A 37-47pp disagreement is what a model produces when it has
been told its data probably is not about this event.

The Checker rejected both at high confidence and was right to. The fix is
upstream — label the data honestly — not a looser gate.
"""
from __future__ import annotations

from workers.context import ContextEnricher

from tests.conftest import make_candidate


class FakeNOAA:
    def get_city_forecast(self, city, target_date=None):
        return {"station": "KNYC", "forecast_high_f": 93,
                "forecast_date": "2026-08-17", "forecast_label": "Thursday",
                "current_temp_f": 84.2, "forecast_today": "Hot"}


class FakeESPN:
    def golf_leaderboard(self, tour="pga"):
        return {"events": [{
            "name": "The Open",
            "competitions": [{"competitors": [
                {"athlete": {"displayName": "Rory McIlroy"}, "score": -12,
                 "status": {"position": {"displayName": "1"}}},
            ]}],
        }]}

    def scoreboard(self, sport, league):
        return {"events": [{"competitions": [{
            "competitors": [{"team": {"displayName": "A"}, "score": "3"}],
            "status": {"type": {"shortDetail": "Final"}},
        }]}]}


class FakeFRED:
    def get_series_for_keyword(self, title):
        return {"series_id": "CPIAUCSL", "value": "3.2", "date": "2026-07-01"}


def weather_candidate():
    c = make_candidate(ticker="KXHIGHNY-26AUG17-T84",
                       title="Highest temperature in NYC today?",
                       category="Weather", close_time="2026-08-17T20:00:00Z")
    c.taxonomy_category = "High Temp"
    c.taxonomy_subcategory = "New York"
    return c


def golf_candidate():
    c = make_candidate(ticker="KXTHEOPEN-26-X", title="The Open winner",
                       category="Sports")
    c.taxonomy_category = "Golf"
    c.taxonomy_subcategory = "The Open"
    return c


# -- weather is not sports data ---------------------------------------------


def test_a_weather_forecast_is_not_labelled_espn():
    """The bug, stated directly."""
    text = ContextEnricher(weather=FakeNOAA()).enrich(weather_candidate())

    assert "ESPN" not in text


def test_the_weather_source_is_named():
    text = ContextEnricher(weather=FakeNOAA()).enrich(weather_candidate())

    assert "National Weather Service" in text
    assert "KNYC" in text


def test_the_weather_context_says_it_is_the_settling_instrument():
    """Not a proxy. For a same-day temperature market this IS what settles it,
    and the model has to be told that to weight it correctly."""
    text = ContextEnricher(weather=FakeNOAA()).enrich(weather_candidate())

    assert "settle" in text.lower()
    assert "not a third-party proxy" in text


def test_the_weather_context_does_not_tell_the_model_to_distrust_it():
    """The old wrapper said "may or may not be the exact event — verify it
    matches before trusting it" over every source, including this one."""
    text = ContextEnricher(weather=FakeNOAA()).enrich(weather_candidate())

    assert "may or may not be the exact event" not in text


# -- ESPN keeps its caveat, because it earns it -----------------------------


def test_golf_context_still_warns_about_event_matching():
    """ESPN genuinely may be reporting a different tournament. That caveat was
    right — it was just being applied to everything."""
    text = ContextEnricher(espn=FakeESPN()).enrich(golf_candidate())

    assert "SOURCE: ESPN" in text
    assert "may not be reporting the same event" in text


def test_sports_context_carries_the_same_warning():
    c = make_candidate(ticker="KXNFL-1", title="NFL game", category="Sports")
    c.taxonomy_category = "NFL"

    text = ContextEnricher(espn=FakeESPN()).enrich(c)

    assert "SOURCE: ESPN" in text
    assert "may not be reporting the same fixture" in text


# -- economics ---------------------------------------------------------------


def test_fred_names_itself_as_official():
    c = make_candidate(ticker="KXCPI-1", title="CPI YoY", category="Finance")

    text = ContextEnricher(fred=FakeFRED()).enrich(c)

    assert "FRED" in text
    assert "official" in text.lower()
    assert "ESPN" not in text


# -- the Maker's wrapper -----------------------------------------------------


class CapturingLLM:
    """Captures the prompt instead of calling a model."""

    def __init__(self):
        self.user_msg = ""

    def complete(self, system, user, temperature=0.3):
        self.user_msg = user
        return '{"probability_yes": 0.5, "confidence": 0.5, "reasoning": "x"}'


def maker_prompt_for(candidate, **enricher_kw):
    """The prompt the Maker actually sends, grounding block included."""
    from workers.maker import Maker

    maker = Maker(enricher=ContextEnricher(**enricher_kw))
    maker._llm = CapturingLLM()
    maker.propose(candidate)
    return maker._llm.user_msg


def test_the_prompt_does_not_call_a_weather_forecast_espn():
    """The wrapper prefixes whatever the enricher returned, so naming one
    source means lying about the others."""
    prompt = maker_prompt_for(weather_candidate(), weather=FakeNOAA())

    assert "ESPN" not in prompt
    assert "National Weather Service" in prompt


def test_the_prompt_defers_to_each_source_on_trust():
    prompt = maker_prompt_for(weather_candidate(), weather=FakeNOAA())

    assert "Live grounding data" in prompt
    assert "states its own" in prompt


def test_a_golf_prompt_still_carries_the_espn_caveat():
    prompt = maker_prompt_for(golf_candidate(), espn=FakeESPN())

    assert "SOURCE: ESPN" in prompt
    assert "may not be reporting the same event" in prompt
