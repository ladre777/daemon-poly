"""
QuantMaker: the fast path. No LLM call per market — a digital/binary option
probability model instead. This is what actually answers "how do I code it
to exploit 15-minute commodities markets": Maker's job for these isn't
reasoning, it's math, and math is milliseconds instead of seconds.

Model: given current spot price S, strike K, time to expiry T (in the same
units as your volatility estimate), and realized volatility sigma, this
prices a "will spot be above/below K at expiry" contract the same way a
digital option is priced — probability of finishing in the money under a
lognormal assumption, via the normal CDF of the standardized log-distance
to strike. This is a real, standard technique (the same shape of model
public write-ups describe using against Kalshi's own KXHIGH weather
contracts), not something novel or guaranteed — lognormal/constant-vol is a
simplifying assumption real markets violate, especially around news events,
and this only works for markets with a numeric strike and a live spot feed
(crypto, GLD/SLV commodities) — not for the reasoning-based categories
(politics, culture) where Maker (the LLM) is doing something the math can't.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from core.spot_price_client import SpotPriceClient
from workers.scout import Candidate
from workers.maker import Proposal
from config import CONFIG

log = logging.getLogger("daemon_kalshi.quant_maker")

# series_ticker prefix (or keyword fallback) -> spot symbol + which price
# source to use. Extend this as you confirm more of Kalshi's numeric series.
SERIES_SPOT_MAP = {
    "KXBTC": ("btc", "crypto"),
    "KXETH": ("eth", "crypto"),
    "KXSOL": ("sol", "crypto"),
    "KXGOLD": ("gold", "etf"),
    "KXSILVER": ("silver", "etf"),
}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass
class QuantProposal:
    candidate: Candidate
    probability_yes: float
    spot_price: float
    volatility_used: float
    seconds_to_expiry: float

    def to_maker_proposal(self, min_confidence: float = 0.6) -> Optional[Proposal]:
        """Adapts this into the same Proposal shape the LLM Maker produces,
        so Checker/RiskGuardrail/Execution don't need separate code paths."""
        edge = abs(self.probability_yes - self.candidate.implied_yes_probability)
        if edge < CONFIG.risk.min_edge_threshold:
            return None
        return Proposal(
            candidate=self.candidate,
            maker_probability=self.probability_yes,
            maker_confidence=min_confidence,  # quant path doesn't self-report confidence the way an LLM does
            reasoning=(
                f"Quant model: spot={self.spot_price:.2f}, strike={self.candidate.floor_strike}, "
                f"{self.seconds_to_expiry:.0f}s to expiry, vol={self.volatility_used:.5f} "
                f"(per-observation-interval stdev of log returns)"
            ),
            source="quant",
        )


class QuantMaker:
    def __init__(self, spot_client: SpotPriceClient = None):
        self.spot = spot_client or SpotPriceClient()

    def _resolve_symbol(self, candidate: Candidate) -> Optional[tuple[str, str]]:
        for prefix, (symbol, source) in SERIES_SPOT_MAP.items():
            if candidate.series_ticker.upper().startswith(prefix) or candidate.ticker.upper().startswith(prefix):
                return symbol, source
        return None

    def can_handle(self, candidate: Candidate) -> bool:
        if candidate.floor_strike is None or not candidate.strike_type:
            return False
        if self._resolve_symbol(candidate) is None:
            return False
        seconds = candidate.seconds_to_close
        return seconds is not None and 0 < seconds

    def propose(self, candidate: Candidate) -> Optional[QuantProposal]:
        resolved = self._resolve_symbol(candidate)
        if not resolved:
            return None
        symbol, source = resolved

        spot = self.spot.crypto_price(symbol) if source == "crypto" else self.spot.etf_price(symbol)
        if spot is None:
            log.warning("No spot price for %s (%s) — skipping", candidate.ticker, symbol)
            return None

        seconds_to_expiry = candidate.seconds_to_close
        if not seconds_to_expiry or seconds_to_expiry <= 0:
            return None

        history = self.spot.get_history(symbol)
        # Look back roughly 20x the time remaining, capped 1hr-24hr, so vol
        # estimate is scaled to the market's own horizon rather than a fixed
        # window that's wrong for both 15-min and daily contracts at once.
        lookback = max(3600, min(seconds_to_expiry * 20, 86400))
        vol = history.realized_vol(lookback) if history else None
        if vol is None or vol == 0:
            # Not enough price history yet to trust a vol estimate — this
            # improves automatically the longer the bot runs and keeps
            # polling. Until then, decline rather than guess a number that
            # would silently produce a false-confidence probability.
            log.info("Insufficient price history for %s yet — skipping quant proposal", symbol)
            return None

        strike = candidate.floor_strike
        # Scale vol (per-poll-interval stdev) to the sqrt of periods between
        # now and expiry, assuming polls roughly track SCOUT_POLL_SECONDS.
        periods = max(seconds_to_expiry / max(CONFIG.scout_poll_seconds, 1), 1)
        vol_to_expiry = vol * math.sqrt(periods)

        z = (math.log(strike / spot)) / vol_to_expiry if spot > 0 and strike > 0 else 0
        # P(spot_T > strike) under lognormal assumption
        prob_above = 1 - _norm_cdf(z)

        if candidate.strike_type == "greater":
            prob_yes = prob_above
        elif candidate.strike_type == "less":
            prob_yes = 1 - prob_above
        elif candidate.strike_type == "between" and candidate.cap_strike:
            z_cap = math.log(candidate.cap_strike / spot) / vol_to_expiry
            prob_yes = _norm_cdf(z_cap) - _norm_cdf(z)
        else:
            return None

        prob_yes = min(max(prob_yes, 0.001), 0.999)
        return QuantProposal(
            candidate=candidate,
            probability_yes=prob_yes,
            spot_price=spot,
            volatility_used=vol,
            seconds_to_expiry=seconds_to_expiry,
        )
