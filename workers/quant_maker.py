"""
QuantMaker: the fast path. No LLM call per market — a digital/binary option
probability model instead.

Model: given spot S, strike K, time to expiry T and volatility sigma, this
prices "will spot be above/below K at expiry" the way a digital option is
priced: the probability of finishing in the money under a lognormal
assumption, via the normal CDF of the standardised log-distance to strike.
Standard technique, not a novel one, and lognormal/constant-vol is a
simplifying assumption real markets violate — especially around news.

What P1 item 8 changed
----------------------
The model arithmetic is unchanged. What changed is everything around it,
because the inputs were the weak part:

- **Contract semantics are now explicit.** The old five-line SERIES_SPOT_MAP
  asserted "CoinGecko BTC settles this contract" with nothing behind it. See
  core/contract_specs.py: each family records its strike units, feed units,
  settlement definition, timezone and observation window, plus whether any of
  that has been verified. None has, so the quant path declines by default.
  The gold/silver entries carry an explicit unit mismatch (per-ounce strike
  vs per-share feed) that would have priced every contract at ~0 or ~1
  without raising anything.
- **Volatility units are honest.** ``realized_vol`` now returns a per-second
  figure computed from the real elapsed time between observations. The old
  version returned a per-observation-interval stdev and the caller scaled it
  by ``seconds_to_expiry / SCOUT_POLL_SECONDS``, assuming observations were
  evenly spaced at exactly the poll interval — which they never were, and
  which duplicated observations made worse in the direction of understating
  volatility. Understated vol means overconfident probabilities.
- **Stale and thin data are refused**, rather than being priced off.

2026-08-21: volatility spike refusal
------------------------------------
Trailing realized vol is still not forward vol. After a sharp move the
estimator spikes while implied does not, manufacturing phantom edge on
out-of-the-money strikes. When short-window vol is sharply elevated versus a
longer baseline, this path now declines rather than trading the spike.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from config import CONFIG
from core.contract_specs import ContractSpec, spec_for, usable
from core.spot_price_client import SpotPriceClient
from workers.scout import Candidate, family_of
from workers.maker import Proposal

log = logging.getLogger("daemon_kalshi.quant_maker")


#: Length of the settlement averaging window, in seconds. Kalshi's crypto
#: contracts settle on "the simple average of the sixty seconds" of the
#: relevant CF Benchmarks index — confirmed verbatim from rules_primary on
#: three live markets, see core/contract_specs.py.
SETTLEMENT_AVERAGING_SECONDS = 60.0


def _diffusion_horizon_seconds(spec, seconds_to_expiry: float) -> float:
    """Effective diffusion horizon for the quantity that actually settles.

    A point-in-time contract settles on the index value at T, whose variance
    grows as sigma^2 * T. An index-settled Kalshi contract instead settles on
    the *mean* of the index over the final w seconds, and a time-average is
    less variable than the endpoint it straddles: for a driftless random walk
    the average over a trailing window of length w has variance

        sigma^2 * (T - w) + sigma^2 * w/3  =  sigma^2 * (T - 2w/3)

    so the correct horizon is ``T - 2w/3``, not ``T``. Treating the average
    as a point sample overstates the spread of outcomes, which pushes every
    probability toward 0.5 and manufactures edge against markets that are
    correctly priced.

    The effect is modest at a 15-minute horizon (900s -> 860s, ~2% off the
    standard deviation) and grows as expiry approaches — which is exactly
    where the settlement blackout stops us pricing anyway. It is applied
    because it is the right quantity, not because it is large.

    Floored at one second: a non-positive horizon would make the diffusion
    term zero and the probability a step function.
    """
    if spec is None or spec.observation != "rti_60s_average":
        return seconds_to_expiry
    horizon = seconds_to_expiry - (2.0 * SETTLEMENT_AVERAGING_SECONDS / 3.0)
    return max(horizon, 1.0)


#: For expressing a per-second sigma in the units people can sanity-check.
_SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass
class QuantProposal:
    candidate: Candidate
    probability_yes: float
    spot_price: float
    #: Per-second volatility, scaled to the contract's horizon below.
    volatility_per_second: float
    volatility_to_expiry: float
    seconds_to_expiry: float
    spot_age_seconds: float
    observations_used: int
    spec: Optional[ContractSpec] = None

    def to_maker_proposal(self, min_confidence: float = 0.6) -> Optional[Proposal]:
        """Adapt into the Proposal shape the LLM Maker produces, so Checker,
        RiskGuardrail and Execution need no separate code path.

        Screens on the same executable, cost-inclusive edge the LLM path uses
        (P1 item 7) rather than the midpoint.
        """
        from core.pricing import net_edge

        proposal = Proposal(
            candidate=self.candidate,
            maker_probability=self.probability_yes,
            # The quant path does not self-report confidence the way an LLM
            # does; this is a fixed prior, not a model output.
            maker_confidence=min_confidence,
            reasoning=(
                f"Quant digital-option model: spot={self.spot_price:.4f} "
                f"({self.spot_age_seconds:.0f}s old), strike="
                f"{self.candidate.floor_strike}, {self.seconds_to_expiry:.0f}s to "
                f"expiry, per-second vol={self.volatility_per_second:.3e} from "
                f"{self.observations_used} observations, vol-to-expiry="
                f"{self.volatility_to_expiry:.5f}"
            ),
            source="quant",
        )
        net = net_edge(self.probability_yes, self.candidate, proposal.direction)
        if net < CONFIG.risk.min_edge_threshold:
            return None
        return proposal


class QuantMaker:
    def __init__(self, spot_client: SpotPriceClient = None):
        self.spot = spot_client or SpotPriceClient()
        self._declined: dict[str, str] = {}
        self._vol_logged: set[str] = set()

    def begin_pass(self) -> None:
        """Reset per-pass state. One spot fetch per symbol per pass."""
        self.spot.begin_pass()
        self._declined.clear()
        self._vol_logged.clear()

    # -- routing -----------------------------------------------------------

    def spec_for_candidate(self, candidate: Candidate) -> Optional[ContractSpec]:
        return spec_for(candidate.ticker, candidate.series_ticker)

    def can_handle(self, candidate: Candidate) -> bool:
        """Whether the quant path may price this market.

        Declining here sends the candidate to the LLM path or to being
        skipped, both of which are better than pricing off a spot instrument
        that may not settle the contract.
        """
        if candidate.floor_strike is None or not candidate.strike_type:
            return False
        spec = self.spec_for_candidate(candidate)
        ok, why = usable(spec)
        if not ok:
            # Logged once per family per pass rather than per market — a
            # thousand BTC strikes should not produce a thousand lines.
            #
            # family_of, not ticker[:8]. The fixed slice was a label and a
            # dedup key at once, and it was wrong at both jobs. It printed
            # KXETHD-26AUG1823-T4299.99 as "KXETHD-2", KXHIGHCHI-... as
            # "KXHIGHCH" and KXWTI-... as "KXWTI-26" — three of the four
            # families in the decline log were names that do not exist, so
            # reading the log meant guessing which real family each stood
            # for. KXHIGHNY only looked right by being exactly eight
            # characters long.
            #
            # As a dedup key it is worse than cosmetic: two distinct families
            # sharing their first eight characters collapse into one entry,
            # and the second one is then never logged at all. spec_for()
            # already derives the family correctly by splitting on the first
            # dash; this uses the same rule via the helper scout exports, so
            # the label, the key and the lookup can no longer disagree.
            key = spec.prefix if spec else family_of(candidate.ticker)
            if key not in self._declined:
                self._declined[key] = why
                log.info("Quant path declining %s: %s", key, why)
            return False
        seconds = candidate.seconds_to_close
        if seconds is None or seconds <= 0:
            return False
        if self._in_settlement_blackout(spec, seconds):
            return False
        return True

    def _in_settlement_blackout(self, spec, seconds_to_close: float) -> bool:
        """True inside the window where spot pricing is least defensible.

        Kalshi's crypto contracts settle on a 60-second average of the CF
        Benchmarks Real-Time Index, so the last minute of a contract's life is
        not a price to be predicted — it is the average currently being taken.
        A point-in-time CoinGecko quote is furthest from the settlement value
        precisely there: the averaging damps the very move the model is
        reacting to, and spot-vs-RTI divergence is unhedged.

        This is independent of the ``verified`` flag on purpose. Verifying a
        family means confirming what it settles on; it does not make a spot
        snapshot a good estimate of a 60-second index mean.
        """
        blackout = CONFIG.risk.crypto_settlement_blackout_seconds
        if blackout <= 0 or spec is None:
            return False
        # Keyed on the settlement MECHANISM, not on the feed name. This
        # previously read `spec.source != "crypto"`, which silently stopped
        # protecting these families the moment they moved to source
        # "kalshi_rti" — the gate would have disabled itself during exactly
        # the change it was written to survive. `observation` describes what
        # the contract does; `source` describes where we get a number, and
        # only the first is a property of the market.
        if spec.observation != "rti_60s_average":
            return False
        if seconds_to_close > blackout:
            return False
        key = f"{spec.prefix}:blackout"
        if key not in self._declined:
            self._declined[key] = "settlement blackout"
            log.info(
                "Quant path declining %s: %.0fs to close is inside the %.0fs "
                "crypto settlement blackout (settles on a 60s RTI average, "
                "not a spot print)", spec.prefix, seconds_to_close, blackout,
            )
        return True

    def _vol_is_spiking(self, history, symbol: str, current_vol: float) -> bool:
        """True when trailing realized has spiked vs a longer baseline.

        After a sharp move, short-window realized vol jumps while forward
        implied does not. Using the spiked number as a forward estimate
        invents edge on tails. Refuse rather than trade that.

        Baseline uses a longer lookback (capped by available history). If the
        baseline cannot be measured yet, we do not treat that as a spike.
        """
        max_ratio = getattr(CONFIG.risk, "max_vol_spike_ratio", 1.75)
        if max_ratio <= 1.0 or current_vol <= 0:
            return False

        # Longer baseline: prefer ~4h, but never more than available span.
        baseline_lookback = min(max(history.span_seconds() * 0.9, 1800.0), 14400.0)
        baseline = history.realized_vol_robust(baseline_lookback)
        if not baseline or baseline <= 0:
            return False

        ratio = current_vol / baseline
        if ratio <= max_ratio:
            return False

        key = f"{symbol}:vol_spike"
        if key not in self._declined:
            self._declined[key] = "vol_spike"
            cur_ann = current_vol * _SECONDS_PER_YEAR ** 0.5 * 100
            base_ann = baseline * _SECONDS_PER_YEAR ** 0.5 * 100
            log.info(
                "Quant path declining %s: volatility spike (short %.1f%% vs "
                "baseline %.1f%%, ratio %.2fx > %.2fx). Trailing realized is "
                "not a good forward estimate right now — refusing rather "
                "than inventing edge on tails.",
                symbol, cur_ann, base_ann, ratio, max_ratio,
            )
        return True

    # -- pricing -----------------------------------------------------------

    def propose(self, candidate: Candidate) -> Optional[QuantProposal]:
        spec = self.spec_for_candidate(candidate)
        ok, why = usable(spec)
        if not ok:
            log.debug("%s: %s", candidate.ticker, why)
            return None

        quote = self.spot.get_quote(spec.symbol, spec.source)
        if quote is None:
            log.info("No usable spot quote for %s (%s) — skipping",
                     candidate.ticker, spec.symbol)
            return None
        if quote.is_stale():
            log.warning(
                "Spot quote for %s is %.0fs old (limit %.0fs) — skipping %s",
                spec.symbol, quote.age_seconds,
                CONFIG.risk.max_spot_age_seconds, candidate.ticker,
            )
            return None
        spot = quote.price

        seconds_to_expiry = candidate.seconds_to_close
        if not seconds_to_expiry or seconds_to_expiry <= 0:
            return None
        # Re-checked here as well as in can_handle: propose() is public, and a
        # gate that only exists on one of two entry points is not a gate.
        if self._in_settlement_blackout(spec, seconds_to_expiry):
            return None

        history = self.spot.get_history(spec.symbol)
        if history is None:
            return None
        # Look back roughly 20x the time remaining, bounded, so the estimate
        # is scaled to this contract's horizon rather than a fixed window
        # that is wrong for 15-minute and daily contracts at once.
        lookback = max(3600, min(seconds_to_expiry * 20, 86400))
        if history.span_seconds() < CONFIG.risk.min_vol_span_seconds:
            log.info(
                "Only %.0fs of price history for %s (need %.0fs) — declining "
                "rather than pricing off noise",
                history.span_seconds(), spec.symbol,
                CONFIG.risk.min_vol_span_seconds,
            )
            return None

        # A volatility measured over one window may be carried a few multiples
        # beyond it, not two orders of magnitude.
        span = history.span_seconds()
        horizon_ratio = seconds_to_expiry / span if span > 0 else float("inf")
        if horizon_ratio > CONFIG.risk.max_horizon_vol_span_ratio:
            key = f"{spec.prefix}:horizon"
            if key not in self._declined:
                self._declined[key] = "horizon"
                log.info(
                    "Quant path declining %s: %.0fs to expiry is %.0fx the "
                    "%.0fs of volatility history, over the %.0fx limit. A "
                    "trailing estimate does not reach that far — mean "
                    "reversion makes it the wrong quantity, not just a noisy "
                    "one.",
                    spec.prefix, seconds_to_expiry, horizon_ratio, span,
                    CONFIG.risk.max_horizon_vol_span_ratio,
                )
            return None

        # Measured across a ladder of sampling intervals rather than at the
        # raw tick spacing. BRTI is a smoothed aggregate, and sampling it
        # every 5 seconds sits inside that smoothing — which understated
        # bitcoin vol by ~3x in production and is what produced 0%/100%
        # proposals against a market quoting 7-94%. See
        # PriceHistory.realized_vol_robust.
        vol_per_second = history.realized_vol_robust(lookback)
        if not vol_per_second or vol_per_second <= 0:
            log.info("Insufficient price history for %s — skipping quant proposal",
                     spec.symbol)
            return None

        # Spike refusal: trailing realized after a move is a bad forward
        # estimate. Decline rather than invent edge on tails.
        if self._vol_is_spiking(history, spec.symbol, vol_per_second):
            return None

        # Fail closed on an implausible estimate. This is deliberately a
        # refusal and not a clamp: a sigma this far from reality means the
        # feed or the estimator is wrong, and the honest response to "I do not
        # know the volatility" is not to trade.
        annualized = vol_per_second * _SECONDS_PER_YEAR ** 0.5
        if spec.symbol not in self._vol_logged:
            self._vol_logged.add(spec.symbol)
            log.info(
                "Pricing %s with sigma %.1f%% annualized (%.2e/s, lookback "
                "%.0fs, %d observations)",
                spec.symbol, annualized * 100, vol_per_second, lookback,
                len(history),
            )
        if not (CONFIG.risk.min_plausible_annual_vol
                <= annualized
                <= CONFIG.risk.max_plausible_annual_vol):
            log.warning(
                "Refusing to price %s: volatility estimate implies %.1f%% "
                "annualized, outside the plausible band %.1f%%-%.1f%%. The "
                "estimate is wrong, not the market — declining rather than "
                "pricing off it.",
                spec.symbol, annualized * 100,
                CONFIG.risk.min_plausible_annual_vol * 100,
                CONFIG.risk.max_plausible_annual_vol * 100,
            )
            return None

        strike = candidate.floor_strike
        if strike is None or strike <= 0 or spot <= 0:
            return None

        # Diffusion scaling on real elapsed time, not on an assumed count of
        # poll intervals — and, for index-settled contracts, on the horizon
        # of the quantity that actually settles them.
        effective_seconds = _diffusion_horizon_seconds(spec, seconds_to_expiry)
        vol_to_expiry = vol_per_second * math.sqrt(effective_seconds)
        if vol_to_expiry <= 0:
            return None

        z = math.log(strike / spot) / vol_to_expiry
        prob_above = 1 - _norm_cdf(z)

        strike_type = (candidate.strike_type or "").lower()
        if strike_type in ("greater", "greater_or_equal"):
            prob_yes = prob_above
        elif strike_type in ("less", "less_or_equal"):
            prob_yes = 1 - prob_above
        elif strike_type == "between" and candidate.cap_strike:
            z_cap = math.log(candidate.cap_strike / spot) / vol_to_expiry
            prob_yes = _norm_cdf(z_cap) - _norm_cdf(z)
        else:
            return None

        if not math.isfinite(prob_yes):
            log.warning("Non-finite probability for %s — skipping", candidate.ticker)
            return None
        prob_yes = min(max(prob_yes, 0.001), 0.999)

        return QuantProposal(
            candidate=candidate,
            probability_yes=prob_yes,
            spot_price=spot,
            volatility_per_second=vol_per_second,
            volatility_to_expiry=vol_to_expiry,
            seconds_to_expiry=seconds_to_expiry,
            spot_age_seconds=quote.age_seconds,
            observations_used=len(history),
            spec=spec,
        )
