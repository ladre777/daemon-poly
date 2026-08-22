"""
Maker: LLM probability path.

Timeouts are transient (Moonshot latency). Missing-key is permanent for the
process. Do not conflate them — production showed 88 successful calls then
timeouts, then a permanent disable that muted golf/weather for the rest of
the process lifetime.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config import CONFIG
from core.llm_client import build_maker_llm, LLMUnavailable
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

# This fixed protocol and the accumulated playbook are intentionally kept ahead
# of market-specific facts. Kimi caches stable prompt prefixes; the dynamic
# evidence card below therefore changes without invalidating this guidance.
DECISION_PROTOCOL = """Decision protocol:
1. Price the settlement condition itself, not the displayed Kalshi price.
2. Treat source provenance and settlement-rule mismatches as uncertainty, not edge.
3. Do not invent missing facts, a wider error band, or an unverified event match.
4. Use the available evidence proportionally; low confidence is preferable to
   false precision.
5. Return the required JSON object only, with concise decision-relevant reasoning."""


def stable_system_prompt(playbook: str) -> str:
    """Build a cacheable, market-independent system prefix."""
    prompt = f"{SYSTEM_PROMPT}\n\n{DECISION_PROTOCOL}"
    if playbook:
        prompt += (
            "\n\nLessons from past trades (use as a prior, not gospel):\n"
            + clamp_text(playbook, CONFIG.risk.max_playbook_chars)
        )
    return prompt


def bounded_evidence_card(value: str, limit: int) -> str:
    """Bound live input without discarding either provenance or target facts.

    Source adapters place provenance and broad conditions first, while market-
    specific findings often land last. For uncommon oversized cards, retain
    both ends rather than applying a head-only cut that can remove the very
    player, city, or fixture being priced.
    """
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    marker = "[… middle of evidence card compacted …]"
    available = max(0, limit - len(marker) - 2)
    head_budget = available * 3 // 5
    tail_budget = available - head_budget
    head = text[:head_budget].rsplit("\n", 1)[0].rstrip()
    tail = text[-tail_budget:].split("\n", 1)[-1].lstrip()
    if not head or not tail:
        return clamp_text(text, limit)
    return f"{head}\n{marker}\n{tail}"


@dataclass
class Proposal:
    candidate: Candidate
    maker_probability: float
    maker_confidence: float
    reasoning: str
    source: str = "llm"

    @property
    def direction(self) -> str:
        return "yes" if self.maker_probability > self.candidate.implied_yes_probability else "no"

    @property
    def midpoint_edge(self) -> float:
        return abs(self.maker_probability - self.candidate.implied_yes_probability)

    @property
    def executable_edge(self) -> float:
        implied = self.candidate.executable_probability(self.direction)
        return (
            self.maker_probability - implied
            if self.direction == "yes"
            else implied - self.maker_probability
        )

    def net_edge(self, fee_cents_per_contract: float = 0.0,
                 slippage_cents: float = 0.0) -> float:
        return self.executable_edge - (fee_cents_per_contract + slippage_cents) / 100.0

    @property
    def edge_size(self) -> float:
        return self.executable_edge


def _is_missing_key_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "api key is empty" in msg
        or "no maker llm configured" in msg
        or ("moonshot_api_key" in msg and "empty" in msg)
    )


class Maker:
    def __init__(self, enricher=None, llm=None):
        self._llm = llm or build_maker_llm(CONFIG.models)
        self.enricher = enricher
        self._disabled = not self._llm.configured
        if self._disabled:
            log.error(
                "Maker LLM disabled: no usable provider. "
                "Set MOONSHOT_API_KEY. Quant path still runs."
            )
        else:
            log.info("Maker LLM: %s", self._llm.describe())

    @property
    def on_fallback(self) -> bool:
        return self._llm.on_fallback

    @property
    def available(self) -> bool:
        return not self._disabled

    def propose(self, candidate: Candidate) -> Optional[Proposal]:
        if self._disabled:
            return None

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
                # The evidence card contains only current, source-attributed
                # facts. Its bounded size protects latency without dropping
                # the stable decision rules or historical playbook.
                user_msg += (
                    "\n\nLIVE EVIDENCE CARD (current facts; verify market match):\n"
                    + bounded_evidence_card(extra, CONFIG.risk.max_context_chars)
                )

        playbook = Reflector.load_playbook()
        system_prompt = stable_system_prompt(playbook)

        try:
            content = self._llm.complete(system_prompt, user_msg, temperature=0.3)
        except LLMUnavailable as e:
            if _is_missing_key_error(e):
                self._disabled = True
                log.error("Maker LLM disabled after missing-key error: %s", e)
            raise

        checked = validate_maker_output(content, ticker=candidate.ticker)
        if checked is None:
            return None

        proposal = Proposal(
            candidate=candidate,
            maker_probability=checked.probability_yes,
            maker_confidence=checked.confidence,
            reasoning=checked.reasoning,
        )
        fee = fee_cents_per_contract(candidate.executable_price_cents(proposal.direction))
        net = proposal.net_edge(fee, CONFIG.risk.slippage_cents)
        if net < CONFIG.risk.min_edge_threshold:
            log.debug(
                "%s: net edge %.2f%% below %.2f%% threshold — no proposal",
                candidate.ticker, net * 100, CONFIG.risk.min_edge_threshold * 100,
            )
            return None
        return proposal
