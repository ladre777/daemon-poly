"""
Checker: the expensive, high-trust second opinion. Maker proposes cheaply and
in volume; Checker is the gate that has to independently agree before capital
moves — same division of labor as DÆMON-POLY's Claude Sonnet checker role.
Checker gets the Maker's reasoning but is explicitly told to critique it
rather than rubber-stamp it, since a Checker that just agrees with Maker
provides no actual risk reduction.

As of 2026-08-21 the Checker is provider-configurable. Default is Moonshot/Kimi
(cheap / effectively free relative to Claude) so the high-volume gate no longer
burns Anthropic tokens. The same strict JSON contract and validation remain.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from config import CONFIG
from core.llm_client import LLMTruncated, build_checker_llm
from core.validation import validate_checker_output
from workers.maker import Proposal

log = logging.getLogger("daemon_kalshi.checker")

SYSTEM_PROMPT = """You are the Checker in an autonomous prediction-market \
trading system. Another model (the Maker) has proposed a probability estimate \
for a Kalshi market and identified an apparent edge against the current \
market price. Your job is to independently evaluate whether that edge is \
real — actively look for reasons the Maker's estimate could be wrong: stale \
information, overconfidence, a market that's actually well-calibrated for \
reasons the Maker missed, or reasoning that doesn't hold up. Do not simply \
agree because the reasoning sounds plausible.

Respond ONLY with a single JSON object and nothing else:
{"verdict": "approve"|"reject"|"abstain", "confidence": 0.0-1.0, \
"reasoning": "2-4 sentences explaining your independent view"}

Rules:
- verdict must be exactly one of: approve, reject, abstain
- confidence must be a number between 0 and 1
- Keep reasoning to at most 4 sentences
- Emit nothing outside the JSON object
- If you are uncertain, prefer reject or abstain over approve
"""


@dataclass
class Verdict:
    proposal: Proposal
    verdict: str
    confidence: float
    reasoning: str

    @property
    def approved(self) -> bool:
        return (
            self.verdict == "approve"
            and self.confidence >= CONFIG.risk.checker_min_confidence
        )


class Checker:
    def __init__(self, llm=None):
        self._llm = llm or build_checker_llm(CONFIG.models)
        # max_tokens on the boot line, not just the provider pair. It is the
        # lever a truncated verdict is diagnosed from, and CHECKER_MAX_TOKENS
        # was tuned for claude-sonnet-5 — the Checker now runs Gemini or
        # Haiku, so the value in force is worth stating where it is read.
        log.info(
            "Checker LLM: %s max_tokens=%d",
            self._llm.describe(), CONFIG.models.checker_max_tokens,
        )

    def check(self, proposal: Proposal) -> Verdict:
        c = proposal.candidate
        # Today's date, stated plainly and first.
        #
        # Without it the Checker reasons from its training cutoff, and that
        # produced a false rejection on the best-grounded market in the
        # system. On KXHIGHNY-26AUG17-T84 it wrote: "NWS forecasts don't
        # extend 2+ years out, so the Maker's claimed 'official forecast' for
        # Aug 2026 is almost certainly a hallucination". The forecast was
        # real, pulled that morning from the same NWS station Kalshi settles
        # against. The Checker simply did not know what year it was, so a
        # correctly dated market looked like a fabrication — and the markets
        # this hits hardest are the weather ones, where our grounding is
        # strongest and the case for a real edge is best.
        #
        # This is not a loosening. Every threshold the gate had, it keeps. It
        # is being told a fact it was previously guessing at, and guessing
        # wrong in the direction of refusing good trades.
        now = datetime.now(timezone.utc)
        user_msg = (
            f"Today's date is {now:%Y-%m-%d} (UTC). This is current and "
            f"authoritative: market and forecast dates near it are real, not "
            f"errors, even if they fall after your training data ends.\n"
            f"Market: {c.title} ({c.ticker})\n"
            f"Market closes: {c.close_time}\n"
            f"Market implied probability: {c.implied_yes_probability:.2%} "
            f"(bid/ask {c.yes_bid}/{c.yes_ask})\n"
            f"Maker's estimate: {proposal.maker_probability:.2%} "
            f"(confidence {proposal.maker_confidence:.2%})\n"
            f"Maker's reasoning: {proposal.reasoning}\n"
            f"Implied edge: {proposal.edge_size:.2%} toward {proposal.direction.upper()}\n"
            f"Volume: {c.volume}\n"
            f"\nRespond with ONLY the JSON object."
        )

        try:
            raw = self._llm.complete(SYSTEM_PROMPT, user_msg, temperature=0.2)
        except LLMTruncated as e:
            # Caught before the generic handler and reported as its own
            # thing. A truncated verdict is also unparseable JSON, so if this
            # fell through to validate_checker_output it would come back as
            # "parse_error" — which reads as "the model answered badly" and
            # sends the next reader after the prompt. It is a budget failure,
            # and the levers are named here so nobody has to rediscover them.
            #
            # CHECKER_MAX_TOKENS was tuned for claude-sonnet-5. The Checker
            # now runs Gemini or Haiku, whose output behaviour differs, so
            # this is a live risk rather than a historical one.
            log.error(
                "Checker TRUNCATED on %s: %s Raise CHECKER_MAX_TOKENS "
                "(currently %d) or shorten the prompt. Abstaining.",
                c.ticker, e, CONFIG.models.checker_max_tokens,
            )
            return Verdict(
                proposal=proposal,
                verdict="abstain",
                confidence=0.0,
                reasoning=(
                    f"truncated: {e.provider} hit the {e.max_tokens}-token "
                    f"cap — budget failure, not a bad verdict"
                ),
            )
        except Exception as e:
            log.error("Checker LLM failed on %s: %s — abstaining", c.ticker, e)
            return Verdict(
                proposal=proposal,
                verdict="abstain",
                confidence=0.0,
                reasoning=f"llm_error: {type(e).__name__}: {e}",
            )

        checked = validate_checker_output(raw, ticker=c.ticker)
        if checked.reasoning == "parse_error":
            log.warning(
                "Checker parse failure on %s — see payload logged by validation",
                c.ticker,
            )
        return Verdict(
            proposal=proposal,
            verdict=checked.verdict,
            confidence=checked.confidence,
            reasoning=checked.reasoning,
        )
