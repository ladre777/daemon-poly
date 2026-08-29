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
    """Conservative per-contract fee, priced as a single-contract order.

    Kalshi's taker fee is ``ceil(fee_rate * C * P * (1 - P))`` in dollars,
    rounded up once per ORDER. Callers here are gates and per-contract cost
    estimates that run before sizing, so the count is not yet known and this
    assumes the worst case, C=1, where the one-cent floor applies in full.
    For a larger order the real per-contract cost is lower, so this is an
    upper bound — the direction that refuses a marginal trade rather than
    putting on one that does not clear its own fees.

    Until 2026-08-29 this ceilinged to a *centicent* despite a docstring
    claiming the cent, returning 1.75c where Kalshi charges 2c and 0.34c on a
    5c contract where Kalshi charges 1c. It understated every fee by 12% to
    66%, always in the direction that makes an edge look bigger than it is.

    FEE_RATE=0.07 comes from published summaries, not a verified schedule —
    see docs/SAFETY.md.
    """
    return float(fee_cents_for_order(price_cents, 1))


def fee_cents_for_order(price_cents: float, count: int) -> int:
    """Total fee in whole cents for an order of ``count`` contracts.

    Kalshi's published formula rounds the whole order up to the next cent:
    ``ceil(fee_rate * C * P * (1 - P))`` in dollars. Two consequences that a
    per-contract rate cannot express, and that matter in opposite directions:

    * At C=1 there is a one-cent floor. A 10c contract owes 0.63c by the
      rate and is charged 1c — 37% more. ``fee_cents_per_contract`` ceilings
      to a *centicent*, so it returns 0.63c and understates every small
      order by up to 0.88c.
    * The rounding is per order, not per contract, so it amortises as C
      grows. Charging a whole cent per contract would overstate a 10-lot at
      10c by 43%.

    Both are wrong to ignore, which is why this takes the count instead of
    returning a rate. Used for counterfactual grading, where C is exactly 1
    and the one-cent floor is the whole story.
    """
    if count <= 0:
        return 0
    p = min(max(price_cents / CONTRACT_PAYOUT_CENTS, 0.0), 1.0)
    # Rounded before the ceiling for the same reason as fee_cents_per_contract:
    # p*(1-p) is symmetric about 50c, but binary dust would otherwise promote
    # one side of a mirrored pair into an extra cent.
    return math.ceil(round(CONFIG.risk.fee_rate * count * p * (1.0 - p) * 100.0, 6))


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


#: Order prices go to Kalshi as dollar-denominated strings on a per-market
#: grid. Two structures appear on live payloads: ``linear_cent`` (1c steps)
#: and ``tapered_deci_cent`` (0.001 steps below $0.10 and above $0.90, 0.01
#: between) — the crypto 15-minute and hourly families quote on the latter,
#: e.g. KXBTC15M resting 0.9590 / 0.9600, a tenth-of-a-cent spread.
#:
#: A whole cent is a member of *every* published grid, so flooring a limit to
#: the cent is a valid price on any market without having to read the
#: structure off the payload first. Sub-cent quoting is a market-making
#: concern (two-sided quotes inside a 0.1c spread) and belongs with that
#: layer, where the grid has to be read per market anyway.
LIMIT_PRICE_TICK_CENTS = 1.0


def grid_floor_cents(price_cents: float) -> float:
    """Floor a price onto the order grid.

    Applied to *buy* limits, so the effect is always to pay no more than the
    caller approved. The tolerance absorbs float dust — 6.000000001c is a 6c
    limit, not a 6c limit that floors to 5.
    """
    return math.floor(round(price_cents, 6) / LIMIT_PRICE_TICK_CENTS) * LIMIT_PRICE_TICK_CENTS


def price_dollars_string(price_cents: float) -> str:
    """Kalshi's dollar wire format, refusing anything it cannot express.

    This used to be ``f"{cents / 100:.2f}"`` at the call site, which rounds to
    the *nearest* cent. On a market quoting in tenths of a cent that rounds
    **up** half the time, so a 5.55c limit left the process as a 6c order —
    above the price the risk decision sized, budgeted and invariant-checked,
    and above the number persisted as the order's limit. Small in absolute
    terms and entirely silent, which is the part that matters: the approved
    budget stopped being the submitted budget.

    Raising rather than rounding keeps that failure impossible to reintroduce
    by accident. Callers are expected to have gone through
    ``cost_per_contract_cents``, which returns a gridded limit.
    """
    if price_cents != grid_floor_cents(price_cents):
        raise ValueError(
            f"limit price {price_cents}c is not on the {LIMIT_PRICE_TICK_CENTS}c "
            f"order grid — refusing to round it silently"
        )
    return f"{price_cents / 100:.2f}"


def cost_per_contract_cents(executable_price: float) -> tuple[float, float, float]:
    """(limit_price, fee, total cost) for one contract at this price.

    The limit price carries the slippage allowance, and the fee is computed
    on the limit price rather than the quote, so a fill at the worst price we
    are willing to pay is still inside the budget it was approved under.

    It is also floored onto the order grid here rather than at submission, so
    the number risk sizes against, the number stored on the order, and the
    number sent to Kalshi are the same number. Flooring can only ever move
    the limit down, so this never widens the budget.
    """
    limit_price = grid_floor_cents(
        min(executable_price + CONFIG.risk.slippage_cents, 99.0)
    )
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
