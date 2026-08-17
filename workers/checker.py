"""
Checker: the expensive, high-trust second opinion. Maker proposes cheaply and
in volume; Checker is the gate that has to independently agree before capital
moves — same division of labor as DÆMON-POLY's Claude Sonnet checker role.
Checker gets the Maker's reasoning but is explicitly told to critique it
rather than rubber-stamp it, since a Checker that just agrees with Maker
provides no actual risk reduction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

from config import CONFIG
from core.llm_client import first_text_block
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
agree because the reasoning sounds plausible. Respond ONLY with JSON: \
{"verdict": "approve"|"reject"|"abstain", "confidence": 0.0-1.0, \
"reasoning": "2-4 sentences explaining your independent view"}

Keep `reasoning` to at most 4 sentences and emit nothing outside the JSON \
object. The response is parsed by a machine and a verdict that runs past the \
token budget is discarded entirely, so a complete short answer is worth far \
more than a thorough one that gets cut off."""


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
    def __init__(self):
        self._client = anthropic.Anthropic(api_key=CONFIG.models.anthropic_api_key)

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
            f"Volume: {c.volume}"
        )
        # max_tokens caps thinking AND response text together, and
        # claude-sonnet-5 runs adaptive thinking whenever `thinking` is
        # omitted. Raising the number alone therefore does not fix
        # truncation — deliberation simply expands into the larger budget
        # and the JSON is still cut off. Both levers are needed: `effort`
        # bounds how much of the budget thinking may take, and max_tokens
        # gives what remains enough room for the verdict.
        #
        # `low` is deliberate. This is a small, well-scoped judgement with a
        # fixed output shape, which is exactly the shape `low` is for; the
        # Checker's value is an independent opinion, not a long deliberation.
        resp = self._client.messages.create(
            model=CONFIG.models.checker_model,
            max_tokens=CONFIG.models.checker_max_tokens,
            output_config={"effort": CONFIG.models.checker_effort},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )

        # Truncation is a distinct failure from bad output, and conflating
        # them is what made this bug recur. A verdict cut off mid-JSON was
        # reported as "unparseable JSON" — which reads as "the model
        # answered badly" and sent three separate investigations after the
        # prompt. The API says plainly when it ran out of room; ask it.
        if getattr(resp, "stop_reason", None) == "max_tokens":
            log.error(
                "Checker hit the %d-token cap on %s and was cut off mid-answer. "
                "This is a BUDGET failure, not a bad verdict — raise "
                "CHECKER_MAX_TOKENS or lower CHECKER_EFFORT. Abstaining.",
                CONFIG.models.checker_max_tokens, c.ticker,
            )
            return Verdict(
                proposal=proposal,
                verdict="abstain",
                confidence=0.0,
                reasoning=(
                    f"truncated: response hit the "
                    f"{CONFIG.models.checker_max_tokens}-token cap"
                ),
            )
        # Not content[0]: a thinking block sits at position 0 whenever the
        # model reasons, and reaching for .text on it raises. See
        # core/llm_client.first_text_block.
        raw = first_text_block(resp)
        # Strictly validated: an unparseable response, an unknown verdict
        # string, or a non-finite/out-of-range confidence all become an
        # abstention rather than an exception mid-pass or a value that
        # accidentally clears the confidence threshold. The previous version
        # indexed straight into the parsed dict and called float() on whatever
        # was there.
        checked = validate_checker_output(raw, ticker=c.ticker)
        if checked.reasoning == "parse_error":
            # Production showed two of these with stop_reason NOT max_tokens,
            # so the budget check above did not fire and the cause is still
            # open. Recording the stop_reason next to the failure is what
            # closes that gap: it separates "the model stopped early for some
            # other reason" from "the model finished and emitted non-JSON",
            # which is the distinction the 200-char log used to destroy.
            log.warning(
                "Checker parse failure on %s with stop_reason=%s, %d content "
                "block(s) — NOT a token-cap truncation (that path abstains "
                "earlier). See the payload logged above.",
                c.ticker, getattr(resp, "stop_reason", "<absent>"),
                len(getattr(resp, "content", []) or []),
            )
        return Verdict(
            proposal=proposal,
            verdict=checked.verdict,
            confidence=checked.confidence,
            reasoning=checked.reasoning,
        )
