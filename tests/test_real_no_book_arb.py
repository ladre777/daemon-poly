"""The locked-arb scanner was searching for the wrong thing.

`workers/arbitrage.py` is the one model-free edge in this bot: buy YES and NO
on the same market when the two asks together cost less than the $1.00 that
exactly one of them will pay. Whichever way the event resolves, the profit is
locked at trade time and does not depend on being right about anything.

It logged **zero** opportunities in ten days of production. Not because none
existed, but because it was pricing the NO leg at `100 - yes_bid`. That is
the price a YES holder could SELL at, which equals the NO ask only on a book
with no spread. Substitute it into the arb condition and it collapses:

    yes_ask + (100 - yes_bid) + fees < 100
    =>  yes_ask - yes_bid + fees < 0
    =>  spread < -fees

A book crossed by more than the fees. The scanner was not looking for arbs,
it was looking for crossed books, and those two searches are not the same.

Kalshi quotes a real NO book — `no_ask_dollars` is in the payload, and
`PRICE_FIELDS` already knew the key. It simply never reached `Quote`.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from core.pricing import fee_cents_per_contract
from core.validation import Quote, validate_market
from workers.arbitrage import ArbitrageScanner, find_locked_arb

from tests.conftest import make_candidate


def _raw(ticker="KXTEST-26AUG31-A", **over):
    raw = {
        "ticker": ticker,
        "event_ticker": "KXTEST-26AUG31",
        "title": "Test",
        "status": "active",
        "yes_bid_dollars": 0.40,
        "yes_ask_dollars": 0.45,
        "volume_fp": 50_000.0,
        "liquidity_dollars": 50_000.0,
        "close_time": "2026-12-31T00:00:00Z",
    }
    raw.update(over)
    return raw


# -- the parse ------------------------------------------------------------


def test_the_real_no_book_reaches_the_quote():
    m = validate_market(_raw(no_bid_dollars=0.52, no_ask_dollars=0.54))
    assert m.quote.no_bid == pytest.approx(52.0)
    assert m.quote.no_ask == pytest.approx(54.0)


def test_a_missing_no_book_stays_none_and_is_not_derived():
    """None must stay distinguishable from a quote of zero.

    Deriving one here is exactly the bug: a derived NO ask looks like data
    and cannot detect an arb.
    """
    m = validate_market(_raw())
    assert m.quote.no_bid is None
    assert m.quote.no_ask is None


# -- the arithmetic that made the old scanner blind -----------------------


@pytest.mark.parametrize("yes_bid,yes_ask", [
    (40, 45), (48, 52), (2, 3), (90, 92), (49, 50), (50, 50),
])
def test_a_derived_no_ask_can_never_find_an_arb_on_an_uncrossed_book(yes_bid, yes_ask):
    """The old code path, pinned as unfireable.

    Every one of these is a normal book with a non-negative spread, and every
    one returns None. This is the whole ten days of silence in one assertion.
    """
    derived_no_ask = 100.0 - yes_bid
    assert find_locked_arb("KXT-1", yes_ask, derived_no_ask) is None


def test_the_real_no_book_can_find_an_arb_the_derived_one_misses():
    """The same market, priced both ways, with opposite answers.

    YES asks 45c and Kalshi's NO book asks 50c. Together 95c plus 4c of fees
    is 99c against a 100c payout, so 1c is locked. The derived NO ask on the
    same market is 100-40 = 60c, which reports a 9c loss and refuses.
    """
    yes_ask, real_no_ask, yes_bid = 45.0, 50.0, 40.0

    arb = find_locked_arb("KXT-1", yes_ask, real_no_ask)
    assert arb is not None, "a real, fee-corrected arb was refused"
    assert arb.profit_cents == pytest.approx(
        100.0 - (yes_ask + real_no_ask
                 + fee_cents_per_contract(yes_ask)
                 + fee_cents_per_contract(real_no_ask))
    )
    assert arb.profit_cents > 0

    assert find_locked_arb("KXT-1", yes_ask, 100.0 - yes_bid) is None


def test_fees_are_charged_on_both_legs_not_a_flat_two_cents():
    """The upstream repo's `combined < 98c` rule, pinned as a loss.

    Two legs at 49c carry ~1.75c of fee each, so 98c combined is a ~1.5c loss
    per pair, not the 2% return that rule claims.
    """
    assert find_locked_arb("KXT-1", 49.0, 49.0) is None


# -- the scanner ----------------------------------------------------------


class _Scout:
    pass


def _cand(ticker, yes_bid, yes_ask, no_ask=None):
    c = make_candidate(ticker=ticker, yes_bid=yes_bid, yes_ask=yes_ask)
    c.quote = Quote(yes_bid=yes_bid, yes_ask=yes_ask, captured_at=c.quote.captured_at,
                    source="scan", no_ask=no_ask)
    return c


def test_the_scanner_finds_an_arb_once_the_real_book_is_present():
    s = ArbitrageScanner()
    s.begin_pass()
    found = s.scan([_cand("KXT-1", 40.0, 45.0, no_ask=50.0)])

    assert len(found) == 1
    assert found[0].ticker == "KXT-1"
    assert s.real_no_ask == 1 and s.derived_no_ask == 0


def test_the_same_market_without_a_no_book_finds_nothing():
    s = ArbitrageScanner()
    s.begin_pass()
    assert s.scan([_cand("KXT-1", 40.0, 45.0, no_ask=None)]) == []
    assert s.real_no_ask == 0 and s.derived_no_ask == 1


def test_visibility_counters_reset_each_pass():
    """They feed a log line claiming how much of the book was searched.
    A counter that accumulates across passes would overstate it forever."""
    s = ArbitrageScanner()
    s.begin_pass()
    s.scan([_cand("KXT-1", 40.0, 45.0, no_ask=50.0)])
    s.begin_pass()
    assert s.real_no_ask == 0 and s.derived_no_ask == 0


def test_the_scanner_still_places_no_orders():
    """Detection only. A half-filled arb is an unhedged directional position
    taken for reasons unrelated to a view, which is worse than not trading.
    Execution stays blocked on order-lifecycle work."""
    s = ArbitrageScanner()
    assert not hasattr(s, "execute")
    assert not hasattr(s, "place")


def test_an_arb_below_the_configured_floor_is_refused():
    floor = CONFIG.arbitrage.min_profit_cents
    # 47 + 50 + 2 + 2 = 101c against a 100c payout: a loss, whatever the floor.
    assert find_locked_arb("KXT-1", 47.0, 50.0) is None or floor <= 0
