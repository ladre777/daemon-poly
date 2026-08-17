"""
Golf context, anchored on the player the market is actually about.

Golf is the one sport the operator named as a priority, and its grounding
path has never executed in production — the demo catalog contains no golf
markets at all, so ESPN has been called zero times in the entire deployment.
That makes these tests the only thing standing behind it.

The shape of the problem: a golf market asks "does *this player* win", and
every market in one event carries the same title — "PGA Championship winner".
The player appears only in Kalshi's ``yes_sub_title``, which nothing captured.
So the context returned the top ten and stopped, and for any player outside
it — most of the field, and most of the markets with an interesting price —
the model was handed a leaderboard that never mentioned the contract it was
pricing.
"""
from __future__ import annotations

import pytest

from workers.context import (
    ContextEnricher,
    _find_competitor,
    _golf_event_state,
    _golf_line,
    _shots_back,
)

from tests.conftest import make_candidate


def competitor(name, score, position="", thru=None, completed=None):
    status = {"position": {"displayName": position}}
    if thru is not None:
        status["thru"] = thru
    if completed is not None:
        status["type"] = {"completed": completed}
    return {"athlete": {"displayName": name}, "score": score, "status": status}


FIELD = [
    competitor("Rory McIlroy", -12, "1"),
    competitor("Scottie Scheffler", -10, "T2"),
    competitor("Jon Rahm", -10, "T2"),
    competitor("Xander Schauffele", -8, "4"),
    competitor("Collin Morikawa", -7, "5"),
    competitor("Viktor Hovland", -6, "6"),
    competitor("Ludvig Aberg", -5, "7"),
    competitor("Tommy Fleetwood", -4, "8"),
    competitor("Justin Thomas", -3, "9"),
    competitor("Patrick Cantlay", -2, "10"),
    # Outside the top ten — the case the old context could not express.
    competitor("Wyndham Clark", 3, "T41"),
]


class FakeESPN:
    def __init__(self, competitors=None, event_extra=None):
        self.calls: list[str] = []
        self._competitors = FIELD if competitors is None else competitors
        self._event_extra = event_extra or {}

    def golf_leaderboard(self, tour="pga"):
        self.calls.append(tour)
        event = {
            "name": "The Open Championship",
            "competitions": [{
                "competitors": self._competitors,
                "status": {"period": 3, "type": {"shortDetail": "Round 3 In Progress"}},
            }],
        }
        event.update(self._event_extra)
        return {"events": [event]}


@pytest.fixture
def enricher():
    return ContextEnricher(espn=FakeESPN())


def golf_candidate(player="", title="The Open Championship winner"):
    return make_candidate(
        ticker="KXTHEOPEN-26-X", title=title, category="Sports",
        event_ticker="KXTHEOPEN-26",
    )


def context_for(enricher, player):
    candidate = golf_candidate()
    candidate.taxonomy_category = "Golf"
    candidate.taxonomy_subcategory = "The Open"
    candidate.yes_sub_title = player
    return enricher.enrich(candidate)


# -- the player the market is about ----------------------------------------


def test_the_named_player_is_called_out_explicitly(enricher):
    text = context_for(enricher, "Scottie Scheffler")

    assert "THIS MARKET IS ABOUT Scottie Scheffler" in text


def test_a_player_outside_the_top_ten_is_still_reported(enricher):
    """The whole point. Wyndham Clark sits 41st; the old context returned ten
    names and left the model to price a contract it had no data on."""
    text = context_for(enricher, "Wyndham Clark")

    assert "Wyndham Clark" in text
    assert "T41" in text


def test_the_deficit_to_the_lead_is_stated(enricher):
    text = context_for(enricher, "Wyndham Clark")

    assert "15 shot(s) back" in text, "3 against a leader at -12"


def test_the_leader_is_described_as_leading(enricher):
    text = context_for(enricher, "Rory McIlroy")

    assert "leading" in text


def test_a_player_not_in_the_field_is_said_so_plainly(enricher):
    """Withdrawn, missed the cut, or never entered — all real information, and
    all very different from "we did not look"."""
    text = context_for(enricher, "Tiger Woods")

    assert "does not appear in ESPN's field" in text
    assert "unsupported" in text


def test_a_market_with_no_named_player_still_gets_the_leaderboard(enricher):
    text = context_for(enricher, "")

    assert "The Open Championship" in text
    assert "Rory McIlroy" in text
    assert "THIS MARKET IS ABOUT" not in text


# -- how much golf is left -------------------------------------------------


def test_the_round_is_reported(enricher):
    """A three-shot deficit in round one and the same deficit with four holes
    to play are not the same bet."""
    text = context_for(enricher, "Scottie Scheffler")

    assert "round 3" in text
    assert "Round 3 In Progress" in text


def test_holes_played_appear_when_espn_says():
    assert "thru 14" in _golf_line(competitor("A B", -4, "T3", thru=14))


def test_a_completed_round_says_so():
    assert "round complete" in _golf_line(
        competitor("A B", -4, "T3", completed=True)
    )


def test_missing_state_renders_as_nothing_not_as_a_guess():
    assert _golf_event_state({}, {}) == ""
    assert _golf_line(competitor("A B", -4)).endswith("A B: -4")


# -- name matching ---------------------------------------------------------


def test_an_exact_name_matches():
    assert _find_competitor(FIELD, "Jon Rahm")["athlete"]["displayName"] == "Jon Rahm"


def test_matching_is_case_and_space_insensitive():
    assert _find_competitor(FIELD, "  jon rahm ") is not None


def test_a_surname_matches_when_the_full_name_differs():
    """Kalshi and ESPN both carry human-typed names: "S. Scheffler" against
    "Scottie Scheffler"."""
    found = _find_competitor(FIELD, "S. Scheffler")

    assert found["athlete"]["displayName"] == "Scottie Scheffler"


def test_an_ambiguous_surname_refuses_rather_than_picking_one():
    field = [competitor("Si Woo Kim", -4), competitor("Tom Kim", -3)]

    assert _find_competitor(field, "Kim") is None


def test_a_very_short_surname_is_not_used_to_match():
    assert _find_competitor([competitor("Bob Li", -4)], "Xu") is None


def test_an_unknown_player_returns_nothing():
    assert _find_competitor(FIELD, "Nobody At All") is None
    assert _find_competitor(FIELD, "") is None


# -- shots back arithmetic -------------------------------------------------


def test_even_par_is_read_as_zero():
    field = [competitor("A", "E"), competitor("B", "+3")]

    assert _shots_back(field, field[1]) == ", 3 shot(s) back"


def test_a_non_numeric_score_yields_no_claim():
    """"CUT" or "WD" is not a number, and inventing one would be worse than
    saying nothing."""
    field = [competitor("A", -5), competitor("B", "CUT")]

    assert _shots_back(field, field[1]) == ""


# -- the field is still captured from Kalshi -------------------------------


def test_yes_sub_title_survives_validation():
    """It is the only field distinguishing markets in one golf event, and
    nothing captured it before."""
    from core.validation import validate_market

    valid = validate_market({
        "ticker": "KXTHEOPEN-26-SCHEF",
        "title": "The Open Championship winner",
        "yes_sub_title": "Scottie Scheffler",
        "yes_bid": 12, "yes_ask": 14, "volume": 5000,
        "close_time": "2036-12-31T00:00:00Z",
    })

    assert valid.yes_sub_title == "Scottie Scheffler"


def test_a_market_without_the_field_is_not_rejected():
    from core.validation import validate_market

    valid = validate_market({
        "ticker": "KXBTCD-26-B1", "title": "Bitcoin above",
        "yes_bid": 40, "yes_ask": 42, "volume": 5000,
        "close_time": "2036-12-31T00:00:00Z",
    })

    assert valid.yes_sub_title == ""
