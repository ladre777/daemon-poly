"""
Maker: takes a Scout candidate, asks Kimi/Moonshot for a probability estimate
and reasoning, and turns that into a proposed edge if it clears the minimum
threshold. Same role Moonshot plays in DÆMON-POLY — fast, cheap, high-volume
first pass; Checker is the expensive second opinion that gates real capital.

The prompt below is a starting scaffold, not your tuned DÆMON-POLY prompt —
port your refined golf/sports prompt language in here once you have it handy.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config import CONFIG
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
    def edge_size(self) -> float:
        return abs(self.maker_probability - self.candidate.implied_yes_probability)

    @property
    def direction(self) -> str:
        return "yes" if self.maker_probability > self.candidate.implied_yes_probability else "no"


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
                user_msg += f"\n\nLive ESPN context (may or may not be the exact event — verify it matches before trusting it):\n{extra}"

        playbook = Reflector.load_playbook()
        if playbook:
            user_msg += f"\n\nLessons from past trades (use as a prior, not gospel):\n{playbook}"
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
        content = resp.json()["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            log.warning("Maker returned non-JSON for %s: %s", candidate.ticker, content[:200])
            return None

        proposal = Proposal(
            candidate=candidate,
            maker_probability=float(parsed["probability_yes"]),
            maker_confidence=float(parsed["confidence"]),
            reasoning=parsed["reasoning"],
        )
        if proposal.edge_size < CONFIG.risk.min_edge_threshold:
            return None
        return proposal
