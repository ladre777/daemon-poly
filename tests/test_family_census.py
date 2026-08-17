"""
Per-family scan accounting.

A production pass reported 2,595 candidates across five groups including
crypto, 37,144 markets under the liquidity floor — and the quant path
attempting exactly zero. Nothing in that output could distinguish "the
15-minute bitcoin markets are too thin to trade" from "the 15-minute bitcoin
markets were never in the catalog we pulled". Those have different fixes, and
telling them apart required changing code and redeploying.

These tests pin the accounting that makes each of those states report itself.
The census is read-only: it counts what the scan already decided and changes
no filtering.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from workers.scout import FamilyCensus, Scout, family_of

from tests.test_categories import CategoryClient, _event, _market


@pytest.fixture(autouse=True)
def watch_the_crypto_families():
    CONFIG.scout_census_families = ["KXBTC15M", "KXETH", "KXBTCD"]
    CONFIG.scout_categories = ["Crypto"]


def scan(client, caplog):
    with caplog.at_level("INFO", logger="daemon_kalshi.scout"):
        candidates = Scout(client).scan()
    return candidates, caplog.text


# -- the family segment ----------------------------------------------------


def test_family_of_takes_the_series_segment():
    assert family_of("KXBTC15M-26AUG1707-B111500") == "KXBTC15M"
    assert family_of("KXETH-26AUG1702-T2594.99") == "KXETH"


def test_family_of_is_case_insensitive_and_survives_junk():
    assert family_of("kxbtc15m-26aug") == "KXBTC15M"
    assert family_of("KXBTC15M") == "KXBTC15M"
    assert family_of("") == ""
    assert family_of(None) == ""


def test_families_are_not_confused_by_a_shared_prefix():
    """KXBTC is a prefix of KXBTC15M. Counting by prefix rather than by the
    whole segment would fold the 15-minute markets into the catch-all and
    hide exactly the family being investigated."""
    assert family_of("KXBTC15M-26AUG") != family_of("KXBTC-26AUG")


# -- an absent family is an answer -----------------------------------------


def test_a_family_that_never_appears_still_reports(caplog):
    """The case the aggregate line cannot express. Silence here is what sent
    an investigation after the liquidity floor when the markets were simply
    not in the catalog."""
    client = CategoryClient([_event("KXETH-26AUG", [_market("KXETH-26AUG-T1")])])

    _, text = scan(client, caplog)

    assert "Family census KXBTC15M: 0 seen" in text
    assert "family absent from the scanned catalog" in text


def test_a_family_that_appears_reports_its_candidates(caplog):
    client = CategoryClient([_event("KXETH-26AUG", [_market("KXETH-26AUG-T1")])])

    candidates, text = scan(client, caplog)

    assert len(candidates) == 1
    assert "Family census KXETH: 1 seen -> 1 candidate(s)" in text


# -- each disposition is attributed ----------------------------------------


def test_thin_markets_are_reported_against_their_family_with_the_best_seen(caplog):
    """"96 seen, 96 below the $500 floor, best $12" is a different problem
    from "96 seen, 0 candidates" — and points at MIN_LIQUIDITY_USD."""
    client = CategoryClient([
        _event("KXBTC15M-26AUG", [
            _market("KXBTC15M-26AUG-B1", volume=12.0),
            _market("KXBTC15M-26AUG-B2", volume=4.0),
        ]),
    ])

    candidates, text = scan(client, caplog)

    assert candidates == []
    assert "Family census KXBTC15M: 2 seen -> 0 candidate(s)" in text
    assert "2 below the $500 floor (best $12)" in text


def test_a_family_excluded_by_scout_categories_says_so(caplog):
    """Counted before the group filter on purpose: otherwise a misconfigured
    SCOUT_CATEGORIES is indistinguishable from an empty catalog."""
    CONFIG.scout_categories = ["Sports"]
    client = CategoryClient([_event("KXETH-26AUG", [_market("KXETH-26AUG-T1")])])

    _, text = scan(client, caplog)

    assert "Family census KXETH: 1 seen" in text
    assert "outside SCOUT_CATEGORIES" in text


def test_invalid_markets_are_attributed_to_their_family(caplog):
    client = CategoryClient([
        _event("KXETH-26AUG", [
            {**_market("KXETH-26AUG-T1"), "close_time": "1999-01-01T00:00:00Z"},
        ]),
    ])

    candidates, text = scan(client, caplog)

    assert candidates == []
    assert "Family census KXETH: 1 seen -> 0 candidate(s)" in text
    assert "1 invalid" in text


def test_a_mixed_family_reports_every_disposition(caplog):
    client = CategoryClient([
        _event("KXETH-26AUG", [
            _market("KXETH-26AUG-T1"),                       # accepted
            _market("KXETH-26AUG-T2", volume=3.0),           # too thin
            {**_market("KXETH-26AUG-T3"),
             "close_time": "1999-01-01T00:00:00Z"},          # invalid
        ]),
    ])

    candidates, text = scan(client, caplog)

    assert len(candidates) == 1
    assert "Family census KXETH: 3 seen -> 1 candidate(s)" in text
    assert "1 below the $500 floor" in text
    assert "1 invalid" in text


# -- the census cannot change what trades ----------------------------------


def test_watching_a_family_does_not_change_which_candidates_are_returned():
    """Purely observational. If this ever fails, the census has grown a side
    effect and become a trading decision."""
    markets = [
        _event("KXETH-26AUG", [_market("KXETH-26AUG-T1")]),
        _event("KXBTCD-26AUG", [_market("KXBTCD-26AUG-B1")]),
    ]

    CONFIG.scout_census_families = []
    without = [c.ticker for c in Scout(CategoryClient(markets)).scan()]

    CONFIG.scout_census_families = ["KXETH", "KXBTCD", "KXBTC15M"]
    with_census = [c.ticker for c in Scout(CategoryClient(markets)).scan()]

    assert without == with_census


def test_an_empty_census_config_logs_nothing(caplog):
    CONFIG.scout_census_families = []
    client = CategoryClient([_event("KXETH-26AUG", [_market("KXETH-26AUG-T1")])])

    _, text = scan(client, caplog)

    assert "Family census" not in text


def test_blank_entries_in_the_config_are_ignored(caplog):
    CONFIG.scout_census_families = ["KXETH", "", "  "]
    client = CategoryClient([_event("KXETH-26AUG", [_market("KXETH-26AUG-T1")])])

    _, text = scan(client, caplog)

    assert text.count("Family census") == 1


def test_lowercase_config_entries_still_match(caplog):
    CONFIG.scout_census_families = ["kxeth"]
    client = CategoryClient([_event("KXETH-26AUG", [_market("KXETH-26AUG-T1")])])

    _, text = scan(client, caplog)

    assert "Family census KXETH: 1 seen -> 1 candidate(s)" in text


# -- the summary itself ----------------------------------------------------


def test_summary_of_an_untouched_family_names_the_absence():
    assert "absent" in FamilyCensus().summary()


def test_summary_omits_dispositions_that_did_not_happen():
    tally = FamilyCensus(seen=3, accepted=3)

    summary = tally.summary()

    assert summary == "3 seen -> 3 candidate(s)"
    assert "invalid" not in summary


# -- self-discovery: what is actually there --------------------------------
#
# The watch list is a guess about ticker prefixes, and the first one shipped
# was wrong twice: KXHIGHNY and PGATOUR both reported "0 seen" because Kalshi
# does not name those families that way. A watch list can only report on names
# someone thought of.


def test_the_families_that_produced_candidates_are_named(caplog):
    CONFIG.scout_categories = ["Crypto"]
    client = CategoryClient([
        _event("KXETH-26AUG", [_market("KXETH-26AUG-T1"),
                               _market("KXETH-26AUG-T2")]),
        _event("KXBTCD-26AUG", [_market("KXBTCD-26AUG-B1")]),
    ])

    _, text = scan(client, caplog)

    assert "Tradeable families in Crypto:" in text
    assert "KXETH x2" in text
    assert "KXBTCD x1" in text


def test_discovery_needs_no_watch_list_entry(caplog):
    """The whole point: a family nobody configured still gets named."""
    CONFIG.scout_census_families = []
    CONFIG.scout_categories = ["Crypto"]
    client = CategoryClient([_event("KXBTCD-26AUG", [_market("KXBTCD-26AUG-B1")])])

    _, text = scan(client, caplog)

    assert "Family census" not in text
    assert "KXBTCD x1" in text


def test_families_are_ordered_by_how_many_candidates_they_produced(caplog):
    CONFIG.scout_categories = ["Crypto"]
    client = CategoryClient([
        _event("KXBTCD-26AUG", [_market("KXBTCD-26AUG-B1")]),
        _event("KXETH-26AUG", [_market(f"KXETH-26AUG-T{i}") for i in range(3)]),
    ])

    _, text = scan(client, caplog)

    line = [ln for ln in text.splitlines() if "Tradeable families" in ln][0]
    assert line.index("KXETH x3") < line.index("KXBTCD x1")


def test_a_family_with_no_tradeable_market_is_not_listed_as_tradeable(caplog):
    CONFIG.scout_categories = ["Crypto"]
    client = CategoryClient([
        _event("KXETH-26AUG", [_market("KXETH-26AUG-T1", volume=1.0)]),
    ])

    _, text = scan(client, caplog)

    assert "Tradeable families" not in text


# -- multi-event shards ----------------------------------------------------
#
# Kalshi lists "multi-value event" markets: one contract standing for a
# combination of legs. A typical scan returned KXMVECROSSCATEGORY x1783 and
# KXMVESPORTSMULTIGAMEEXTENDED x934 — 2,717 of 2,863 candidates — and they
# crowded the per-pass model budget out of the 15-minute, hourly and weather
# families that are actually in scope.
#
# Nothing here can price them. A parlay resolves on the joint outcome of
# several events and this bot has no joint model and no grounding for one, so
# the Maker was handed a title and guessed. Live, that produced "model says
# 72% against a market at 0.7%" — 5.97 in log-odds, refused by the coherence
# gate after the model call had already been paid for.


def _shard(ticker="KXBTCD-26AUG17-SHARD1", **extra):
    """A parlay shard that is otherwise perfectly tradeable: liquid, valid,
    and in a watched family. Only the mve_* fields make it different."""
    m = _market(ticker)
    m.update(extra)
    return m


@pytest.fixture
def watch_everything():
    CONFIG.scout_census_families = ["KXBTCD", "KXBTC15M"]
    CONFIG.scout_categories = ["Crypto"]
    CONFIG.risk.skip_multi_event_shards = True


def test_a_shard_is_recognised_by_its_own_fields_not_its_ticker(
    watch_everything, caplog
):
    """Kalshi labels these on the payload, so read that rather than
    pattern-matching a name — a ticker convention can change under us, and
    KXBTCD is a real family whose ordinary markets must survive."""
    client = CategoryClient([[
        _shard("KXBTCD-26AUG17-S1", mve_collection_ticker="KXMVECROSS"),
        _shard("KXBTCD-26AUG17-S2", mve_selected_legs=[{"ticker": "A"}]),
        _market("KXBTCD-26AUG17-T64000"),
    ]])

    candidates, _ = scan(client, caplog)

    assert [c.ticker for c in candidates] == ["KXBTCD-26AUG17-T64000"]


def test_the_exclusion_is_counted_rather_than_silent(watch_everything, caplog):
    """"0 candidates" with no reason is indistinguishable from a broken scan.
    Every other filter in this file reports its count; so does this one."""
    client = CategoryClient([[
        _shard("KXBTCD-26AUG17-S1", mve_collection_ticker="KXMVECROSS"),
    ]])

    _, text = scan(client, caplog)

    assert "multi-event shard" in text


def test_the_exclusion_is_operator_reversible(watch_everything, caplog):
    """A judgement about what this bot can price, not a safety invariant."""
    CONFIG.risk.skip_multi_event_shards = False
    client = CategoryClient([[
        _shard("KXBTCD-26AUG17-S1", mve_collection_ticker="KXMVECROSS"),
    ]])

    candidates, _ = scan(client, caplog)

    assert len(candidates) == 1


def test_shards_still_count_as_seen_in_the_census(watch_everything, caplog):
    """The census exists to explain where the catalog went. A family whose
    markets are all shards must not read "0 seen", which would point at
    pagination instead of at this filter."""
    client = CategoryClient([[
        _shard("KXBTCD-26AUG17-S1", mve_collection_ticker="KXMVECROSS"),
        _shard("KXBTCD-26AUG17-S2", mve_collection_ticker="KXMVECROSS"),
    ]])

    _, text = scan(client, caplog)

    assert "KXBTCD: 2 seen" in text
