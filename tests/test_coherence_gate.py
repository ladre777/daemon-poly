"""
Model coherence gates.

The production pass that motivated these, verbatim from the logs — ten WTI
strikes on one contract, one pass:

    strike   model   market
    83.49    48.0%   22.5%
    84.49    45.0%   10.5%
    84.99    32.0%    8.5%
    86.49    45.0%    3.5%
    87.99    45.0%    1.5%

P(WTI > 84.99) = 32% and P(WTI > 86.49) = 45% cannot both be true. Sixteen of
forty-five strike pairs violated it, some by 13 percentage points, while the
market's own prices fell cleanly from 22.5% to 1.5%. The model was not reading
the strike; it emitted roughly 45% for everything, and the whole apparent
edge — 35 percentage points on average — was that artifact.

All ten were caught by the Checker. That is the margin these gates exist to
widen: one LLM's per-trade judgement was the only thing between the bot and
buying deep out-of-the-money contracts at twenty to thirty times fair value.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from workers.coherence import CoherenceGate, log_odds_distance
from workers.maker import Proposal

from tests.conftest import make_candidate


#: The real pass: (floor_strike, model probability, market probability).
WTI_PASS = [
    (83.49, 0.48, 0.225), (83.99, 0.52, 0.165), (84.49, 0.45, 0.105),
    (84.99, 0.32, 0.085), (85.49, 0.35, 0.045), (85.99, 0.42, 0.035),
    (86.49, 0.45, 0.035), (86.99, 0.35, 0.015), (87.49, 0.45, 0.025),
    (87.99, 0.45, 0.015),
]


def proposal(strike, model_p, market_p=0.30, event="KXWTI-26AUG1714",
             strike_type="greater", ticker=None):
    """A Maker proposal on one strike of a ladder.

    market_p is expressed through the book, since that is where
    implied_yes_probability reads it from.
    """
    mid = market_p * 100
    candidate = make_candidate(
        ticker=ticker or f"{event}-T{strike}",
        yes_bid=max(mid - 0.5, 1.0), yes_ask=min(mid + 0.5, 99.0),
        event_ticker=event,
    )
    candidate.strike_type = strike_type
    candidate.floor_strike = strike
    return Proposal(candidate=candidate, maker_probability=model_p,
                    maker_confidence=0.8, reasoning="stub")


@pytest.fixture
def gate():
    g = CoherenceGate()
    g.begin_pass()
    return g


# --------------------------------------------------------------------------
# gate 1: monotonicity across strikes
# --------------------------------------------------------------------------

def test_the_production_pass_is_caught(gate):
    """The whole point. Replayed exactly as it happened."""
    accepted, refused = [], []
    for strike, model_p, market_p in WTI_PASS:
        report = gate.check(proposal(strike, model_p, market_p))
        (accepted if report.ok else refused).append(strike)

    assert refused, "this pass must not sail through"
    assert len(refused) >= 8, (
        f"only {len(refused)} of 10 refused — the model was incoherent across "
        f"the whole ladder"
    )
    assert gate.rejected_monotonicity or gate.rejected_implausible


def test_a_higher_strike_cannot_be_more_likely(gate):
    """The exact contradiction, isolated."""
    assert gate.check(proposal(84.99, 0.32)).ok, "first one has nothing to contradict"

    report = gate.check(proposal(86.49, 0.45))
    assert not report.ok
    assert "higher strike cannot be more likely" in report.reason
    assert "84.99" in report.reason and "86.49" in report.reason


def test_a_coherent_ladder_passes_untouched(gate):
    """A model that reads the strike must not be obstructed."""
    for strike, p in [(83.0, 0.60), (84.0, 0.45), (85.0, 0.30), (86.0, 0.18)]:
        assert gate.check(proposal(strike, p, market_p=0.30)).ok, strike
    assert gate.rejected_monotonicity == 0


def test_order_of_arrival_does_not_matter(gate):
    """Candidates arrive in scan order, not strike order."""
    assert gate.check(proposal(87.0, 0.20)).ok
    assert gate.check(proposal(83.0, 0.60)).ok, "lower strike, higher prob: fine"
    assert not gate.check(proposal(85.0, 0.70)).ok, (
        "70% at 85 contradicts 60% at 83"
    )


def test_a_tainted_event_is_refused_for_the_rest_of_the_pass(gate):
    """Once the model has shown it is not reading the strike, its other
    answers on the same ladder are not trustworthy either."""
    gate.check(proposal(84.99, 0.32))
    assert not gate.check(proposal(86.49, 0.45)).ok      # taints the event

    report = gate.check(proposal(83.49, 0.48))
    assert not report.ok
    assert "already produced contradictory" in report.reason


def test_events_are_independent(gate):
    """One bad ladder must not suppress a different contract."""
    gate.check(proposal(84.99, 0.32, event="KXWTI-A"))
    assert not gate.check(proposal(86.49, 0.45, event="KXWTI-A")).ok

    assert gate.check(proposal(84.99, 0.32, event="KXWTI-B")).ok


def test_less_than_strikes_run_the_other_way(gate):
    """P(X < K) must RISE with the strike."""
    assert gate.check(proposal(83.0, 0.20, strike_type="less")).ok
    assert gate.check(proposal(85.0, 0.40, strike_type="less")).ok
    assert not gate.check(proposal(87.0, 0.30, strike_type="less")).ok


def test_tolerance_absorbs_a_rounding_wobble(gate):
    """A one-point wobble is not evidence of incoherence."""
    CONFIG.risk.coherence_tolerance = 0.01
    assert gate.check(proposal(84.0, 0.400)).ok
    assert gate.check(proposal(85.0, 0.405)).ok, "0.5pp inversion, within tolerance"
    assert not gate.check(proposal(86.0, 0.50)).ok, "10pp is not a wobble"


def test_two_sided_and_unknown_strikes_are_not_ordered(gate):
    """'between' markets have no single ordering against one another."""
    assert gate.check(proposal(84.0, 0.40, strike_type="between")).ok
    assert gate.check(proposal(85.0, 0.55, strike_type="between")).ok


def test_a_market_without_a_strike_is_not_ordered(gate):
    """Weather markets are single-outcome — this gate cannot judge them, and
    must not pretend to."""
    p1 = proposal(84.0, 0.40)
    p1.candidate.floor_strike = None
    p2 = proposal(85.0, 0.55)
    p2.candidate.floor_strike = None
    assert gate.check(p1).ok
    assert gate.check(p2).ok


def test_state_resets_between_passes(gate):
    gate.check(proposal(84.99, 0.32))
    assert not gate.check(proposal(86.49, 0.45)).ok

    gate.begin_pass()
    assert gate.check(proposal(86.49, 0.45)).ok, "a new pass starts clean"


# --------------------------------------------------------------------------
# gate 2: implausible disagreement, measured in log-odds
# --------------------------------------------------------------------------

def test_log_odds_separates_a_real_edge_from_an_absurd_one():
    """Why this is not a flat percentage-point cap.

    Both are ~30-35pp. Only one asserts the market is wrong by a factor of
    fifty, and a points-based cap could not tell them apart.
    """
    weather = log_odds_distance(0.15, 0.50)     # model 15% vs market 50%
    wti = log_odds_distance(0.45, 0.015)        # model 45% vs market 1.5%

    assert weather == pytest.approx(1.73, abs=0.02)
    assert wti == pytest.approx(3.99, abs=0.02)
    assert wti > weather * 2


def test_the_weather_thesis_survives(gate):
    """The user's priority market. A 35pp disagreement against a market at
    50% is a real, defensible view and must not be gated away."""
    CONFIG.risk.max_log_odds_disagreement = 3.0
    p = proposal(None, 0.15, market_p=0.50)
    p.candidate.floor_strike = None
    assert gate.check(p).ok


def test_claiming_a_market_is_wrong_fiftyfold_is_refused(gate):
    CONFIG.risk.max_log_odds_disagreement = 3.0
    p = proposal(None, 0.45, market_p=0.015)
    p.candidate.floor_strike = None

    report = gate.check(p)
    assert not report.ok
    assert "more likely a model error than an edge" in report.reason
    assert gate.rejected_implausible == 1


def test_the_gate_is_symmetric(gate):
    """A model claiming 1.5% against a market at 45% is equally suspect."""
    CONFIG.risk.max_log_odds_disagreement = 3.0
    p = proposal(None, 0.015, market_p=0.45)
    p.candidate.floor_strike = None
    assert not gate.check(p).ok


def test_the_implausibility_gate_can_be_disabled_alone(gate):
    CONFIG.risk.max_log_odds_disagreement = 0.0
    p = proposal(None, 0.45, market_p=0.015)
    p.candidate.floor_strike = None
    assert gate.check(p).ok


def test_logit_is_clamped_not_infinite():
    """A 0 or 1 probability must not produce inf and poison every comparison."""
    import math

    for value in (log_odds_distance(0.0, 0.5), log_odds_distance(1.0, 0.5),
                  log_odds_distance(0.0, 1.0)):
        assert math.isfinite(value)


# --------------------------------------------------------------------------
# the gates can only refuse
# --------------------------------------------------------------------------

def test_disabling_the_checks_restores_previous_behaviour(gate):
    CONFIG.risk.coherence_checks_enabled = False
    for strike, model_p, market_p in WTI_PASS:
        assert gate.check(proposal(strike, model_p, market_p)).ok


def test_the_gate_never_approves_anything_it_was_not_given(gate):
    """Structural: check() returns ok/not-ok on one proposal and has no way to
    introduce, resize, or upgrade a trade."""
    report = gate.check(proposal(84.0, 0.45, market_p=0.40))
    assert report.ok is True
    assert not hasattr(report, "size")
    assert not hasattr(report, "probability")


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

def test_run_once_refuses_an_incoherent_ladder_before_the_checker(
    client, order_store, edge_store, account, execution, risk, ledger
):
    """Caught before a Checker call is spent, and recorded in the ledger with
    no verdict — which is what marks it as never having been asked."""
    import main
    from tests.test_pass_loop import (
        StubChecker, StubMaker, StubQuantMaker, StubScout, _positions_follow_fills,
    )

    CONFIG.risk.dry_run = False
    CONFIG.risk.coherence_checks_enabled = True

    low = make_candidate(ticker="KXWTI-A-T84", event_ticker="KXWTI-A")
    low.strike_type, low.floor_strike = "greater", 84.0
    high = make_candidate(ticker="KXWTI-A-T86", event_ticker="KXWTI-A")
    high.strike_type, high.floor_strike = "greater", 86.0
    candidates = [low, high]
    _positions_follow_fills(client, candidates)

    class LadderMaker(StubMaker):
        """Emits the production failure: the higher strike scored higher.

        Exactly the shape of P(WTI>84.99)=32% alongside P(WTI>86.49)=45%.
        """

        def propose(self, candidate):
            p = super().propose(candidate)
            if p is not None:
                p.maker_probability = (
                    0.65 if candidate.floor_strike >= 86.0 else 0.45
                )
            return p

    checker = StubChecker()
    main.run_once(StubScout(candidates), LadderMaker(), StubQuantMaker(),
                  checker, risk, execution, ledger, account)

    rows = edge_store.recent_edges(limit=10)
    refused = [r for r in rows if r["action_taken"] == "skipped_incoherent"]
    assert refused, "the contradiction must be recorded, not silently dropped"
    assert refused[0]["checker_verdict"] is None, (
        "no verdict — the Checker was never asked, and the row must say so"
    )


# --------------------------------------------------------------------------
# budget diversification
# --------------------------------------------------------------------------
#
# Production spent all ten model calls of every pass on ONE oil contract's
# strike ladder, while weather, crypto and golf — the markets that actually
# matter — got none at all. Worse, the coherence gate then refused that whole
# ladder, so every one of those calls was wasted.


def test_a_tainted_event_is_visible_before_the_next_model_call(gate):
    """So the ladder can be skipped BEFORE paying for another proposal.

    Without this the gate still refuses the trade, but only after the LLM
    call that produced it.
    """
    first = proposal(84.99, 0.32)
    assert not gate.is_tainted(first.candidate), "nothing seen yet"

    gate.check(first)
    contradiction = proposal(86.49, 0.45)
    assert not gate.check(contradiction).ok

    assert gate.is_tainted(proposal(83.49, 0.48).candidate), (
        "every remaining strike on this event is now skippable"
    )


def test_taint_does_not_leak_across_events(gate):
    gate.check(proposal(84.99, 0.32, event="KXWTI-A"))
    gate.check(proposal(86.49, 0.45, event="KXWTI-A"))

    assert gate.is_tainted(proposal(83.0, 0.5, event="KXWTI-A").candidate)
    assert not gate.is_tainted(proposal(83.0, 0.5, event="KXRAIN-B").candidate)


def test_taint_clears_between_passes(gate):
    gate.check(proposal(84.99, 0.32))
    gate.check(proposal(86.49, 0.45))
    assert gate.is_tainted(proposal(83.0, 0.5).candidate)

    gate.begin_pass()
    assert not gate.is_tainted(proposal(83.0, 0.5).candidate)


def test_one_event_cannot_eat_the_whole_model_budget(
    client, order_store, edge_store, account, execution, risk, ledger
):
    """The production failure, as a test.

    Twelve strikes of one contract plus two other events. Without a per-event
    cap the ladder consumes everything and the other markets are never looked
    at — which is exactly what starved weather and crypto.
    """
    import main
    from tests.test_pass_loop import (
        StubChecker, StubMaker, StubQuantMaker, StubScout, _positions_follow_fills,
    )

    CONFIG.risk.dry_run = True
    CONFIG.max_llm_calls_per_event = 2
    CONFIG.max_llm_calls_per_pass = 10
    CONFIG.priority_keywords = []
    CONFIG.llm_reasoning_categories = ["sports"]

    ladder = [
        make_candidate(ticker=f"KXWTI-A-T{i}", event_ticker="KXWTI-A")
        for i in range(12)
    ]
    others = [
        make_candidate(ticker="KXRAIN-B-SFO", event_ticker="KXRAIN-B"),
        make_candidate(ticker="KXBTC-C-T1", event_ticker="KXBTC-C"),
    ]
    candidates = ladder + others
    _positions_follow_fills(client, candidates)

    class CountingMaker(StubMaker):
        def __init__(self):
            super().__init__()
            self.seen: list[str] = []

        def propose(self, candidate):
            self.seen.append(candidate.event_ticker)
            return super().propose(candidate)

    maker = CountingMaker()
    main.run_once(StubScout(candidates), maker, StubQuantMaker(), StubChecker(),
                  risk, execution, ledger, account)

    assert maker.seen.count("KXWTI-A") <= 2, (
        f"the ladder took {maker.seen.count('KXWTI-A')} calls; the cap is 2"
    )
    assert "KXRAIN-B" in maker.seen, "weather must get a look in"
    assert "KXBTC-C" in maker.seen, "crypto must get a look in"


def test_a_priority_ladder_cannot_starve_other_events_either(
    client, order_store, edge_store, account, execution, risk, ledger
):
    """The production failure as it actually shipped.

    ``wti`` is in the default PRIORITY_KEYWORDS, so the starvation test above
    was passing on a candidate set the cap never applied to in production.
    Same twelve-strike ladder, this time on the priority path.
    """
    import main
    from tests.test_pass_loop import (
        StubChecker, StubMaker, StubQuantMaker, StubScout, _positions_follow_fills,
    )

    CONFIG.risk.dry_run = True
    CONFIG.max_llm_calls_per_event = 2
    CONFIG.max_llm_calls_per_pass = 10
    CONFIG.priority_keywords = ["wti"]
    CONFIG.llm_reasoning_categories = ["sports"]

    ladder = [
        make_candidate(ticker=f"KXWTI-A-T{i}", title="WTI crude above",
                       event_ticker="KXWTI-A")
        for i in range(12)
    ]
    others = [
        make_candidate(ticker="KXRAIN-B-SFO", event_ticker="KXRAIN-B"),
        make_candidate(ticker="KXBTC-C-T1", event_ticker="KXBTC-C"),
    ]
    candidates = ladder + others
    _positions_follow_fills(client, candidates)

    class CountingMaker(StubMaker):
        def __init__(self):
            super().__init__()
            self.seen: list[str] = []

        def propose(self, candidate):
            self.seen.append(candidate.event_ticker)
            return super().propose(candidate)

    maker = CountingMaker()
    main.run_once(StubScout(candidates), maker, StubQuantMaker(), StubChecker(),
                  risk, execution, ledger, account)

    assert maker.seen.count("KXWTI-A") <= 2, (
        f"the priority ladder took {maker.seen.count('KXWTI-A')} calls; "
        f"the cap is 2 and priority does not waive it"
    )
    assert "KXRAIN-B" in maker.seen, "weather must get a look in"
    assert "KXBTC-C" in maker.seen, "crypto must get a look in"


def test_priority_markets_are_rationed_per_event_like_everything_else(
    client, order_store, edge_store, account, execution, risk, ledger
):
    """This test used to assert the opposite, and the opposite was wrong.

    Its reasoning was that golf is the family the operator named as
    always-first, so it should be exempt from the per-event cap as it is from
    the per-pass one. But the exemption keyed on PRIORITY_KEYWORDS, whose
    shipped default is:

        golf, pga, masters, liv, btc, bitcoin, eth, high, temperature, rain,
        wti, oil, gas, gold, silver, fed, cpi

    That matches essentially every in-scope candidate — including ``wti``,
    the exact family in ``test_one_event_cannot_eat_the_whole_model_budget``
    above. So the cap that test exists to protect was dead in production: the
    twelve-strike WTI ladder that starved weather and crypto was a priority
    candidate and skipped the cap entirely. Two tests, one asserting the cap
    binds and one asserting it does not, and the second silently won.

    Priority still means what it usefully meant. Priority candidates are
    sorted first, so they are looked at before anything else, and they are
    still waived from the per-*pass* cap — that is what stops an earlier
    event exhausting the budget before a priority one is reached. What they
    no longer get is unlimited calls on a single event, because "first" and
    "unlimited" are different claims and only the first one was intended.
    """
    import main
    from tests.test_pass_loop import (
        StubChecker, StubMaker, StubQuantMaker, StubScout, _positions_follow_fills,
    )

    CONFIG.risk.dry_run = True
    CONFIG.max_llm_calls_per_event = 1
    CONFIG.priority_keywords = ["golf"]
    CONFIG.llm_reasoning_categories = ["sports"]

    candidates = [
        make_candidate(ticker=f"KXGOLF-T{i}", title="PGA golf winner",
                       event_ticker="KXGOLF-A")
        for i in range(4)
    ]
    _positions_follow_fills(client, candidates)

    class CountingMaker(StubMaker):
        def __init__(self):
            super().__init__()
            self.seen: list[str] = []

        def propose(self, candidate):
            self.seen.append(candidate.ticker)
            return super().propose(candidate)

    maker = CountingMaker()
    main.run_once(StubScout(candidates), maker, StubQuantMaker(), StubChecker(),
                  risk, execution, ledger, account)

    assert len(maker.seen) == 1, (
        f"the priority ladder took {len(maker.seen)} calls; the cap is 1"
    )


# --------------------------------------------------------------------------
# sampling a family the gate refuses on every pass
# --------------------------------------------------------------------------
#
# is_tainted forgets at begin_pass, which is right for a ladder that
# contradicts itself inside one pass and blind to the live case: KXHIGHCHI
# and KXHIGHNY refused for implausibility on every pass for hours, the model
# swinging 15%, 35%, 45%, 55% against a market pinned at 0.5%, each swing
# paid for with a Maker call. This samples such a family instead of pricing
# it every pass — and re-probes, so it is never written off.


def _incoherent(gate, family="KXHIGHCHI", n=1):
    """Refuse `family` n times, one pass each, as production does."""
    for _ in range(n):
        gate.begin_pass()
        report = gate.check(proposal(
            80, 0.50, market_p=0.005,
            event=f"{family}-26AUG28", ticker=f"{family}-26AUG28-T80",
        ))
        assert not report.ok, "fixture expects this to be refused"


def test_a_family_is_priced_normally_until_it_earns_the_strikes(gate):
    cand = proposal(80, 0.5, ticker="KXHIGHCHI-26AUG28-T80").candidate
    assert not gate.should_skip_maker(cand)

    _incoherent(gate, n=CONFIG.risk.coherence_family_skip_after - 1)
    assert not gate.should_skip_maker(cand), "one short of the threshold"


def test_a_persistently_incoherent_family_stops_being_priced_every_pass(gate):
    _incoherent(gate, n=CONFIG.risk.coherence_family_skip_after)
    cand = proposal(80, 0.5, ticker="KXHIGHCHI-26AUG28-T80").candidate

    skipped = 0
    for _ in range(20):
        gate.begin_pass()
        if gate.should_skip_maker(cand):
            skipped += 1
    assert skipped >= 17, f"expected most passes skipped, got {skipped}/20"


def test_the_family_is_still_re_probed(gate):
    """The property that keeps this from being a permanent write-off."""
    _incoherent(gate, n=CONFIG.risk.coherence_family_skip_after)
    cand = proposal(80, 0.5, ticker="KXHIGHCHI-26AUG28-T80").candidate

    probed = 0
    for _ in range(CONFIG.risk.coherence_family_reprobe_every * 3):
        gate.begin_pass()
        if not gate.should_skip_maker(cand):
            probed += 1
    assert probed >= 2, "a sampled family must still be asked periodically"


def test_one_coherent_answer_restores_full_rate_pricing(gate):
    """Self-healing, and consecutive rather than cumulative."""
    _incoherent(gate, n=CONFIG.risk.coherence_family_skip_after)
    cand = proposal(80, 0.5, ticker="KXHIGHCHI-26AUG28-T80").candidate
    assert "KXHIGHCHI" in gate.sampled_families(), "precondition: now sampled"

    # A coherent proposal on the same family — model near the market.
    gate.begin_pass()
    ok = gate.check(proposal(
        80, 0.30, market_p=0.28,
        event="KXHIGHCHI-26AUG28", ticker="KXHIGHCHI-26AUG28-T80",
    ))
    assert ok.ok

    for _ in range(5):
        gate.begin_pass()
        assert not gate.should_skip_maker(cand), (
            "a family that answered coherently must go straight back to "
            "full-rate pricing"
        )


def test_one_family_being_sampled_does_not_affect_another(gate):
    _incoherent(gate, family="KXHIGHCHI",
                n=CONFIG.risk.coherence_family_skip_after)
    other = proposal(80, 0.5, ticker="KXHIGHNY-26AUG28-T80").candidate

    for _ in range(10):
        gate.begin_pass()
        assert not gate.should_skip_maker(other), "strikes are per family"


def test_sampling_is_disabled_by_zero(gate, monkeypatch):
    monkeypatch.setattr(CONFIG.risk, "coherence_family_skip_after", 0)
    _incoherent(gate, n=8)
    cand = proposal(80, 0.5, ticker="KXHIGHCHI-26AUG28-T80").candidate

    for _ in range(10):
        gate.begin_pass()
        assert not gate.should_skip_maker(cand)
    assert gate.sampled_families() == {}


def test_sampled_families_are_reportable(gate):
    _incoherent(gate, family="KXHIGHCHI",
                n=CONFIG.risk.coherence_family_skip_after)
    assert "KXHIGHCHI" in gate.sampled_families()


def test_skipping_never_admits_a_proposal_the_gate_would_refuse(gate):
    """The safety property. This must only ever decline to ask.

    should_skip_maker runs before the Maker, so nothing it does can put a
    proposal in front of risk. check() is still the only way past the gate,
    and it is unchanged for anything that reaches it.
    """
    _incoherent(gate, n=CONFIG.risk.coherence_family_skip_after)
    gate.begin_pass()
    still_refused = gate.check(proposal(
        80, 0.50, market_p=0.005,
        event="KXHIGHCHI-26AUG28", ticker="KXHIGHCHI-26AUG28-T80",
    ))
    assert not still_refused.ok, (
        "sampling must not change what check() does to a proposal that "
        "actually reaches it"
    )
