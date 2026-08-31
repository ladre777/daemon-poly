"""Adverse selection: the number that decides whether quoting is real.

A maker's problem is not spread arithmetic — ``QuoteGenerator`` already refuses
anything that cannot clear the round-trip fee. The problem is that a resting
quote fills when the market comes to you, which is disproportionately when you
are about to be wrong. If adverse fills outweigh the spread captured, a
strategy with a positive theoretical edge loses money on every fill.

So the tests that matter here are the ones that stop this from flattering
itself, and the ones that stop it from libelling itself:

* a side that was never quoted must never count as filled;
* a market that could not be read must not count as "did not fill";
* a fill that moved against us must be recorded as adverse rather than netted;
* and the mark must come from a strictly later reading than the fill.

That last one is not a stylistic preference, it is the bug this module was
rebuilt to remove, and ``test_the_mark_never_comes_from_the_reading_that_
established_the_fill`` is the test that keeps it removed.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from memory.db import connect
from memory.quote_store import QuoteStore
from workers.adverse_selection import (
    AdverseSelectionResolver,
    mid_cents,
    would_fill_ask,
    would_fill_bid,
)
from workers.quoting import LONGSHOT, MID_RANGE, TwoSidedQuote

T0 = 1000.0


@pytest.fixture
def store(tmp_path):
    return QuoteStore(str(tmp_path / "q.db"))


def _quote(bid=45.0, ask=55.0, bid_size=1, ask_size=1, zone=MID_RANGE,
           fair=50.0, ticker="KXT-1"):
    return TwoSidedQuote(ticker=ticker, zone=zone, fair_value_cents=fair,
                         bid_cents=bid, ask_cents=ask,
                         bid_size=bid_size, ask_size=ask_size)


class _Tape:
    """A market that reads differently at each pass, which is the point.

    The resolver is handed one callable and calls it whenever it needs the
    book. Holding the current reading here — rather than closing over a fixed
    tuple — is what lets a test give the fill check and the mark two different
    markets, exactly as the two passes see in production.
    """

    def __init__(self, book):
        self.book = book

    def __call__(self, ticker):
        return self.book


def _t1_at() -> float:
    return T0 + CONFIG.quoting.observation_min_age_seconds + 1


def _t2_at() -> float:
    return _t1_at() + CONFIG.quoting.mark_horizon_seconds + 1


def _cycle(store, quote, t0_book, t1_book, t2_book):
    """Record a quote, check its fill at t1, mark it at t2. Returns summary."""
    tape = _Tape(t0_book)
    r = AdverseSelectionResolver(store, tape)
    store.record(quote, *t0_book, at=T0)

    tape.book = t1_book
    r.resolve_due(now=_t1_at())
    tape.book = t2_book
    r.resolve_due(now=_t2_at())
    return store.summary()


def _row(store, obs_id=1):
    with connect(store.db_path) as c:
        return dict(c.execute(
            "SELECT * FROM quote_observations WHERE id = ?",
            (obs_id,)).fetchone())


# -- the fill test itself -------------------------------------------------


@pytest.mark.parametrize("bid,market_ask,fills", [
    (45.0, 44.0, True),    # seller crossed below us
    (45.0, 45.0, True),    # seller met us exactly
    (45.0, 46.0, False),   # never came down
])
def test_a_resting_bid_fills_when_a_seller_crosses_down(bid, market_ask, fills):
    assert would_fill_bid(bid, market_ask) is fills


@pytest.mark.parametrize("ask,market_bid,fills", [
    (55.0, 56.0, True), (55.0, 55.0, True), (55.0, 54.0, False),
])
def test_a_resting_ask_fills_when_a_buyer_crosses_up(ask, market_bid, fills):
    assert would_fill_ask(ask, market_bid) is fills


def test_mid_is_the_midpoint():
    assert mid_cents(40.0, 60.0) == 50.0


# -- the two stages must not collapse into one ----------------------------


def test_the_mark_never_comes_from_the_reading_that_established_the_fill(store):
    """The bug this module was rebuilt to remove.

    Marking a fill against the snapshot that established it is degenerate.
    Filling a resting bid at B requires the market ask to fall to B or below,
    and the mid is always strictly below the ask, so ``mid - B`` is negative
    for arithmetic reasons that have nothing to do with the market. Every fill
    would have read as adverse and the metric would have been measuring its
    own definition.

    The structural guarantee is that one sweep can never do both stages for
    the same observation: the fill check writes ``t1 = now``, and the mark is
    only due once ``t1`` is a full mark horizon old. So no matter how stale
    the quote is when the first sweep runs, it comes back unmarked.
    """
    store.record(_quote(), 45.0, 55.0, at=T0)
    out = AdverseSelectionResolver(store, _Tape((40.0, 44.0))).resolve_due(
        now=T0 + 10 * (CONFIG.quoting.observation_min_age_seconds
                       + CONFIG.quoting.mark_horizon_seconds))

    assert out["filled_checked"] == 1
    assert out["marked"] == 0, "one sweep filled and marked the same quote"
    assert _row(store)["t2"] is None


def test_a_marked_observation_records_two_distinct_readings(store):
    _cycle(store, _quote(), (45.0, 55.0), (40.0, 44.0), (50.0, 54.0))
    row = _row(store)

    assert row["t1"] < row["t2"], "the mark must be read after the fill"
    assert (row["t1_yes_bid"], row["t1_yes_ask"]) == (40.0, 44.0)
    assert (row["t2_yes_bid"], row["t2_yes_ask"]) == (50.0, 54.0)


# -- what must never count as a fill --------------------------------------


def test_a_side_that_was_never_quoted_never_fills(store):
    """The longshot zone rests no bid. If a suppressed side could still be
    recorded as filled, the fill statistics would describe a strategy nobody
    is running."""
    q = _quote(bid=5.0, ask=12.0, bid_size=0, ask_size=1, zone=LONGSHOT, fair=8.0)
    # Market collapses through our (unquoted) bid, and never reaches our ask.
    s = _cycle(store, q, (6.0, 10.0), (1.0, 2.0), (1.0, 2.0))

    assert s["bid_fills"] == 0, "a side with zero size was counted as filled"
    assert s["ask_fills"] == 0, "market bid 1c never reached our 12c ask"


def test_an_unreadable_market_is_left_open_not_recorded_as_no_fill(store):
    """A market we could not see is not a market that stood still."""
    store.record(_quote(), 45.0, 55.0, at=T0)
    out = AdverseSelectionResolver(store, lambda t: None).resolve_due(
        now=_t1_at())

    assert out["unreadable"] == 1 and out["filled_checked"] == 0
    assert store.summary()["n"] == 0
    assert len(store.awaiting_fill_check(older_than=_t1_at())) == 1, \
        "an unreadable market must leave the quote checkable on a later pass"


def test_an_unreadable_market_leaves_a_fill_unmarked_rather_than_unmarkable(store):
    """The same guarantee one stage later: a fill whose mark could not be read
    stays in the mark queue instead of being resolved at a made-up price."""
    tape = _Tape((45.0, 55.0))
    r = AdverseSelectionResolver(store, tape)
    store.record(_quote(), 45.0, 55.0, at=T0)
    tape.book = (40.0, 44.0)
    r.resolve_due(now=_t1_at())

    tape.book = None
    out = r.resolve_due(now=_t2_at())

    assert out["unreadable"] == 1 and out["marked"] == 0
    assert len(store.awaiting_mark(older_than=_t2_at())) == 1
    assert _row(store)["bid_mark_cents"] is None


def test_a_quote_younger_than_the_minimum_age_is_not_checked(store):
    """Checking instantly would record a fill rate of zero for a reason that
    has nothing to do with the market."""
    store.record(_quote(), 45.0, 55.0, at=T0)
    out = AdverseSelectionResolver(store, _Tape((40.0, 44.0))).resolve_due(
        now=T0 + 1)

    assert out["filled_checked"] == 0 and out["marked"] == 0


# -- adverse versus favourable -------------------------------------------


def test_a_bid_filled_into_a_falling_market_is_adverse(store):
    """We bought at 45c and the price kept going down. This is the fill a
    maker must count, and the one an optimistic model hides."""
    s = _cycle(store, _quote(bid=45.0, ask=55.0),
               (45.0, 55.0), (40.0, 44.0), (30.0, 34.0))

    assert s["bid_fills"] == 1 and s["ask_fills"] == 0
    assert s["bid_adverse"] == 1
    assert s["bid_mark"] == pytest.approx(32.0 - 45.0)


def test_a_bid_filled_into_a_recovering_market_is_favourable(store):
    """The case the single-stage design could not represent at all.

    Under the old measurement this assertion was unsatisfiable: the mark was
    taken from the reading that produced the fill, so it was negative by
    construction. That a favourable fill can now be recorded is the evidence
    the metric describes the market rather than its own definition.
    """
    s = _cycle(store, _quote(bid=45.0, ask=55.0),
               (45.0, 55.0), (40.0, 44.0), (50.0, 54.0))

    assert s["bid_fills"] == 1
    assert s["bid_adverse"] == 0
    assert s["bid_mark"] == pytest.approx(52.0 - 45.0)


def test_an_ask_filled_into_a_rising_market_is_adverse(store):
    """We sold at 55c and the price kept climbing."""
    s = _cycle(store, _quote(bid=45.0, ask=55.0),
               (45.0, 55.0), (56.0, 60.0), (70.0, 74.0))

    assert s["ask_fills"] == 1 and s["bid_fills"] == 0
    assert s["ask_adverse"] == 1
    assert s["ask_mark"] == pytest.approx(55.0 - 72.0)


def test_an_ask_filled_into_a_fading_market_is_favourable(store):
    s = _cycle(store, _quote(bid=45.0, ask=55.0),
               (45.0, 55.0), (56.0, 60.0), (40.0, 44.0))

    assert s["ask_fills"] == 1
    assert s["ask_adverse"] == 0
    assert s["ask_mark"] == pytest.approx(55.0 - 42.0)


def test_an_unfilled_quote_is_never_marked(store):
    """A mark on a fill that did not happen is a fabricated number, so an
    unfilled observation must never even enter the mark queue."""
    s = _cycle(store, _quote(bid=45.0, ask=55.0),
               (45.0, 55.0), (46.0, 54.0), (10.0, 12.0))

    assert s["bid_fills"] == 0 and s["ask_fills"] == 0
    assert s["bid_mark"] is None and s["ask_mark"] is None
    assert _row(store)["t2"] is None
    assert store.awaiting_mark(older_than=_t2_at()) == []


def test_only_the_filled_side_of_a_one_sided_fill_is_marked(store):
    """Marking the unfilled side too would invent a position we never held."""
    _cycle(store, _quote(bid=45.0, ask=55.0),
           (45.0, 55.0), (40.0, 44.0), (50.0, 54.0))
    row = _row(store)

    assert row["bid_mark_cents"] is not None
    assert row["ask_mark_cents"] is None


def test_both_sides_filling_is_recorded_as_a_round_trip(store):
    """The good case: we captured the spread, and it must be distinguishable
    from two unrelated one-sided fills.

    Note what this test needs to construct it — a crossed book at t1. That is
    not a coincidence, it is the sampling model's third bias: one reading per
    stage means a round trip only registers if both sides are through at the
    same instant. ``round_trips`` is therefore a floor near zero and is not the
    measure of spread capture; the per-side fill counts are.
    """
    q = _quote(bid=45.0, ask=55.0)
    s = _cycle(store, q, (45.0, 55.0), (56.0, 44.0), (50.0, 54.0))

    assert s["bid_fills"] == 1 and s["ask_fills"] == 1
    assert s["round_trips"] == 1
    row = _row(store)
    assert row["bid_mark_cents"] is not None
    assert row["ask_mark_cents"] is not None


# -- reporting ------------------------------------------------------------


def test_zones_are_summarised_separately(store):
    """The zones rest different sides for different reasons. A fill rate
    averaged across them describes none of them."""
    store.record(_quote(zone=LONGSHOT, bid=5.0, ask=12.0, bid_size=0, fair=8.0),
                 6.0, 10.0, at=T0)
    store.record(_quote(zone=MID_RANGE), 45.0, 55.0, at=T0)
    AdverseSelectionResolver(store, _Tape((60.0, 62.0))).resolve_due(
        now=_t1_at())

    assert store.summary(LONGSHOT)["n"] == 1
    assert store.summary(MID_RANGE)["n"] == 1
    assert store.summary()["n"] == 2
    # 60c bid is through the longshot 12c ask and through the mid-range 55c
    # ask; neither bid is through, one of them was never rested at all.
    assert store.summary(LONGSHOT)["ask_fills"] == 1
    assert store.summary(LONGSHOT)["bid_fills"] == 0


def test_each_stage_is_idempotent(store):
    """A repeated sweep must not re-check a fill or re-mark a position."""
    tape = _Tape((45.0, 55.0))
    r = AdverseSelectionResolver(store, tape)
    store.record(_quote(bid=45.0, ask=55.0), 45.0, 55.0, at=T0)

    tape.book = (40.0, 44.0)
    r.resolve_due(now=_t1_at())
    assert r.resolve_due(now=_t1_at())["filled_checked"] == 0

    tape.book = (50.0, 54.0)
    assert r.resolve_due(now=_t2_at())["marked"] == 1
    # A second mark pass must not overwrite the mark with a newer price: the
    # measurement is "where had it gone by the horizon", not "where is it now".
    tape.book = (10.0, 12.0)
    assert r.resolve_due(now=_t2_at() + 10_000)["marked"] == 0

    s = store.summary()
    assert s["n"] == 1 and s["bid_fills"] == 1
    assert s["bid_mark"] == pytest.approx(52.0 - 45.0)


def test_report_names_every_zone_it_has_data_for(store, caplog):
    store.record(_quote(zone=MID_RANGE), 45.0, 55.0, at=T0)
    r = AdverseSelectionResolver(store, _Tape((40.0, 44.0)))
    r.resolve_due(now=_t1_at())

    with caplog.at_level("INFO"):
        r.report()

    text = caplog.text
    assert MID_RANGE in text
    assert LONGSHOT not in text, "a zone with no observations must not be reported"
    assert "bounded estimate" in text, \
        "the sampling caveat must travel with the number"


# -- structural -----------------------------------------------------------


def test_the_resolver_cannot_trade(store):
    r = AdverseSelectionResolver(store, _Tape((45.0, 55.0)))
    for forbidden in ("client", "execution", "place_order", "submit", "cancel"):
        assert not hasattr(r, forbidden)


def test_the_stage_delays_are_configurable(store, monkeypatch):
    monkeypatch.setattr(CONFIG.quoting, "observation_min_age_seconds", 10.0)
    monkeypatch.setattr(CONFIG.quoting, "mark_horizon_seconds", 20.0)
    tape = _Tape((40.0, 44.0))
    r = AdverseSelectionResolver(store, tape)
    store.record(_quote(), 45.0, 55.0, at=T0)

    assert r.resolve_due(now=T0 + 11)["filled_checked"] == 1
    tape.book = (50.0, 54.0)
    assert r.resolve_due(now=T0 + 11 + 21)["marked"] == 1
