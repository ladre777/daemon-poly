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
    from core.kalshi_categories import _OVERRIDES, _PATTERNS_BY_SPECIFICITY

    lengths = [len(row[0]) for row in _PATTERNS_BY_SPECIFICITY]
    assert lengths == sorted(lengths, reverse=True)
    # Overrides sit alongside the verbatim table rather than editing it.
    assert len(_PATTERNS_BY_SPECIFICITY) == len(SUBCATEGORY_PATTERNS) + len(_OVERRIDES)


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
        # Honours series_ticker the way the real endpoint does. Scout now
        # fetches priority families by series as well as sweeping, and a fake
        # that ignored the filter would return the whole catalog for every
        # targeted call — which is not what production does.
        if series_ticker:
            return {
                "markets": [m for m in self._markets
                            if (m.get("ticker") or "").upper().split("-", 1)[0]
                            == series_ticker.upper()],
                "cursor": None,
            }
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
    """Uses golf rather than NFL: SCOUT_SPORTS_CATEGORIES now restricts the
    Sports group to golf, so an NFL market would be filtered by that rather
    than by the group filter this test is about."""
    client = CategoryClient([
        _event("KXBTCD-25AUG14", [_market("KXBTCD-25AUG14-B")]),
        _event("PGATOUR-25", [_market("PGATOUR-25SEP07")]),
    ])
    CONFIG.scout_categories = ["Sports"]

    candidates = Scout(client).scan()

    assert [c.ticker for c in candidates] == ["PGATOUR-25SEP07"]


def test_empty_category_config_takes_everything():
    """"Everything" still means everything the sports filter permits — the
    two filters are independent and both apply."""
    client = CategoryClient([
        _event("KXBTCD-25AUG14", [_market("KXBTCD-25AUG14-B")]),
        _event("PGATOUR-25", [_market("PGATOUR-25SEP07")]),
    ])
    CONFIG.scout_categories = []

    assert len(Scout(client).scan()) == 2


# -- sports scope ----------------------------------------------------------
#
# Golf is the only sport in scope, and the group cannot express that: golf and
# MLB are both "Sports", so dropping Sports from SCOUT_CATEGORIES would drop
# golf too. Without a category-level filter, MLB player props reached the
# Checker in production — KXMLBKS-26AUG181835NYYBAL-NYYCRODON55-6 was
# evaluated and rejected on 2026-08-18 — spending model budget on a sport
# nobody asked to trade, with no grounding source behind it.


def test_only_golf_survives_inside_the_sports_group():
    client = CategoryClient([
        _event("PGATOUR-25", [_market("PGATOUR-25SEP07")]),
        _event("KXNFLGAME-25", [_market("KXNFLGAME-25SEP07")]),
        _event("MLBGAME-25", [_market("MLBGAME-25SEP07")]),
    ])
    CONFIG.scout_categories = ["Sports"]

    assert [c.ticker for c in Scout(client).scan()] == ["PGATOUR-25SEP07"]


def test_non_sports_groups_are_untouched_by_it():
    """The filter must scope only Sports — weather and crypto go through it
    unchanged."""
    client = CategoryClient([
        _event("KXBTCD-25AUG14", [_market("KXBTCD-25AUG14-B")]),
        _event("KXHIGHNY-25AUG14", [_market("KXHIGHNY-25AUG14-T90")]),
    ])
    CONFIG.scout_categories = []

    assert len(Scout(client).scan()) == 2


def test_the_exclusion_is_reported_as_scope_not_as_invalid_data(caplog):
    """An out-of-scope sport is a deliberate skip, not malformed data.
    Filing it under "invalid" would send the next reader after a parser bug."""
    client = CategoryClient([_event("MLBGAME-25", [_market("MLBGAME-25SEP07")])])
    CONFIG.scout_categories = ["Sports"]

    with caplog.at_level("INFO", logger="daemon_kalshi.scout"):
        Scout(client).scan()

    assert "out of scope" in caplog.text
    assert "as invalid" not in caplog.text


def test_an_empty_allowlist_restores_the_old_behaviour():
    """Escape hatch: SCOUT_SPORTS_CATEGORIES="" scans every sport again."""
    CONFIG.risk.scout_sports_categories = []
    client = CategoryClient([_event("MLBGAME-25", [_market("MLBGAME-25SEP07")])])
    CONFIG.scout_categories = ["Sports"]

    assert len(Scout(client).scan()) == 1


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


# -- scan bounding ----------------------------------------------------------


def test_the_scan_is_page_capped():
    """MEASURED IN PRODUCTION: Kalshi's open catalog is ~50,000 markets, and
    an unbounded scan pages for 6+ minutes. That is worse than slow — a quote
    read at the start of the pass is minutes old by the time risk evaluates
    it, and the freshness gate then throws it away. An unbounded scan does a
    lot of work to collect data it will refuse to use."""
    class EndlessClient(FakeKalshiClient):
        def __init__(self):
            super().__init__()
            self.pages = 0

        def list_markets(self, series_ticker=None, status="open", limit=200,
                         cursor=None):
            self.pages += 1
            return {"markets": [_market(f"KXBTCD-{self.pages}")],
                    "cursor": f"page-{self.pages}"}

    CONFIG.scout_categories = ["Crypto"]
    CONFIG.scout_max_pages = 5
    # Isolates the sweep. Targeted per-series fetches are a separate mechanism
    # with its own bound — see tests/test_targeted_series_fetch.py.
    CONFIG.scout_census_families = []
    client = EndlessClient()

    candidates = Scout(client).scan()

    assert client.pages == 5, "must stop at the cap, not page forever"
    assert len(candidates) == 5


def test_the_cap_can_be_disabled():
    class TwoPageClient(FakeKalshiClient):
        def __init__(self):
            super().__init__()
            self.pages = 0

        def list_markets(self, series_ticker=None, status="open", limit=200,
                         cursor=None):
            self.pages += 1
            return {"markets": [_market(f"KXBTCD-{self.pages}")],
                    "cursor": "next" if self.pages < 2 else None}

    CONFIG.scout_categories = ["Crypto"]
    CONFIG.scout_max_pages = 0
    CONFIG.scout_census_families = []
    client = TwoPageClient()

    assert len(Scout(client).scan()) == 2


# -- production corrections -------------------------------------------------


def test_mve_sports_markets_are_sports_not_esports():
    """VERIFIED AGAINST PRODUCTION: Kalshi's MVE prefix marks a multi-value
    event. Upstream reads it correctly for MVENFL and MVENBA (both Sports)
    but maps MVESPORTSMULTIGAMEEXTENDED to Esports — parsing the prefix as
    MV-ESPORTS rather than MVE-SPORTS.

    Not a marginal error: a live scan of 80,000 open markets put 32,000 of
    them, 40% of the whole catalog, into Esports and skipped them before
    anything looked at a price."""
    assert classify_group("KXMVESPORTSMULTIGAMEEXTENDED-S2026") == "Sports"
    assert classify_group("KXMVESPORTSMULTIGAME-1") == "Sports"
    assert classify_group("KXMVENFLMULTIGAME-25") == "Sports"
    assert classify_group("KXMVENBASINGLEGAME-25") == "Sports"


def test_the_upstream_table_still_says_esports():
    """The override is deliberate and sits outside the verbatim table, so the
    port can still be diffed against upstream."""
    assert get_hierarchy("MVESPORTSMULTIGAMEEXTENDED")[0] == "Esports"


def test_genuine_esports_are_untouched():
    for ticker in ("KXLOLGAMES-25", "KXCSGOGAME-25", "KXLEAGUEWORLDS-26"):
        assert classify_group(ticker) == "Esports", ticker


# -- accidental substring matches -------------------------------------------
#
# VERIFIED AGAINST PRODUCTION, 2026-08-17. Matching is a substring test, so a
# short pattern can be found inside an unrelated English word spelled out in a
# ticker. Sorting by specificity stopped patterns shadowing each other; it
# cannot help when the only match is accidental.


def test_a_political_market_is_not_filed_as_rain():
    """KXDRAINTHESWAMP contains "RAIN". On a live pass it was the ONLY market
    in the Weather group — and Weather sorts to the front of the model-call
    queue, so the misfile was actively spending budget."""
    assert classify_ticker("KXDRAINTHESWAMP-26", "")[0] == "Politics"


def test_a_television_market_is_not_filed_as_ether():
    """KXTVSEASONRELEASETHELASTOFUS contains "ETH", inside "RELEASETHE"."""
    assert classify_ticker("KXTVSEASONRELEASETHELASTOFUS-26", "")[0] == "Entertainment"


def test_the_correction_covers_the_family_not_just_the_one_market():
    """The pattern is chosen to catch the next show too, rather than the
    single ticker that exposed the problem."""
    group, _, sub = classify_ticker("KXTVSEASONRELEASESTRANGER-26", "")

    assert group == "Entertainment"
    assert sub == "Season Release"


def test_a_confident_wrong_answer_is_worse_than_no_answer():
    """Why these are worth correcting individually: "Other" gets reported and
    reviewed, a wrong group routes the market to the wrong grounding source
    and is silent."""
    assert classify_ticker("KXBRANDNEWTHING-26", "")[0] == "Other"


def test_genuine_mid_ticker_matches_still_work():
    """Anchoring the match to the start of the ticker was tried and rejected
    because it breaks these — the pattern legitimately appears mid-ticker."""
    assert classify_ticker("KXFRENCHPRES-27", "")[0] == "Politics"
    assert classify_ticker("KXVPRESNOMR-28", "")[0] == "Politics"


def test_anchoring_would_have_handed_cpi_to_the_electoral_college():
    """The other reason anchoring was rejected: "ECONSTATCPIYOY" starts with
    "EC", which is the two-letter Electoral College pattern."""
    assert classify_ticker("KXECONSTATCPIYOY-26", "")[0] == "Finance"


def test_the_real_weather_families_are_unaffected():
    assert classify_ticker("KXHIGHNY-26", "")[0] == "Weather"
    assert classify_ticker("KXHIGHCHI-26", "")[0] == "Weather"


def test_the_real_crypto_families_are_unaffected():
    assert classify_ticker("KXETHY-26", "")[0] == "Crypto"
    assert classify_ticker("KXBTCY-26", "")[0] == "Crypto"
    assert classify_ticker("KXBTC15M-26AUG", "")[0] == "Crypto"
