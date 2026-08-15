"""
Shared price and cost arithmetic.

Lives in core/ rather than in risk_guardrail because Maker needs the same
numbers to decide whether a proposal is worth making at all, and
``workers.maker`` cannot import ``workers.risk_guardrail`` — risk imports
checker, which imports maker. One definition, imported by both, also means
the threshold Maker screens on and the one risk enforces cannot drift apart.
"""
from __future__ import annotations

import math

from config import CONFIG

#: A settled binary contract pays this per contract.
CONTRACT_PAYOUT_CENTS = 100.0


def fee_cents_per_contract(price_cents: float) -> float:
    """Conservative per-contract fee estimate, rounded up to the cent.

    Kalshi's taker fee is approximately ``fee_rate * P * (1 - P)`` per
    contract with ``P`` the price in dollars, peaking near 50c. Rounded up
    because underestimating a cost that is subtracted from edge is the
    direction that puts on trades which do not clear their own fees.

    FEE_RATE=0.07 comes from published summaries, not a verified schedule —
    see docs/SAFETY.md.
    """
    p = min(max(price_cents / CONTRACT_PAYOUT_CENTS, 0.0), 1.0)
    fee_cents = CONFIG.risk.fee_rate * p * (1.0 - p) * 100.0
    # Round before the ceiling. p*(1-p) is symmetric about 50c, but 0.7 has no
    # exact binary representation, so 0.7*(1-0.7) lands a few ulps above
    # 0.3*(1-0.3) — and ceil() promotes that dust into a whole extra cent.
    # Without this, a 70c contract is charged 1.48c and a 30c contract 1.47c
    # for what is the same trade mirrored, which breaks the symmetry the fee
    # policy is supposed to have between YES and NO.
    return math.ceil(round(fee_cents * 100.0, 6)) / 100.0


def executable_price_cents(candidate, direction: str) -> float:
    """The price we would actually pay, not the midpoint.

    Buying YES lifts the ask; buying NO lifts the NO ask, which is
    ``100 - yes_bid``.
    """
    return (
        candidate.yes_ask
        if direction == "yes"
        else (CONTRACT_PAYOUT_CENTS - candidate.yes_bid)
    )


def executable_probability(candidate, direction: str) -> float:
    """Break-even probability implied by the executable price."""
    price = executable_price_cents(candidate, direction)
    return price / 100.0 if direction == "yes" else 1.0 - (price / 100.0)


def cost_per_contract_cents(executable_price: float) -> tuple[float, float, float]:
    """(limit_price, fee, total cost) for one contract at this price.

    The limit price carries the slippage allowance, and the fee is computed
    on the limit price rather than the quote, so a fill at the worst price we
    are willing to pay is still inside the budget it was approved under.
    """
    limit_price = min(executable_price + CONFIG.risk.slippage_cents, 99.0)
    fee = fee_cents_per_contract(limit_price)
    return limit_price, fee, limit_price + fee


def net_edge(
    model_probability: float,
    candidate,
    direction: str,
    include_costs: bool = True,
) -> float:
    """Expected value per contract, as a probability, after crossing the spread.

    This is the quantity that decides whether to trade. The midpoint edge the
    old code used is systematically optimistic by half the spread plus the
    whole fee.
    """
    implied = executable_probability(candidate, direction)
    edge = (
        model_probability - implied
        if direction == "yes"
        else implied - model_probability
    )
    if not include_costs:
        return edge
    price = executable_price_cents(candidate, direction)
    _, fee, _ = cost_per_contract_cents(price)
    return edge - (fee + CONFIG.risk.slippage_cents) / 100.0
