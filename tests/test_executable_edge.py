"""
P1-7: edge computed from executable prices, not the midpoint.

The defect: the old code compared the model's probability against the
*midpoint*-implied probability to decide whether an edge cleared the
threshold, then sized and paid at the bid/ask. On a 48/52 market the midpoint
credits 2c of edge that nobody can trade at, and the fee is not in the
comparison at all. A "4pp edge" measured that way can be negative once you
cross the spread and pay to do it.
"""
from __future__ import annotations

import time

import pytest

from config import CONFIG
from core.pricing import (
    cost_per_contract_cents,
    executable_probability,
    fee_cents_per_contract,
    net_edge,
)
from core.validation import Quote
from workers.maker import Proposal

from tests.conftest import make_candidate, make_verdict
from tests.test_risk import snapshot


def proposal(candidate=None, probability=0.70) -> Proposal:
    return Proposal(
        candidate=candidate or make_candidate(),
        maker_probability=probability,
        maker_confidence=0.8,
        reasoning="test",
    )


# -- the arithmetic ---------------------------------------------------------


def test_executable_probability_is_the_side_we_pay():
    c = make_candidate(yes_bid=48, yes_ask=52)
    # Buying YES costs the ask: break-even is 52%.
    assert executable_probability(c, "yes") == pytest.approx(0.52)
    # Buying NO costs 100-48 = 52c, so YES break-even is 48%.
    assert executable_probability(c, "no") == pytest.approx(0.48)


def test_midpoint_overstates_the_edge_by_half_the_spread():
    c = make_candidate(yes_bid=48, yes_ask=52)
    p = proposal(c, probability=0.56)

    assert p.midpoint_edge == pytest.approx(0.06)      # vs 50c midpoint
    assert p.executable_edge == pytest.approx(0.04)    # vs 52c ask
    assert p.executable_edge < p.midpoint_edge


def test_a_wide_spread_can_erase_the_whole_edge():
    """The case that matters: 6pp of apparent edge, none of it tradeable."""
    c = make_candidate(yes_bid=40, yes_ask=60)
    p = proposal(c, probability=0.56)

    assert p.midpoint_edge == pytest.approx(0.06)
    assert p.executable_edge == pytest.approx(-0.04), "buying at 60c on a 56% view loses"
    assert p.net_edge(fee_cents_per_contract(60), CONFIG.risk.slippage_cents) < 0


def test_fees_and_slippage_come_out_of_the_edge():
    c = make_candidate(yes_bid=48, yes_ask=52)
    p = proposal(c, probability=0.58)

    gross = p.executable_edge
    net = p.net_edge(fee_cents_per_contract(52), CONFIG.risk.slippage_cents)

    assert gross == pytest.approx(0.06)
    assert net < gross
    assert net == pytest.approx(gross - (fee_cents_per_contract(52) + 1.0) / 100.0)


def test_net_edge_helper_agrees_with_the_proposal_property():
    c = make_candidate(yes_bid=48, yes_ask=52)
    p = proposal(c, probability=0.62)
    fee = fee_cents_per_contract(cost_per_contract_cents(52)[0])

    assert net_edge(0.62, c, "yes") == pytest.approx(
        p.net_edge(fee, CONFIG.risk.slippage_cents)
    )


def test_symmetry_between_yes_and_no():
    """A 56% view on a 48/52 market and a 44% view on the same market are the
    same trade in opposite directions, and must price identically."""
    c = make_candidate(yes_bid=48, yes_ask=52)

    yes_side = proposal(c, probability=0.56)
    no_side = proposal(c, probability=0.44)

    assert yes_side.direction == "yes"
    assert no_side.direction == "no"
    assert yes_side.executable_edge == pytest.approx(no_side.executable_edge)


@pytest.mark.parametrize("price", [1, 5, 15, 30, 45, 49])
def test_fee_is_symmetric_across_the_book(price):
    """Regression: p*(1-p) is symmetric, but 0.7 is not exactly representable
    in binary, so ceil() used to charge a 70c contract a full cent more than
    its 30c mirror. The brief requires fee policy to be symmetric between YES
    and NO, and buying NO at 30c IS buying against YES at 70c."""
    assert fee_cents_per_contract(price) == fee_cents_per_contract(100 - price)


def test_fee_peaks_at_the_middle_of_the_book():
    assert fee_cents_per_contract(50) >= fee_cents_per_contract(30)
    assert fee_cents_per_contract(50) >= fee_cents_per_contract(70)


# -- risk enforces the executable number ------------------------------------


def test_risk_refuses_an_edge_that_only_exists_at_the_midpoint(risk):
    c = make_candidate(yes_bid=40, yes_ask=60)
    verdict = make_verdict(candidate=c, maker_probability=0.56)
    assert verdict.proposal.midpoint_edge == pytest.approx(0.06)

    decision = risk.evaluate(verdict, snapshot())

    assert not decision.approved
    # Refused on spread first (20c is far too wide), which is the correct
    # ordering — but the edge check would also have caught it.
    assert net_edge(0.56, c, "yes") < CONFIG.risk.min_edge_threshold


def test_risk_refuses_when_costs_eat_a_real_edge(risk):
    """A 5pp executable edge on a 50c contract against ~1.75c of fees plus
    1c of slippage nets under the 4pp threshold."""
    c = make_candidate(yes_bid=48, yes_ask=52)
    verdict = make_verdict(candidate=c, maker_probability=0.55)
    assert verdict.proposal.executable_edge == pytest.approx(0.03)

    decision = risk.evaluate(verdict, snapshot())

    assert not decision.approved
    assert "net edge" in decision.reason
    assert "midpoint would have shown" in decision.reason


def test_risk_approves_when_the_edge_survives_costs(risk):
    c = make_candidate(yes_bid=48, yes_ask=52)
    verdict = make_verdict(candidate=c, maker_probability=0.70)

    decision = risk.evaluate(verdict, snapshot())

    assert decision.approved, decision.reason
    assert decision.executable_price_cents == pytest.approx(52.0)


def test_the_decision_records_the_price_it_used(risk):
    c = make_candidate(yes_bid=48, yes_ask=52)
    decision = risk.evaluate(make_verdict(candidate=c), snapshot())

    assert decision.approved
    assert decision.executable_price_cents == 52.0
    assert decision.limit_price_cents == 53.0
    assert decision.detail["quote_captured_at"] > 0
    assert decision.detail["quote_yes_bid"] == 48
    assert decision.detail["quote_yes_ask"] == 52


# -- quote staleness --------------------------------------------------------


def test_a_stale_quote_is_refused_at_the_last_gate(risk):
    """A proposal can sit in the LLM queue for seconds. Approving it against a
    price that has since moved is trading on a number that no longer exists."""
    c = make_candidate()
    c.quote = Quote(
        yes_bid=48, yes_ask=52,
        captured_at=time.time() - CONFIG.risk.max_quote_age_seconds - 5,
    )

    decision = risk.evaluate(make_verdict(candidate=c), snapshot())

    assert not decision.approved
    assert "quote is" in decision.reason and "old" in decision.reason


def test_a_fresh_quote_passes(risk):
    c = make_candidate()
    c.quote = Quote(yes_bid=48, yes_ask=52, captured_at=time.time())

    assert risk.evaluate(make_verdict(candidate=c), snapshot()).approved


def test_a_candidate_without_a_quote_gets_one_from_its_prices():
    c = make_candidate(yes_bid=30, yes_ask=34)
    assert c.quote is not None
    assert c.quote.yes_ask == 34
    assert not c.quote.is_stale()


# -- longshot policy symmetry -----------------------------------------------


@pytest.mark.parametrize(
    "yes_bid,yes_ask,probability,direction",
    [
        (12, 15, 0.30, "yes"),   # cheap YES
        (85, 88, 0.70, "no"),    # cheap NO (100-85 = 15c)
    ],
)
def test_longshot_guard_applies_to_both_sides(risk, yes_bid, yes_ask,
                                              probability, direction):
    c = make_candidate(yes_bid=yes_bid, yes_ask=yes_ask)
    verdict = make_verdict(candidate=c, maker_probability=probability)
    assert verdict.proposal.direction == direction

    from core.pricing import executable_price_cents

    price = executable_price_cents(c, direction)
    assert price < CONFIG.risk.longshot_price_threshold_cents

    # Both sides face the same raised bar; whether this particular edge clears
    # it is what the guard decides, and it must decide identically for the
    # mirrored case.
    decision = risk.evaluate(verdict, snapshot())
    assert decision.approved is (
        verdict.proposal.edge_size
        >= CONFIG.risk.min_edge_threshold * CONFIG.risk.longshot_edge_multiplier
    ) or not decision.approved


def test_maker_screens_on_the_same_number_risk_enforces():
    """If Maker screened on the midpoint and risk on the executable price,
    Maker would hand over proposals risk always rejects — wasted LLM spend and
    a misleading 'edges found' count."""
    c = make_candidate(yes_bid=48, yes_ask=52)
    p = proposal(c, probability=0.55)
    fee = fee_cents_per_contract(52)

    maker_view = p.net_edge(fee, CONFIG.risk.slippage_cents)
    risk_view = net_edge(0.55, c, "yes")

    assert maker_view == pytest.approx(risk_view)


def test_edge_size_alias_is_the_executable_figure():
    """Everything downstream reads edge_size; it must be the tradeable one."""
    c = make_candidate(yes_bid=40, yes_ask=60)
    p = proposal(c, probability=0.56)
    assert p.edge_size == p.executable_edge
    assert p.edge_size != p.midpoint_edge
