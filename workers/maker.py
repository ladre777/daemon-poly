"""
Maker: takes a Scout candidate, asks Kimi/Moonshot for a probability estimate
and reasoning, and turns that into a proposed edge if it clears the minimum
threshold. Same role Moonshot plays in DÆMON-POLY — fast, cheap, high-volume
first pass; Checker is the expensive second opinion that gates real capital.

The prompt below is a starting scaffold, not your tuned DÆMON-POLY prompt —
port your refined golf/sports prompt language in here once you have it handy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config import CONFIG
from core.pricing import fee_cents_per_contract
from core.validation import clamp_text, validate_maker_output
from workers.scout import Candidate
from workers.reflect import Reflector

log = logging.getLogger("daemon_kalshi.maker")

SYSTEM_PROMPT = """You are the Maker in an autonomous prediction-market trading \
system. Given a market on Kalshi, estimate the true probability of YES \
resolving, independent of the current market price. Ground your estimate in \
whatever real information you have about the event — do not anchor on the \
market price itself, since the whole point is finding cases where the market \
is wrong. If live ESPN context is provided, check first whether it actually \
matches this market's event before relying on it — the matching is a \
heuristic and can be wrong when multiple tournaments/games are live at once. \
Respond ONLY with JSON: \
{"probability_yes": 0.0-1.0, "confidence": 0.0-1.0, "reasoning": "2-4 sentences"}"""


@dataclass
class Proposal:
    candidate: Candidate
    maker_probability: float
    maker_confidence: float
    reasoning: str
    source: str = "llm"   # "llm" | "quant" — lets edge memory separate calibration by strategy type

    @property
    def direction(self) -> str:
        """Which side to buy.

        Decided against the midpoint, because direction is a question about
        which side of the market the model disagrees with, and the midpoint is
        the neutral reference for that. Whether the disagreement is *tradeable*
        is a separate question, answered by executable_edge below.
        """
        return "yes" if self.maker_probability > self.candidate.implied_yes_probability else "no"

    @property
    def midpoint_edge(self) -> float:
        """Edge against the midpoint. Reporting only.

        This is what the old code approved on, and it is systematically
        optimistic by half the spread: on a 48/52 market it credits 2c of edge
        that no one can trade at. On a wide market that difference is the
        entire signal.
        """
        return abs(self.maker_probability - self.candidate.implied_yes_probability)

    @property
    def executable_edge(self) -> float:
        """Edge against the price we would actually pay, before fees.

        Buying YES at the ask costs ask/100 for something worth 1 if YES, so
        the edge is ``p - ask/100``. Buying NO at ``(100-bid)/100``, the edge
        is ``bid/100 - p``. Negative means the "edge" disappears once you
        cross the spread, which is the case the midpoint hides.
        """
        implied = self.candidate.executable_probability(self.direction)
        return (
            self.maker_probability - implied
            if self.direction == "yes"
            else implied - self.maker_probability
        )

    def net_edge(self, fee_cents_per_contract: float = 0.0,
                 slippage_cents: float = 0.0) -> float:
        """Executable edge net of fees and a slippage allowance.

        This is the number that decides whether a trade is worth making. A
        3pp edge on a 50c contract is roughly $0.03/contract of expected
        value against ~1.75c of fees plus slippage — thin enough that leaving
        costs out of the comparison flips the sign of the decision.
        """
        return self.executable_edge - (fee_cents_per_contract + slippage_cents) / 100.0

    @property
    def edge_size(self) -> float:
        """Backwards-compatible alias, now the executable figure.

        Everything downstream that reads ``edge_size`` (risk thresholds,
        longshot guard, edge memory) gets the tradeable number rather than
        the midpoint one.
        """
        return self.executable_edge


class Maker:
    def __init__(self, enricher=None):
        self._http = httpx.Client(
            base_url=CONFIG.models.moonshot_base_url,
            headers={"Authorization": f"Bearer {CONFIG.models.moonshot_api_key}"},
            timeout=30.0,
        )
        self.enricher = enricher

    def propose(self, candidate: Candidate) -> Optional[Proposal]:
        user_msg = (
            f"Market: {candidate.title}\n"
            f"Ticker: {candidate.ticker}\n"
            f"Category: {candidate.category}\n"
            f"Current YES bid/ask: {candidate.yes_bid}/{candidate.yes_ask} "
            f"(implied probability ~{candidate.implied_yes_probability:.2%})\n"
            f"Volume: {candidate.volume}\n"
            f"Closes: {candidate.close_time}"
        )
        if self.enricher:
            extra = self.enricher.enrich(candidate)
            if extra:
                user_msg += (
                    "\n\nLive ESPN context (may or may not be the exact event — "
                    "verify it matches before trusting it):\n"
                    + clamp_text(extra, CONFIG.risk.max_context_chars)
                )

        playbook = Reflector.load_playbook()
        if playbook:
            user_msg += (
                "\n\nLessons from past trades (use as a prior, not gospel):\n"
                + clamp_text(playbook, CONFIG.risk.max_playbook_chars)
            )
        resp = self._http.post(
            "/chat/completions",
            json={
                "model": CONFIG.models.moonshot_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            log.warning("Maker response had no message content for %s", candidate.ticker)
            return None

        # Strictly validated: a non-finite or out-of-range probability, a
        # missing field, or prose instead of JSON all mean "no proposal", not
        # an exception and not a coerced value. A NaN probability in
        # particular used to flow straight through, and every comparison
        # against NaN is False, so it failed thresholds silently.
        checked = validate_maker_output(content, ticker=candidate.ticker)
        if checked is None:
            return None

        proposal = Proposal(
            candidate=candidate,
            maker_probability=checked.probability_yes,
            maker_confidence=checked.confidence,
            reasoning=checked.reasoning,
        )
        # Threshold applies to the EXECUTABLE edge net of fees and slippage,
        # not the midpoint edge. On a 48/52 market the midpoint flatters the
        # trade by 2c before costs; a 4pp "edge" measured that way can be
        # negative once you actually cross the spread and pay the fee.
        fee = fee_cents_per_contract(candidate.executable_price_cents(proposal.direction))
        net = proposal.net_edge(fee, CONFIG.risk.slippage_cents)
        if net < CONFIG.risk.min_edge_threshold:
            log.debug(
                "%s: net edge %.2f%% below %.2f%% threshold (midpoint edge would "
                "have been %.2f%%) — no proposal",
                candidate.ticker, net * 100, CONFIG.risk.min_edge_threshold * 100,
                proposal.midpoint_edge * 100,
            )
            return None
        return proposal
