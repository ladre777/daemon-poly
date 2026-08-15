"""
Tests for the ported Kalshi taxonomy and Scout's use of it.

The bug class these guard against is silent, not loud: a category string that
matches nothing does not raise, it just means Scout never surfaces that
vertical. The previous default had three such strings out of six.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from core.kalshi_categories import (
    GROUPS,
    SUBCATEGORY_PATTERNS,
    classify_group,
    classify_ticker,
    get_hierarchy,
    match_hierarchy,
    ticker_prefix,
)
from workers.scout import Scout

from tests.fakes import FakeKalshiClient


# -- the ported table -------------------------------------------------------


def test_table_ported_intact():
    """Guards against a truncated copy — the upstream table has 509 rows."""
    assert len(SUBCATEGORY_PATTERNS) == 509
    assert all(len(row) == 4 for row in SUBCATEGORY_PATTERNS)


def test_every_row_maps_to_a_declared_group():
    """GROUPS is what config and risk key off, so it must be exhaustive."""
    groups = {row[1] for row in SUBCATEGORY_PATTERNS}
    assert groups <= set(GROUPS), groups - set(GROUPS)


def test_upstream_behaviour_is_unchanged():
    assert get_hierarchy("BTCD") == ("Crypto", "Bitcoin", "Daily")
    assert get_hierarchy("NOTATHING") == ("Other", "Other", "NOTATHING")


# -- the shadowing fix ------------------------------------------------------


def test_short_patterns_no_longer_shadow_specific_ones():
    """Upstream scans in list order, so the 2-char Politics pattern 'EC' wins
    on KXFE(DEC)ISION and files every Fed rate decision under Electoral
    College. Matching most-specific-first fixes it."""
    assert get_hierarchy("KXFEDDECISION")[:2] == ("Politics", "Electoral College")
    assert match_hierarchy("KXFEDDECISION") == ("Finance", "Fed", "Decisions")


def test_womens_march_madness_is_not_filed_as_the_mens_tournament():
    assert get_hierarchy("KXWMARMAD")[2] == "March Madness M"
    assert match_hierarchy("KXWMARMAD")[2] == "March Madness W"


def test_specificity_ordering_is_stable_for_equal_length_patterns():
    from core.kalshi_categories import _PATTERNS_BY_SPECIFICITY

    lengths = [len(row[0]) for row in _PATTERNS_BY_SPECIFICITY]
    assert lengths == sorted(lengths, reverse=True)
    assert len(_PATTERNS_BY_SPECIFICITY) == len(SUBCATEGORY_PATTERNS)


# -- ticker classification --------------------------------------------------


@pytest.mark.parametrize(
    "ticker,expected_group",
    [
        ("KXBTCD-25AUG14-B", "Crypto"),
        ("KXETHD-25AUG14", "Crypto"),
        ("KXPGATOUR-25", "Sports"),
        ("KXNFLGAME-25SEP07DALPHI", "Sports"),
        ("KXHIGHNY-25AUG14-T90", "Weather"),
        ("KXCPIYOY-25JUL", "Finance"),
        ("KXFEDDECISION-25SEP", "Finance"),
        ("KXPRES-28", "Politics"),
        ("KXSENATE-26TX", "Politics"),
        ("KXOSCARPIC-26", "Entertainment"),
        ("KXLOLGAMES-25", "Esports"),
        ("KXNOBELPEACE-25", "World Events"),
        ("KXSPACEX-25", "Science/Tech"),
    ],
)
def test_real_kalshi_ticker_shapes_classify(ticker, expected_group):
    assert classify_group(ticker) == expected_group


def test_golf_is_reachable_since_it_is_the_priority_category():
    """PRIORITY_KEYWORDS defaults to golf/pga — if golf did not classify into
    a group in SCOUT_CATEGORIES, the priority path would never fire."""
    group, category, _ = classify_ticker("KXPGATOUR-25AUG", "KXPGATOUR-25AUG")
    assert (group, category) == ("Sports", "Golf")


def test_prefix_extraction_strips_the_date_and_market_suffix():
    assert ticker_prefix("KXBTCD-25AUG14-B") == "KXBTCD"
    assert ticker_prefix("kxnflgame-25") == "KXNFLGAME"
    assert ticker_prefix("") == ""
    assert ticker_prefix("---") == ""


def test_event_ticker_wins_over_market_ticker():
    """Upstream keyed on the event ticker; the market ticker carries a suffix
    that can drag the substring match somewhere unrelated."""
    assert classify_group("SOMETHING-ODD", "KXBTCD-25AUG14") == "Crypto"


def test_unknown_ticker_is_other_not_a_guess():
    group, category, sub = classify_ticker("KXTOTALLYNEWSERIES-26")
    assert (group, category) == ("Other", "Other")
    assert sub == "KXTOTALLYNEWSERIES"


def test_empty_ticker_does_not_raise():
    assert classify_ticker("", "") == ("Other", "Other", "")


# -- config sanity ----------------------------------------------------------


def test_shipped_scout_categories_are_all_real_groups():
    """The regression this whole port exists for."""
    assert Scout.unknown_configured_categories() == set()


def test_the_old_guessed_defaults_would_now_be_caught():
    CONFIG.scout_categories = ["Sports", "Crypto", "Politics", "Economics",
                               "Climate", "Culture"]
    assert Scout.unknown_configured_categories() == {"Economics", "Climate", "Culture"}


def test_llm_reasoning_categories_are_real_groups():
    valid = {g.lower() for g in GROUPS}
    assert CONFIG.llm_reasoning_categories <= valid, (
        CONFIG.llm_reasoning_categories - valid
    )


# -- Scout integration ------------------------------------------------------


def _event(event_ticker, markets, category=""):
    """Kept as a grouping helper for readability; Scout reads markets now."""
    for m in markets:
        m.setdefault("event_ticker", event_ticker)
        m.setdefault("series_ticker", event_ticker)
        m["_kalshi_category"] = category
    return markets


def _market(ticker, volume=10_000):
    return {
        "ticker": ticker,
        "title": ticker,
        "yes_bid": 48,
        "yes_ask": 52,
        "volume": volume,
        "close_time": "2036-12-31T00:00:00Z",
    }


class CategoryClient(FakeKalshiClient):
    """Serves GET /markets, which is where Kalshi actually returns quotes.

    The events endpoint's nested markets carry no price fields — verified
    against production, where it caused every one of 50,422 markets to be
    rejected as having a non-finite yes_bid.
    """

    def __init__(self, market_groups):
        super().__init__()
        self._markets = [m for group in market_groups for m in group]

    def list_markets(self, series_ticker=None, status="open", limit=200,
                     cursor=None):
        return {"markets": self._markets, "cursor": None}

    def list_events(self, status="open", limit=200, cursor=None,
                    with_nested_markets=False):
        events = {}
        for m in self._markets:
            key = m.get("event_ticker", "")
            events.setdefault(key, {
                "event_ticker": key,
                "category": m.get("_kalshi_category", ""),
                "title": "Test event",
                "series_ticker": key,
                "markets": [],
            })["markets"].append(m)
        return {"events": list(events.values()), "cursor": None}


def test_scout_classifies_by_ticker_not_by_kalshis_category_string():
    client = CategoryClient([
        # Kalshi calls it something the old config never listed; the taxonomy
        # still places it correctly.
        _event("KXBTCD-25AUG14", [_market("KXBTCD-25AUG14-B")],
               category="Financials"),
    ])
    CONFIG.scout_categories = ["Crypto"]

    candidates = Scout(client).scan()

    assert len(candidates) == 1
    assert candidates[0].category == "Crypto"
    assert candidates[0].taxonomy_category == "Bitcoin"


def test_scout_filters_on_taxonomy_group():
    client = CategoryClient([
        _event("KXBTCD-25AUG14", [_market("KXBTCD-25AUG14-B")]),
        _event("KXNFLGAME-25", [_market("KXNFLGAME-25SEP07")]),
    ])
    CONFIG.scout_categories = ["Sports"]

    candidates = Scout(client).scan()

    assert [c.ticker for c in candidates] == ["KXNFLGAME-25SEP07"]


def test_empty_category_config_takes_everything():
    client = CategoryClient([
        _event("KXBTCD-25AUG14", [_market("KXBTCD-25AUG14-B")]),
        _event("KXNFLGAME-25", [_market("KXNFLGAME-25SEP07")]),
    ])
    CONFIG.scout_categories = []

    assert len(Scout(client).scan()) == 2


def test_liquidity_floor_still_applies():
    client = CategoryClient([
        _event("KXBTCD-25AUG14", [_market("KXBTCD-25AUG14-B", volume=1.0)]),
    ])
    CONFIG.scout_categories = ["Crypto"]

    assert Scout(client).scan() == []


def test_unclassified_markets_are_reported(caplog):
    """New Kalshi series will appear that the ported snapshot never saw. They
    must be visible, not silently traded as group 'Other'."""
    client = CategoryClient([
        _event("KXBRANDNEWSERIES-26", [_market("KXBRANDNEWSERIES-26-A")]),
    ])
    CONFIG.scout_categories = ["Other"]

    with caplog.at_level("WARNING"):
        candidates = Scout(client).scan()

    assert len(candidates) == 1
    assert "KXBRANDNEWSERIES" in caplog.text
    assert "fell outside the ported taxonomy" in caplog.text


def test_a_bogus_configured_category_is_logged_as_an_error(caplog):
    client = CategoryClient([])
    CONFIG.scout_categories = ["Sports", "Nonexistent"]

    with caplog.at_level("ERROR"):
        Scout(client).scan()

    assert "Nonexistent" in caplog.text


def test_audit_reports_agreement_with_kalshis_own_field():
    client = CategoryClient([
        _event("KXBTCD-25AUG14", [_market("KXBTCD-25AUG14-B")], category="Crypto"),
        _event("KXNFLGAME-25", [_market("KXNFLGAME-25SEP07")], category="Financials"),
    ])

    rows = {r["ticker"]: r for r in Scout(client).audit_taxonomy_against_kalshi()}

    assert rows["KXBTCD-25AUG14-B"]["agrees"] is True
    assert rows["KXNFLGAME-25SEP07"]["agrees"] is False
    assert rows["KXNFLGAME-25SEP07"]["taxonomy_group"] == "Sports"
