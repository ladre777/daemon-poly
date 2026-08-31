"""Two-sided quote generation.

The strategy is Becker's optimism tax, and the whole of it lives in which side
rests. At longshot YES prices taker flow is disproportionately optimistic YES
buying, so the maker wants to be the SELLER of YES: the ask rests and the bid
does not. Near certainty the same flow runs the other way and the bid rests
instead. Getting that backwards would put this bot on the -41% side of the
asymmetry it is built to harvest — which is precisely where its own long-YES
book already sits, 1,144 settled rows with zero wins.

Most of what follows is therefore about refusals and about which side is
suppressed, not about the arithmetic of the midpoint.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from core.pricing import fee_cents_for_order
from workers.quoting import (
    LONGSHOT,
    MID_RANGE,
    NEAR_CERTAIN,
    QuoteGenerator,
    QuoteRefusal,
    TwoSidedQuote,
    zone_for,
)


@pytest.fixture
def gen():
    return QuoteGenerator()


def _q(gen, fair=50.0, bid=45.0, ask=55.0, expiry=86400.0, inventory=0):
    return gen.generate("KXT-1", fair, market_bid_cents=bid,
                        market_ask_cents=ask, seconds_to_expiry=expiry,
                        net_inventory=inventory)


# -- the strategy: which side rests -------------------------------------


def test_longshot_zone_rests_the_ask_only(gen):
    """We sell YES to the optimists. Resting a bid here buys the -41% side."""
    q = _q(gen, fair=8.0, bid=5.0, ask=12.0)
    assert isinstance(q, TwoSidedQuote) and q.zone == LONGSHOT
    assert q.ask_size > 0
    assert q.bid_size == 0, "quoting a bid in the longshot zone buys the optimism"


def test_near_certain_zone_rests_the_bid_only(gen):
    """Same asymmetry reversed: takers overpay for the NO longshot."""
    q = _q(gen, fair=92.0, bid=89.0, ask=95.0)
    assert isinstance(q, TwoSidedQuote) and q.zone == NEAR_CERTAIN
    assert q.bid_size > 0
    assert q.ask_size == 0


def test_mid_range_rests_both_sides(gen):
    q = _q(gen, fair=50.0)
    assert q.zone == MID_RANGE
    assert q.bid_size > 0 and q.ask_size > 0


@pytest.mark.parametrize("fair,zone", [
    (1.0, LONGSHOT), (14.9, LONGSHOT), (15.0, MID_RANGE),
    (50.0, MID_RANGE), (85.0, MID_RANGE), (85.1, NEAR_CERTAIN), (99.0, NEAR_CERTAIN),
])
def test_zone_boundaries(fair, zone):
    assert zone_for(fair) == zone


# -- refusals ------------------------------------------------------------


def test_a_wide_market_is_refused(gen):
    """A book is wide because nobody knows the price. Resting inside it is
    volunteering to be the one who finds out."""
    r = _q(gen, fair=50.0, bid=30.0, ask=70.0)
    assert isinstance(r, QuoteRefusal) and "spread" in r.reason


def test_a_market_near_expiry_is_refused(gen):
    r = _q(gen, expiry=60.0)
    assert isinstance(r, QuoteRefusal) and "expiry" in r.reason


def test_a_crossed_or_one_sided_market_is_refused(gen):
    assert isinstance(_q(gen, bid=55.0, ask=45.0), QuoteRefusal)
    assert isinstance(_q(gen, bid=50.0, ask=50.0), QuoteRefusal)


@pytest.mark.parametrize("fair", [0.0, 100.0, -5.0, 140.0])
def test_a_fair_value_that_is_not_a_probability_is_refused(gen, fair):
    assert isinstance(_q(gen, fair=fair), QuoteRefusal)


def test_skip_mid_range_refuses_only_mid_range(gen, monkeypatch):
    monkeypatch.setattr(CONFIG.quoting, "skip_mid_range", True)
    assert isinstance(_q(gen, fair=50.0), QuoteRefusal)
    assert isinstance(_q(gen, fair=8.0, bid=5.0, ask=12.0), TwoSidedQuote)


def test_a_refusal_names_the_market_and_the_reason(gen):
    """A quoter that silently produces nothing is indistinguishable from one
    that is broken."""
    r = _q(gen, expiry=1.0)
    assert r.ticker == "KXT-1" and r.reason


# -- the spread floor ----------------------------------------------------


def test_the_spread_floor_is_applied_symmetrically(gen, monkeypatch):
    """Widening one side only would quietly change the position the quote
    expresses, which is a different decision from quoting more cautiously."""
    monkeypatch.setattr(CONFIG.quoting, "min_spread_cents", 10.0)
    monkeypatch.setattr(CONFIG.quoting, "mid_edge_cents", 1.0)
    q = _q(gen, fair=50.0, bid=40.0, ask=60.0)

    assert q.spread_cents == pytest.approx(10.0)
    assert q.fair_value_cents - q.bid_cents == pytest.approx(q.ask_cents - q.fair_value_cents)


def test_a_quote_never_rests_outside_1_to_99_cents(gen):
    for fair in (1.5, 2.0, 98.0, 98.5):
        r = _q(gen, fair=fair, bid=max(1.0, fair - 3), ask=min(99.0, fair + 3))
        if isinstance(r, TwoSidedQuote):
            assert 1.0 <= r.bid_cents < r.ask_cents <= 99.0


def test_clamping_that_destroys_the_spread_refuses_rather_than_quoting_it(gen, monkeypatch):
    monkeypatch.setattr(CONFIG.quoting, "min_spread_cents", 30.0)
    r = _q(gen, fair=98.0, bid=95.0, ask=99.0)
    assert isinstance(r, QuoteRefusal) and "floor" in r.reason


@pytest.mark.parametrize("fair", [5.0, 10.0, 15.0, 25.0, 40.0, 50.0,
                                  60.0, 75.0, 85.0, 92.0])
def test_the_spread_floor_is_profitable_at_every_fair_value(gen, fair):
    """A filled round trip must NET something, not break even.

    Kalshi's fee peaks near 50c, which is exactly where a two-sided quote
    sits. The original 4c default produced buy 48 / sell 52: 4c of capture
    against 4c of fees, netting zero at every fair value from 25c to 75c.
    That is not a thin edge, it is no edge, and it would have had the bot
    quoting all day to break even before adverse selection is considered.

    Strictly greater than, deliberately. A floor that merely equals the fee
    is the bug this test exists to keep out.
    """
    q = _q(gen, fair=fair, bid=max(1.0, fair - 8), ask=min(99.0, fair + 8))
    assert isinstance(q, TwoSidedQuote)
    round_trip = (fee_cents_for_order(q.bid_cents, 1)
                  + fee_cents_for_order(q.ask_cents, 1))
    assert q.spread_cents > round_trip, (
        f"at fair {fair}c a {q.spread_cents}c spread nets "
        f"{q.spread_cents - round_trip:+.0f}c against {round_trip}c of fees"
    )


# -- inventory skew ------------------------------------------------------


def test_being_long_suppresses_the_bid(gen):
    q = _q(gen, fair=50.0, inventory=CONFIG.quoting.max_inventory)
    assert q.bid_size == 0
    assert q.ask_size > 0, "reducing the position must stay available"


def test_being_short_suppresses_the_ask(gen):
    q = _q(gen, fair=50.0, inventory=-CONFIG.quoting.max_inventory)
    assert q.ask_size == 0
    assert q.bid_size > 0


def test_inventory_can_only_remove_size_never_add_it(gen):
    base = CONFIG.quoting.size_per_side
    for inv in (-50, -10, -1, 0, 1, 10, 50):
        q = _q(gen, fair=50.0, inventory=inv)
        if isinstance(q, TwoSidedQuote):
            assert q.bid_size <= base and q.ask_size <= base


def test_inventory_suppressing_both_sides_becomes_a_refusal(gen, monkeypatch):
    """An empty quote is not a quote, and must not be returned as one."""
    monkeypatch.setattr(CONFIG.quoting, "size_per_side", 0)
    r = _q(gen, fair=50.0)
    assert isinstance(r, QuoteRefusal) and "suppressed" in r.reason


def test_a_long_position_in_the_longshot_zone_still_rests_its_ask(gen):
    """Inventory must not switch off the side that reduces the position —
    that is how a book gets stuck holding what it wanted to sell."""
    q = _q(gen, fair=8.0, bid=5.0, ask=12.0,
           inventory=CONFIG.quoting.max_inventory)
    assert isinstance(q, TwoSidedQuote) and q.ask_size > 0


# -- structural ----------------------------------------------------------


def test_the_generator_cannot_place_anything(gen):
    """Same guarantee as the arb scanner: it holds no client, no store and no
    execution path, so it cannot rest an order however it is called."""
    for forbidden in ("client", "store", "execution", "place", "submit", "account"):
        assert not hasattr(gen, forbidden)


def test_the_generator_is_stateless_across_calls(gen):
    """A quote must never depend on an inventory or a market that has since
    changed underneath it."""
    a = _q(gen, fair=8.0, bid=5.0, ask=12.0)
    _q(gen, fair=92.0, bid=89.0, ask=95.0, inventory=-99)
    b = _q(gen, fair=8.0, bid=5.0, ask=12.0)
    assert a == b
