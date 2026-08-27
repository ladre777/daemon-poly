"""
The Checker stops re-answering one question once per strike.

A strike ladder is many markets on one ``event_ticker`` priced from a single
model run — one sigma, one spot, one horizon. Sending every strike to the
Checker asks a second model the same question repeatedly. Measured over 501
rows / 15 passes, 14 deep ladders (14% of ladders) generated 55% of all
Checker calls.

This is quota headroom and signal quality, not a cost measure: the dollar
saving is around $3/day and no cap fits a 500/day free tier. What it buys is
a pass that is not dominated by whichever event happened to have the deepest
ladder.
"""
from __future__ import annotations

from workers.ladder_dedup import group_key, select_for_checker
from workers.maker import Proposal

from tests.conftest import make_candidate


def proposal(event, strike, probability, source="quant"):
    """A proposal whose |edge| is set by how far the model is from the market.

    The market sits at 50% (bid/ask 48/52), so a model probability above it
    is a YES and below it is a NO — which is what makes direction part of the
    grouping key rather than an afterthought.
    """
    candidate = make_candidate(
        ticker=f"{event}-T{strike}", event_ticker=event,
        title=f"above {strike}",
    )
    return Proposal(
        candidate=candidate,
        maker_probability=probability,
        maker_confidence=0.8,
        reasoning="x",
        source=source,
    )


# -- the grouping key --------------------------------------------------------


def test_strikes_on_one_event_and_side_share_a_key():
    a = proposal("KXBTCD-26", 100, 0.90)
    b = proposal("KXBTCD-26", 200, 0.85)

    assert group_key(a) == group_key(b)


def test_the_two_sides_of_one_event_are_kept_apart():
    """"YES above 84.49" and "NO above 84.49" are different trades against
    the same model output. Collapsing them lets one side crowd out the
    other."""
    yes = proposal("KXWTI-26", 84, 0.90)
    no = proposal("KXWTI-26", 84, 0.10)

    assert yes.direction == "yes" and no.direction == "no"
    assert group_key(yes) != group_key(no)


def test_different_events_never_share_a_key():
    assert group_key(proposal("KXBTCD-26", 1, 0.9)) != group_key(
        proposal("KXETHD-26", 1, 0.9)
    )


# -- the cap -----------------------------------------------------------------


def test_a_ladder_at_or_under_the_cap_is_untouched():
    proposals = [proposal("KXBTCD-26", i, 0.60 + i / 100) for i in range(3)]

    kept, dropped = select_for_checker(proposals, cap=3)

    assert dropped == 0
    assert kept == proposals


def test_a_deep_ladder_is_cut_to_the_cap():
    proposals = [proposal("KXBTCD-26", i, 0.60 + i / 100) for i in range(14)]

    kept, dropped = select_for_checker(proposals, cap=3)

    assert len(kept) == 3
    assert dropped == 11


def test_the_survivors_are_the_widest_disagreements():
    """The point of ranking rather than truncating: what reaches the Checker
    is where the model and the market disagree most — the strike actually
    worth a second opinion — not whichever three arrived first."""
    weak = proposal("KXBTCD-26", 1, 0.55)
    mid = proposal("KXBTCD-26", 2, 0.70)
    strong = proposal("KXBTCD-26", 3, 0.95)
    weakest = proposal("KXBTCD-26", 4, 0.51)

    kept, _ = select_for_checker([weak, mid, strong, weakest], cap=2)

    assert set(id(p) for p in kept) == {id(strong), id(mid)}


def test_the_no_side_is_ranked_by_magnitude_too():
    """``edge_size`` is the executable edge in favour of whichever side the
    proposal took, so it is positive on both. Ranking is on ``|edge|`` as
    specified — the abs() is defensive rather than load-bearing — and the NO
    side is ranked exactly like the YES side."""
    strong_no = proposal("KXBTCD-26", 1, 0.02)
    weak_no = proposal("KXBTCD-26", 2, 0.45)

    assert strong_no.direction == weak_no.direction == "no"
    assert strong_no.edge_size > weak_no.edge_size > 0

    kept, _ = select_for_checker([weak_no, strong_no], cap=1)

    assert kept == [strong_no]


def test_a_strong_no_never_competes_with_a_weak_yes():
    """They are different groups, so a pass cannot be quietly biased long by
    a deep YES ladder crowding out the shorts on the same event."""
    strong_no = proposal("KXBTCD-26", 1, 0.02)
    weak_yes = proposal("KXBTCD-26", 2, 0.55)

    kept, dropped = select_for_checker([strong_no, weak_yes], cap=1)

    assert dropped == 0
    assert kept == [strong_no, weak_yes]


def test_each_event_and_side_gets_its_own_allowance():
    """The cap is per group, not per pass — a deep ladder on one event must
    not consume another event's budget."""
    btc = [proposal("KXBTCD-26", i, 0.60 + i / 100) for i in range(5)]
    eth = [proposal("KXETHD-26", i, 0.60 + i / 100) for i in range(5)]

    kept, dropped = select_for_checker(btc + eth, cap=3)

    assert len(kept) == 6
    assert dropped == 4


def test_the_original_order_is_preserved_among_survivors():
    """Selection only removes. The upstream priority sort still decides what
    is looked at first."""
    proposals = [proposal("KXBTCD-26", i, 0.50 + i / 100) for i in range(6)]

    kept, _ = select_for_checker(proposals, cap=3)

    assert kept == sorted(kept, key=proposals.index)


def test_ties_break_on_arrival_so_a_pass_is_reproducible():
    a = proposal("KXBTCD-26", 1, 0.80)
    b = proposal("KXBTCD-26", 2, 0.80)

    assert select_for_checker([a, b], cap=1)[0] == [a]
    assert select_for_checker([b, a], cap=1)[0] == [b]


# -- the off switch ----------------------------------------------------------


def test_a_zero_cap_disables_the_rule_entirely():
    """Turning it off must not need a code change — this is a strategy
    surface, and the operator has to be able to revert it from config."""
    proposals = [proposal("KXBTCD-26", i, 0.60 + i / 100) for i in range(14)]

    kept, dropped = select_for_checker(proposals, cap=0)

    assert kept == proposals
    assert dropped == 0


def test_an_empty_pass_is_not_a_special_case():
    assert select_for_checker([], cap=3) == ([], 0)


# -- it only ever removes ----------------------------------------------------


def test_the_cap_can_never_add_or_alter_a_proposal():
    """Same guarantee CoherenceGate makes: this gate cannot approve anything
    or change a number, only drop."""
    proposals = [proposal("KXBTCD-26", i, 0.60 + i / 100) for i in range(9)]
    before = [(p.candidate.ticker, p.maker_probability) for p in proposals]

    kept, dropped = select_for_checker(proposals, cap=3)

    assert len(kept) + dropped == len(proposals)
    assert all(p in proposals for p in kept)
    assert [(p.candidate.ticker, p.maker_probability) for p in proposals] == before


# -- wired into the pass ------------------------------------------------------


def test_the_cap_actually_binds_inside_a_pass(
    client, order_store, edge_store, account, execution, risk, ledger
):
    """The unit tests above prove the rule; this proves it is plugged in.

    A fourteen-strike ladder through the real ``run_once``. Without the cap
    every strike that clears coherence reaches the Checker — that is the 55%
    of Checker calls the deep ladders were generating.
    """
    import main
    from config import CONFIG
    from tests.test_pass_loop import (
        StubMaker, StubQuantMaker, StubScout, _positions_follow_fills,
    )

    CONFIG.risk.dry_run = True
    CONFIG.max_llm_calls_per_event = 0          # per-event cap off, so the
    CONFIG.max_llm_calls_per_pass = 0           # Checker cap is what is tested
    CONFIG.priority_keywords = []
    CONFIG.llm_reasoning_categories = ["sports"]
    CONFIG.max_checker_calls_per_event_direction = 3

    candidates = [
        make_candidate(ticker=f"KXBTCD-26-T{i}", event_ticker="KXBTCD-26")
        for i in range(14)
    ]
    _positions_follow_fills(client, candidates)

    class CountingChecker:
        def __init__(self):
            self.seen: list[str] = []

        def check(self, proposal):
            from workers.checker import Verdict

            self.seen.append(proposal.candidate.ticker)
            return Verdict(proposal=proposal, verdict="reject",
                           confidence=0.9, reasoning="stub")

    checker = CountingChecker()
    main.run_once(StubScout(candidates), StubMaker(), StubQuantMaker(),
                  checker, risk, execution, ledger, account)

    assert len(checker.seen) == 3, (
        f"the Checker saw {len(checker.seen)} strikes of one ladder; "
        f"the cap is 3"
    )


def test_turning_the_cap_off_restores_the_old_behaviour(
    client, order_store, edge_store, account, execution, risk, ledger
):
    """The revert path, exercised rather than assumed."""
    import main
    from config import CONFIG
    from tests.test_pass_loop import (
        StubMaker, StubQuantMaker, StubScout, _positions_follow_fills,
    )

    CONFIG.risk.dry_run = True
    CONFIG.max_llm_calls_per_event = 0
    CONFIG.max_llm_calls_per_pass = 0
    CONFIG.priority_keywords = []
    CONFIG.llm_reasoning_categories = ["sports"]
    CONFIG.max_checker_calls_per_event_direction = 0

    candidates = [
        make_candidate(ticker=f"KXBTCD-26-T{i}", event_ticker="KXBTCD-26")
        for i in range(6)
    ]
    _positions_follow_fills(client, candidates)

    class CountingChecker:
        def __init__(self):
            self.seen: list[str] = []

        def check(self, proposal):
            from workers.checker import Verdict

            self.seen.append(proposal.candidate.ticker)
            return Verdict(proposal=proposal, verdict="reject",
                           confidence=0.9, reasoning="stub")

    checker = CountingChecker()
    main.run_once(StubScout(candidates), StubMaker(), StubQuantMaker(),
                  checker, risk, execution, ledger, account)

    assert len(checker.seen) == 6
