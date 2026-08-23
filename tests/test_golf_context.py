"""
Golf context, anchored on the player the market is actually about.

Golf is the one sport the operator named as a priority. The grounding path
was rewritten on 2026-08-21 (a58802e / cce7b72 / e4cade6): ESPN's
scoreboard — which never carried PGA leaderboards, only game scores — was
replaced with Slash Golf's live leaderboard feed. That rewrite renamed every
function this file imported (``_find_competitor`` -> ``_find_slash_player``,
``_golf_line`` -> ``_slash_line``, ``_shots_back`` -> ``_slash_shots_back``,
and ``_golf_event_state`` was dropped entirely) without updating this file,
so it has failed to import — not failed a test, failed to *collect* — on
every push since. The whole suite has been unable to run for two days as a
result.

This is a full rewrite against the shipped implementation, not a rename.
Slash Golf's leaderboard is flatter than ESPN's competition object: rows
carry name/position/total/thru/status directly, with no separate
"event state" (round number, round description) to parse — so the
round-progress assertions from the old file are dropped along with the
function they tested. What survives is the shape of the original problem:
a golf market asks "does *this player* win", every market in one event
carries the same title, and the player appears only in Kalshi's
``yes_sub_title``. That is still true, and Slash Golf still has to answer
it for players outside whatever page of the leaderboard the feed returns
first.
"""
from __future__ import annotations

import pytest

from workers.context import (
    ContextEnricher,
    _find_slash_player,
    _slash_line,
    _slash_player_name,
    _slash_shots_back,
)

from tests.conftest import make_candidate


def row(name, total, position="", thru=None, status=""):
    first, _, last = name.partition(" ")
    return {
        "firstName": first,
        "lastName": last,
        "position": position,
        "total": total,
        "currentHole": thru,
        "status": status,
    }


FIELD = [
    row("Rory McIlroy", -12, "1"),
    row("Scottie Scheffler", -10, "T2"),
    row("Jon Rahm", -10, "T2"),
    row("Xander Schauffele", -8, "4"),
    row("Collin Morikawa", -7, "5"),
    row("Viktor Hovland", -6, "6"),
    row("Ludvig Aberg", -5, "7"),
    row("Tommy Fleetwood", -4, "8"),
    row("Justin Thomas", -3, "9"),
    row("Patrick Cantlay", -2, "10"),
    row("Min Woo Lee", -1, "11"),
    row("Tyrrell Hatton", 0, "12"),
    # Outside the 12 lines the context prints — the case the leaderboard
    # excerpt cannot express on its own, and the reason yes_sub_title
    # matching exists at all.
    row("Wyndham Clark", 3, "T41"),
]


class FakeSlashGolf:
    available = True

    def __init__(self, rows=None, event_name="The Open Championship"):
        self.calls = 0
        self._rows = FIELD if rows is None else rows
        self._event_name = event_name

    def leaderboard(self, **kwargs):
        self.calls += 1
        return {"tournId": "1", "year": 2026, "name": self._event_name, "rows": self._rows}


class UnavailableSlashGolf:
    available = False

    def leaderboard(self, **kwargs):
        raise AssertionError("must not be called when unavailable")


@pytest.fixture
def enricher():
    return ContextEnricher(slash_golf=FakeSlashGolf())


def golf_candidate(player="", title="The Open Championship winner"):
    candidate = make_candidate(
        ticker="KXTHEOPEN-26-X", title=title, category="Sports",
        event_ticker="KXTHEOPEN-26",
        taxonomy_category="Golf", taxonomy_subcategory="The Open",
    )
    candidate.yes_sub_title = player
    return candidate


def context_for(enricher, player):
    return enricher.enrich(golf_candidate(player))


# -- the player the market is about ----------------------------------------


def test_the_named_player_is_called_out_explicitly(enricher):
    text = context_for(enricher, "Scottie Scheffler")

    assert "THIS MARKET IS ABOUT Scottie Scheffler" in text


def test_a_player_outside_the_printed_leaderboard_is_still_reported(enricher):
    """The whole point. Wyndham Clark sits 41st, past the 12 rows the context
    prints — a leaderboard excerpt alone would leave the model pricing a
    contract it had no data on."""
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
    """Withdrawn, missed the cut, or never entered — all real information,
    and all very different from "we did not look"."""
    text = context_for(enricher, "Tiger Woods")

    assert "does not appear on the current leaderboard" in text
    assert "unsupported" in text


def test_a_market_with_no_named_player_still_gets_the_leaderboard(enricher):
    text = context_for(enricher, "")

    assert "The Open Championship" in text
    assert "Rory McIlroy" in text
    assert "THIS MARKET IS ABOUT" not in text


def test_slash_golf_is_named_as_the_source(enricher):
    """The old ESPN source line must not survive the rewrite by accident —
    ESPN never carried PGA leaderboards, only game scores."""
    text = context_for(enricher, "Rory McIlroy")

    assert "Slash Golf" in text
    assert "ESPN" not in text


def test_unavailable_slash_golf_yields_no_context_and_no_call():
    enricher = ContextEnricher(slash_golf=UnavailableSlashGolf())

    assert context_for(enricher, "Rory McIlroy") is None


def test_no_slash_golf_client_at_all_yields_no_context():
    """The pre-e4cade6 state: the client existed but was never wired into
    main.py, so every golf candidate silently got no grounding."""
    enricher = ContextEnricher()

    assert context_for(enricher, "Rory McIlroy") is None


# -- leaderboard line rendering ----------------------------------------------


def test_holes_played_appear_when_the_feed_says():
    assert "thru 14" in _slash_line(row("A B", -4, "T3", thru=14))


def test_a_completed_round_says_so():
    assert "round complete" in _slash_line(row("A B", -4, "T3", status="complete"))


def test_a_cut_status_is_reported_not_hidden():
    assert "(CUT)" in _slash_line(row("A B", 5, "T60", status="cut"))


def test_missing_state_renders_as_the_name_alone_not_as_a_guess():
    assert _slash_line({"firstName": "A", "lastName": "B", "total": -4}).endswith("A B: -4")


def test_player_name_falls_back_to_display_name_when_split_is_absent():
    assert _slash_player_name({"displayName": "A B"}) == "A B"
    assert _slash_player_name({}) == "?"


# -- name matching -----------------------------------------------------------


def test_an_exact_name_matches():
    assert _slash_player_name(_find_slash_player(FIELD, "Jon Rahm")) == "Jon Rahm"


def test_matching_is_case_and_space_insensitive():
    assert _find_slash_player(FIELD, "  jon rahm ") is not None


def test_a_surname_matches_when_the_full_name_differs():
    """Kalshi and the feed both carry human-typed names: "S. Scheffler"
    against "Scottie Scheffler"."""
    found = _find_slash_player(FIELD, "S. Scheffler")

    assert _slash_player_name(found) == "Scottie Scheffler"


def test_an_ambiguous_surname_refuses_rather_than_picking_one():
    field = [row("Si Woo Kim", -4), row("Tom Kim", -3)]

    assert _find_slash_player(field, "Kim") is None


def test_a_very_short_surname_is_not_used_to_match():
    assert _find_slash_player([row("Bob Li", -4)], "Xu") is None


def test_an_unknown_player_returns_nothing():
    assert _find_slash_player(FIELD, "Nobody At All") is None
    assert _find_slash_player(FIELD, "") is None


# -- shots back arithmetic ---------------------------------------------------


def test_even_par_is_read_as_zero():
    field = [row("A", "E"), row("B", "+3")]

    assert _slash_shots_back(field, field[1]) == ", 3 shot(s) back"


def test_a_non_numeric_score_yields_no_claim():
    """"CUT" or "WD" is not a number, and inventing one would be worse than
    saying nothing."""
    field = [row("A", -5), row("B", "CUT")]

    assert _slash_shots_back(field, field[1]) == ""


# -- the field is still captured from Kalshi ---------------------------------


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
