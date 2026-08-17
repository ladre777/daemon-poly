"""
Who gets asked first when the model budget is smaller than the catalog.

A production pass, repeated every five minutes for hours:

    Pass funnel: 2595 candidate(s) -> ... llm 10 (... capped 2451 ...)

Ten model calls, 2,451 candidates skipped for want of budget — and the ten
went to KXVOTEPRIMARY, KXTRUMPSAY and KXCLARITYVOTE, because politics is what
Kalshi's catalog happens to list first. Weather, named as a main focus,
received nothing on any pass.

Ordering was the whole cause. There is nothing wrong with the caps; the
budget was simply being handed out in catalog order.

Two tiers with different powers, and the difference is the point:

- PRIORITY_KEYWORDS (golf) — asked first *and* exempt from the caps.
- PRIORITY_CATEGORIES (weather) — asked first, still inside the caps.

Asking first is a preference. Asking without limit is a spending decision.
"""
from __future__ import annotations

import pytest

import main
from config import CONFIG
from workers.maker import Proposal

from tests.conftest import make_candidate
from tests.test_pass_loop import (
    StubChecker, StubQuantMaker, StubScout, _positions_follow_fills,
)


class RecordingMaker:
    """Records the order candidates were actually asked about."""

    def __init__(self):
        self.seen: list[str] = []

    def propose(self, candidate):
        self.seen.append(candidate.ticker)
        return Proposal(candidate=candidate, maker_probability=0.70,
                        maker_confidence=0.8, reasoning="stub")


@pytest.fixture
def run_pass(client, order_store, edge_store, account, execution, risk, ledger):
    CONFIG.risk.dry_run = True
    CONFIG.llm_reasoning_categories = {"politics", "weather", "sports"}
    CONFIG.priority_keywords = ["golf"]
    CONFIG.priority_categories = {"weather"}

    def run(candidates):
        maker = RecordingMaker()
        _positions_follow_fills(client, candidates)
        main.run_once(StubScout(candidates), maker, StubQuantMaker(),
                      StubChecker(), risk, execution, ledger, account)
        return maker

    return run


def politics(n):
    return make_candidate(ticker=f"KXVOTE-{n}", title=f"Senate vote {n}",
                          category="Politics", event_ticker=f"KXVOTE-E{n}")


def weather(n):
    return make_candidate(ticker=f"KXHIGHNY-{n}", title=f"High temp NYC {n}",
                          category="Weather", event_ticker=f"KXHIGHNY-E{n}")


def golf(n):
    return make_candidate(ticker=f"PGATOUR-{n}", title=f"PGA golf winner {n}",
                          category="Sports", event_ticker=f"PGATOUR-E{n}")


# -- ordering --------------------------------------------------------------


def test_weather_is_asked_before_politics(run_pass):
    """The production failure, as a test: politics first in the catalog,
    weather never reached."""
    maker = run_pass([politics(1), politics(2), weather(1)])

    assert maker.seen[0] == "KXHIGHNY-1"


def test_a_starved_weather_market_now_gets_the_budget(run_pass):
    """Twelve politics markets ahead of one weather market, and a budget of
    two. Before the ordering change, weather got nothing."""
    CONFIG.max_llm_calls_per_pass = 2
    candidates = [politics(i) for i in range(12)] + [weather(1)]

    maker = run_pass(candidates)

    assert "KXHIGHNY-1" in maker.seen


def test_golf_still_outranks_a_preferred_category(run_pass):
    """Golf is the one family the operator named as always-first."""
    maker = run_pass([weather(1), politics(1), golf(1)])

    assert maker.seen[0] == "PGATOUR-1"


def test_ordering_is_stable_within_a_tier(run_pass):
    """Scout's order is preserved among equals — the sort adds a preference,
    it does not shuffle."""
    maker = run_pass([politics(1), politics(2), politics(3)])

    assert maker.seen == ["KXVOTE-1", "KXVOTE-2", "KXVOTE-3"]


def test_no_candidate_is_dropped_by_the_ordering(run_pass):
    """A sort must not be a filter."""
    candidates = [politics(1), weather(1), golf(1), politics(2)]

    maker = run_pass(candidates)

    assert sorted(maker.seen) == sorted(c.ticker for c in candidates)


# -- ordering is not a spending increase -----------------------------------


def test_a_preferred_category_still_obeys_the_per_pass_cap(run_pass):
    """The distinction that keeps this from being a budget change. Weather
    gets asked first; it does not get asked without limit."""
    CONFIG.max_llm_calls_per_pass = 2
    candidates = [weather(i) for i in range(6)]

    maker = run_pass(candidates)

    assert len(maker.seen) == 2, "preferred is not the same as exempt"


def test_a_preferred_category_still_obeys_the_per_event_cap(run_pass):
    CONFIG.max_llm_calls_per_event = 1
    candidates = [
        make_candidate(ticker=f"KXHIGHNY-T{i}", title="High temp NYC",
                       category="Weather", event_ticker="KXHIGHNY-E1")
        for i in range(4)
    ]

    maker = run_pass(candidates)

    assert len(maker.seen) == 1


def test_golf_remains_exempt_from_the_caps(run_pass):
    """Unchanged behaviour, pinned so the new tier cannot quietly demote it."""
    CONFIG.max_llm_calls_per_pass = 1
    CONFIG.max_llm_calls_per_event = 1
    candidates = [golf(i) for i in range(4)]

    maker = run_pass(candidates)

    assert len(maker.seen) == 4


# -- configuration ---------------------------------------------------------


def test_an_empty_priority_category_set_leaves_scout_order_alone(run_pass):
    CONFIG.priority_categories = set()

    maker = run_pass([politics(1), weather(1)])

    assert maker.seen == ["KXVOTE-1", "KXHIGHNY-1"]


def test_categories_match_case_insensitively(run_pass):
    """Candidate.category is the taxonomy's canonical "Weather"; the config
    is lowercased. A mismatch here would silently do nothing."""
    CONFIG.priority_categories = {"weather"}

    maker = run_pass([politics(1), weather(1)])

    assert maker.seen[0] == "KXHIGHNY-1"
