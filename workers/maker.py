"""
Maker: LLM probability path. If no LLM is configured, propose() returns None
immediately so the orchestrator can keep running quant without failure spam.
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


class Maker:
    def __init__(self, enricher=None, llm=None):
        self._llm = llm or build_maker_llm(CONFIG.models)
        self.enricher = enricher
        self._disabled = not self._llm.configured
        if self._disabled:
            log.error(
                "Maker LLM disabled: no usable provider. "
                "Set MOONSHOT_API_KEY (exact name) in Railway. "
                "Quant path will still run."
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
                user_msg += (
                    "\n\nLive grounding data. Each source states its own "
                    "provenance and how far it should be trusted — read that "
                    "before weighing it:\n"
                    + clamp_text(extra, CONFIG.risk.max_context_chars)
                )

        playbook = Reflector.load_playbook()
        if playbook:
            user_msg += (
                "\n\nLessons from past trades (use as a prior, not gospel):\n"
                + clamp_text(playbook, CONFIG.risk.max_playbook_chars)
            )

        try:
            content = self._llm.complete(SYSTEM_PROMPT, user_msg, temperature=0.3)
        except LLMUnavailable as e:
            # Treat hard misconfig as disable for this process so we stop
            # hammering and alerting every candidate.
            msg = str(e).lower()
            if "api key" in msg or "no maker llm" in msg or "no provider" in msg:
                self._disabled = True
                log.error("Maker LLM disabled after config error: %s", e)
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
