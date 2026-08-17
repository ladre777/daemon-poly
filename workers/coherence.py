"""
Coherence gates on model output, before it becomes a trade.

Why this exists
---------------
A single production pass, ten WTI strikes on one contract:

    strike   model   market
    83.49    48.0%   22.5%
    84.49    45.0%   10.5%
    84.99    32.0%    8.5%      <-- 32% for "above 84.99"
    86.49    45.0%    3.5%      <-- 45% for "above 86.49"
    87.99    45.0%    1.5%

``P(X > 84.99) = 32%`` and ``P(X > 86.49) = 45%`` cannot both be true: a
higher strike cannot be more likely. Sixteen of forty-five strike pairs in
that pass violated it, some by 13 percentage points. The market's own prices
fell cleanly from 22.5% to 1.5%; the model's sat at 32-52% with no
relationship to the strike at all.

So the model was not pricing those markets. It was emitting roughly "45%" and
the entire apparent edge — averaging 35 percentage points — was an artifact
of it ignoring the strike. Every one was caught by the Checker, which means a
second LLM's judgement was the only thing between this bot and systematically
buying deep out-of-the-money contracts at twenty to thirty times fair value.

That is too thin a margin to rely on, and it is checkable arithmetically. Two
gates here, both of which can only ever *refuse* a trade:

1. **Monotonicity across strikes.** Model output on the same event must order
   correctly with the strike. This is a hard mathematical constraint, not a
   judgement, so a violation is proof of model error rather than evidence of
   edge.
2. **Implausibility in log-odds.** Measured as distance in log-odds rather
   than percentage points, deliberately. A flat "reject edges over 30pp" cap
   would also kill the weather thesis, where the model saying 15% against a
   market at 50% is a real and defensible disagreement. In log-odds that
   weather case is 1.73 apart, while claiming 45% against a market at 1.5% is
   3.99 apart — the difference between disagreeing with a market and
   asserting it is wrong by a factor of fifty.

Neither gate can approve anything. Both only remove proposals that would
otherwise have been traded.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from config import CONFIG

log = logging.getLogger("daemon_kalshi.coherence")

#: Strike types whose probability must fall as the strike rises, and those
#: whose probability must rise. "between" is excluded: two-sided strikes have
#: no single ordering against one another.
_DECREASING = ("greater", "greater_or_equal")
_INCREASING = ("less", "less_or_equal")


def _logit(p: float) -> float:
    """Log-odds, clamped away from the asymptotes.

    Kalshi prices bottom out at 1c, so a market at 0.015 is a real quote
    rather than a degenerate one; the clamp only stops 0 and 1 producing
    infinities.
    """
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def log_odds_distance(model_probability: float, market_probability: float) -> float:
    """How far apart two probabilities are, measured where it matters.

    Percentage points are the wrong unit near the tails. 45% against 1.5% and
    45% against 30% are both "a big edge" in points, but the first asserts the
    market is wrong by a factor of fifty and the second by a factor of two.
    """
    return abs(_logit(model_probability) - _logit(market_probability))


@dataclass
class _EventGroup:
    """Proposals seen so far on one event, keyed by strike."""

    direction: str
    by_strike: dict[float, float] = field(default_factory=dict)
    tainted: bool = False


@dataclass
class CoherenceReport:
    ok: bool
    reason: str = ""


class CoherenceGate:
    """Per-pass memory of what the model has already claimed.

    Checks are incremental rather than batched: candidates arrive one at a
    time and a proposal is acted on before the next is made, so there is no
    point at which a whole event is in hand. Each new proposal is tested
    against the ones already seen on its event, and an event that has
    contradicted itself is refused for the rest of the pass — once the model
    has demonstrated it is not reading the strike, its other answers on the
    same ladder are not trustworthy either.
    """

    def __init__(self):
        self._events: dict[str, _EventGroup] = {}
        self.rejected_monotonicity = 0
        self.rejected_implausible = 0

    def begin_pass(self) -> None:
        self._events.clear()

    # -- gate 1: strike monotonicity ---------------------------------------

    def _check_monotonic(self, proposal) -> CoherenceReport:
        c = proposal.candidate
        strike_type = (c.strike_type or "").lower()
        if strike_type in _DECREASING:
            direction = "decreasing"
        elif strike_type in _INCREASING:
            direction = "increasing"
        else:
            return CoherenceReport(True)          # "between" or unknown
        if c.floor_strike is None:
            return CoherenceReport(True)
        event = c.event_ticker or c.ticker
        key = f"{event}|{direction}"

        group = self._events.get(key)
        if group is None:
            group = self._events[key] = _EventGroup(direction=direction)

        if group.tainted:
            return CoherenceReport(
                False,
                f"event {event} already produced contradictory probabilities "
                f"this pass — the model is not reading the strike",
            )

        p = proposal.maker_probability
        tol = CONFIG.risk.coherence_tolerance
        for strike, seen in group.by_strike.items():
            if strike == c.floor_strike:
                continue
            lower, higher = (
                (seen, p) if strike < c.floor_strike else (p, seen)
            )
            lower_k, higher_k = (
                (strike, c.floor_strike) if strike < c.floor_strike
                else (c.floor_strike, strike)
            )
            violated = (
                lower < higher - tol if direction == "decreasing"
                else lower > higher + tol
            )
            if violated:
                group.tainted = True
                return CoherenceReport(
                    False,
                    f"incoherent across strikes on {event}: "
                    f"P(>{lower_k:g})={lower:.0%} vs P(>{higher_k:g})={higher:.0%} "
                    f"— a higher strike cannot be more likely",
                )

        group.by_strike[c.floor_strike] = p
        return CoherenceReport(True)

    # -- gate 2: implausible disagreement ----------------------------------

    def _check_plausible(self, proposal) -> CoherenceReport:
        limit = CONFIG.risk.max_log_odds_disagreement
        if limit <= 0:
            return CoherenceReport(True)
        market = proposal.candidate.implied_yes_probability
        distance = log_odds_distance(proposal.maker_probability, market)
        if distance <= limit:
            return CoherenceReport(True)
        return CoherenceReport(
            False,
            f"model says {proposal.maker_probability:.0%} against a market at "
            f"{market:.1%} — {distance:.2f} in log-odds, over the {limit:.2f} "
            f"limit. A disagreement this large is more likely a model error "
            f"than an edge",
        )

    # -- public ------------------------------------------------------------

    def check(self, proposal) -> CoherenceReport:
        """Refuse a proposal the model cannot possibly be right about."""
        if not CONFIG.risk.coherence_checks_enabled:
            return CoherenceReport(True)

        report = self._check_monotonic(proposal)
        if not report.ok:
            self.rejected_monotonicity += 1
            log.warning("Refusing %s: %s", proposal.candidate.ticker, report.reason)
            return report

        report = self._check_plausible(proposal)
        if not report.ok:
            self.rejected_implausible += 1
            log.warning("Refusing %s: %s", proposal.candidate.ticker, report.reason)
            return report

        return CoherenceReport(True)
