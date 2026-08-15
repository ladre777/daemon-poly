"""
Fractional Kelly position sizing.

Ported from a survey of open-source Kalshi bots, all of which size by edge
magnitude and all of which use a *fraction* of Kelly rather than the full
criterion:

- ``ryanfrigo/kalshi-ai-trading-bot`` — ``kelly_fraction = 0.25``, with the
  blunt justification that "three-quarter Kelly compounds losses
  catastrophically on a 45% win-rate strategy".
- ``OctagonAI/kalshi-trading-bot-cli`` — half-Kelly (0.5) default.
- ``brandononchain/kalshibot`` — ``min(f* * 0.25, 0.25)``, quarter-Kelly with
  a hard 25%-of-balance ceiling on top.
- ``suislanchez/polymarket-kalshi-weather-bot`` — ``kelly * 0.15 * bankroll``.

What this repo had instead: sizing purely by *headroom*, i.e. take the
smallest of the concentration caps and buy as many contracts as fit. That
treats a 4pp edge and a 40pp edge identically — both get the whole available
budget — which is precisely backwards. Kelly makes size a function of how
good the bet actually is.

The critical design decision, and the reason this is safe to switch on: Kelly
enters the sizing calculation as **one more cap among the existing gates**,
never as a replacement for them and never as a floor. It can only make a
position smaller than the concentration caps would have allowed. If Kelly says
"bet 40% of bankroll" and MAX_POSITION_PCT says 5%, the answer is still 5%.
No existing risk control is weakened by turning this on.

Fees are inside the arithmetic rather than applied afterwards: the cost basis
passed in is the fee-inclusive one, so the odds Kelly optimises against are
the odds actually available after Kalshi takes its cut. Sizing off pre-fee
odds would systematically oversize, which is the exact failure mode fractional
Kelly exists to avoid.
"""
from __future__ import annotations

from core.pricing import CONTRACT_PAYOUT_CENTS


def kelly_fraction(win_probability: float, cost_per_contract_cents: float) -> float:
    """Full-Kelly fraction of bankroll for one binary contract, after fees.

    A Kalshi contract bought for ``c`` cents pays ``100`` if it settles in our
    favour and ``0`` otherwise, so the net odds are::

        b = (100 - c) / c

    and the Kelly optimum is::

        f* = p - (1 - p) / b

    ``c`` is the *fee-inclusive* cost, so ``b`` is the real payoff on money
    actually committed. Using the raw contract price would overstate the odds
    by the fee and oversize every position.

    Returns 0.0 for a non-positive edge — Kelly's answer to a bad bet is to
    not take it, and a negative fraction has no meaning as a position size.
    """
    if not 0.0 < win_probability < 1.0:
        return 0.0
    if cost_per_contract_cents <= 0:
        return 0.0
    if cost_per_contract_cents >= CONTRACT_PAYOUT_CENTS:
        # Paying 100c or more for something that pays at most 100c cannot be
        # a positive-expectancy bet at any probability.
        return 0.0

    net_odds = (CONTRACT_PAYOUT_CENTS - cost_per_contract_cents) / cost_per_contract_cents
    fraction = win_probability - (1.0 - win_probability) / net_odds
    return max(fraction, 0.0)


def kelly_cap_cents(
    win_probability: float,
    cost_per_contract_cents: float,
    bankroll_cents: float,
    fraction_of_kelly: float,
) -> float:
    """Fractional-Kelly budget for this trade, in cents.

    Fed into the risk guardrail as one more gate, so it competes with the
    concentration caps and the smallest wins. It never raises a limit.
    """
    if bankroll_cents <= 0 or fraction_of_kelly <= 0:
        return 0.0
    full = kelly_fraction(win_probability, cost_per_contract_cents)
    return full * fraction_of_kelly * bankroll_cents


def win_probability_for(model_probability: float, direction: str) -> float:
    """Probability that the contract we are *buying* settles at 100.

    The model always states P(YES). Buying NO wins when YES does not, so the
    two directions need opposite readings of the same number. Getting this
    backwards would size NO trades by the probability of the thing that makes
    them worthless.
    """
    return model_probability if direction == "yes" else 1.0 - model_probability
