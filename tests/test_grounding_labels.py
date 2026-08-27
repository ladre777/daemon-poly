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


class FakeSlashGolf:
    """Golf grounding moved off ESPN on 2026-08-21. ESPN never carried PGA
    leaderboards — only game scores — so the golf path now reads Slash Golf's
    live scoring feed and the ESPN doubles below no longer reach it."""

    available = True

    def leaderboard(self, **kwargs):
        return {
            "tournId": "1", "year": 2026, "name": "The Open Championship",
            "rows": [
                {"firstName": "Rory", "lastName": "McIlroy",
                 "position": "1", "total": -12},
                {"firstName": "Scottie", "lastName": "Scheffler",
                 "position": "T2", "total": -10},
            ],
        }


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


def test_golf_context_names_slash_golf_as_the_official_feed():
    """This test used to assert golf carried ESPN's "may not be reporting the
    same event" caveat. That caveat was correct for ESPN and is wrong for what
    replaced it: Slash Golf is the live scoring feed for the event itself, not
    a third party that might be covering a different tournament. Asserting the
    old caveat here would mean re-labelling authoritative data as unreliable —
    the exact failure this whole file exists to prevent, pointed the other way.
    """
    text = ContextEnricher(slash_golf=FakeSlashGolf()).enrich(golf_candidate())

    assert "SOURCE: Slash Golf" in text
    assert "official live scoring feed" in text
    assert "ESPN" not in text


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

    #: ``Maker.__init__`` reads this to decide whether the LLM path is usable.
    #: Without it the double looks like an unconfigured provider.
    configured = True
    on_fallback = False

    def __init__(self):
        self.user_msg = ""

    def describe(self):
        return "primary=fake:fake-1 fallback=none"

    def complete(self, system, user, temperature=0.3):
        self.user_msg = user
        return '{"probability_yes": 0.5, "confidence": 0.5, "reasoning": "x"}'


def maker_prompt_for(candidate, **enricher_kw):
    """The prompt the Maker actually sends, grounding block included.

    The double goes in through the constructor. Swapping ``maker._llm``
    afterwards does not work: ``Maker.__init__`` computes ``_disabled`` from
    the *original* llm, so a post-hoc swap left ``_disabled=True`` and
    ``propose()`` returned ``None`` before it ever built a prompt — the
    assertions below were running against an empty string.
    """
    from workers.maker import Maker

    llm = CapturingLLM()
    maker = Maker(enricher=ContextEnricher(**enricher_kw), llm=llm)
    assert maker.available, "the Maker must not be disabled in this harness"
    maker.propose(candidate)
    return llm.user_msg


def test_the_prompt_does_not_call_a_weather_forecast_espn():
    """The wrapper prefixes whatever the enricher returned, so naming one
    source means lying about the others."""
    prompt = maker_prompt_for(weather_candidate(), weather=FakeNOAA())

    assert "ESPN" not in prompt
    assert "National Weather Service" in prompt


def test_the_prompt_defers_to_each_source_on_trust():
    """The wrapper wording is now "LIVE EVIDENCE CARD (current facts; verify
    market match)". What matters is unchanged and is what is asserted: the
    wrapper stays source-neutral, so each SOURCE: line inside the card is the
    only thing making a trust claim."""
    prompt = maker_prompt_for(weather_candidate(), weather=FakeNOAA())

    header = prompt.split("LIVE EVIDENCE CARD", 1)
    assert len(header) == 2, "the evidence card wrapper must be present"
    assert "verify market match" in prompt

    wrapper_line = header[1].splitlines()[0]
    for source in ("ESPN", "National Weather Service", "FRED", "Slash Golf"):
        assert source not in wrapper_line, (
            f"the wrapper named {source}, which mislabels every other source"
        )


def test_a_golf_prompt_carries_the_slash_golf_attribution():
    prompt = maker_prompt_for(golf_candidate(), slash_golf=FakeSlashGolf())

    assert "SOURCE: Slash Golf" in prompt
    assert "official live scoring feed" in prompt
