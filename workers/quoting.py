"""Two-sided quote generation. Produces quotes; cannot place them.

Why this exists
---------------
Everything measured on this book says the same thing: the edge, if there is
one, belongs to the maker.

Becker's prediction-market microstructure work — the source
``_longshot_bias_guard`` already cites, and which ``newyorkcompute/kalshi``
implements independently as its optimism-tax strategy — finds that at longshot
YES prices of roughly 1-15c, YES carries an expected value near **-41%** while
NO at those same prices carries about **+23%**. The asymmetry exists because
taker flow is disproportionately optimistic YES buying. Whoever is the
counterparty collects it, and the counterparty is the maker.

This bot's ledger reproduces both halves on entirely separate data:

* Its long-YES book under 20c is **1,144 settled rows with zero wins**.
* The taker version of the profitable side — buying NO at 80-100c, which is
  selling a YES longshot — measures **-$0.0102 a row over 174 distinct
  events**, t = -1.05. Buying NO at 90c needs an 91% win rate to clear the
  fee; the observed rate is 87.3%.

That shortfall is the spread: paid by a taker, earned by a maker. The arb
scanner found the same gap from another direction — the book sits about 2c
from a lock for a taker.

What this module is careful not to be
-------------------------------------
It returns a quote or it returns ``None``. It holds no client, no store and no
execution path, so it cannot rest an order however it is called — the same
structural guarantee the arb scanner has. ``ORDER_STRATEGY=maker`` is still
refused, and this changes nothing about that.

It also does not grade itself. A resting quote is not a taker fill: it executes
only when the market comes to you, which is disproportionately when you are
wrong. Any counterfactual that assumes our quote filled at our price would
credit us the spread on every quote while ignoring adverse selection, and would
manufacture exactly the flattering number four reviews of this ledger have been
spent removing. Fill modelling is deliberately left to a later, measured step.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config import CONFIG
from core.pricing import CONTRACT_PAYOUT_CENTS

log = logging.getLogger("daemon_kalshi.quoting")

#: Zone names, so a refusal or a fill can be attributed to the regime that
#: produced it rather than to "the quoter".
LONGSHOT = "longshot"
NEAR_CERTAIN = "near_certain"
MID_RANGE = "mid_range"


@dataclass(frozen=True)
class TwoSidedQuote:
    """What we would rest, on both sides, in cents.

    ``bid_cents`` is what we would pay for YES; ``ask_cents`` is what we would
    sell YES for. Either size may be zero, which is how inventory skew and
    zone asymmetry suppress one side without suppressing the quote.
    """

    ticker: str
    zone: str
    fair_value_cents: float
    bid_cents: float
    ask_cents: float
    bid_size: int
    ask_size: int

    @property
    def spread_cents(self) -> float:
        return self.ask_cents - self.bid_cents

    @property
    def is_empty(self) -> bool:
        return self.bid_size <= 0 and self.ask_size <= 0

    def describe(self) -> str:
        return (
            f"{self.ticker} [{self.zone}] fair={self.fair_value_cents:.1f}c "
            f"bid {self.bid_size}@{self.bid_cents:.1f}c "
            f"ask {self.ask_size}@{self.ask_cents:.1f}c "
            f"spread={self.spread_cents:.1f}c"
        )


@dataclass(frozen=True)
class QuoteRefusal:
    ticker: str
    reason: str


def zone_for(fair_value_cents: float) -> str:
    q = CONFIG.quoting
    if fair_value_cents < q.longshot_threshold_cents:
        return LONGSHOT
    if fair_value_cents > q.near_certain_threshold_cents:
        return NEAR_CERTAIN
    return MID_RANGE


class QuoteGenerator:
    """Turns a fair value into a two-sided quote, or refuses.

    Stateless by construction. It is handed everything it needs per call and
    keeps nothing between them, so a quote can never depend on an inventory or
    a market that has since changed underneath it.
    """

    def generate(
        self,
        ticker: str,
        fair_value_cents: float,
        market_bid_cents: float,
        market_ask_cents: float,
        seconds_to_expiry: float,
        net_inventory: int = 0,
    ):
        """Return a TwoSidedQuote, or a QuoteRefusal naming why not.

        A refusal is a value rather than ``None`` so that "we did not quote
        this market" is attributable. A quoter that silently produces nothing
        is indistinguishable from one that is broken.
        """
        q = CONFIG.quoting

        if not 0 < fair_value_cents < CONTRACT_PAYOUT_CENTS:
            return QuoteRefusal(ticker, f"fair value {fair_value_cents:.1f}c "
                                        f"is not a probability")
        if market_ask_cents <= market_bid_cents:
            return QuoteRefusal(ticker, "market is crossed or one-sided")

        market_spread = market_ask_cents - market_bid_cents
        if market_spread > q.max_market_spread_cents:
            # A book is wide because nobody knows the price. Resting inside it
            # is not liquidity provision, it is volunteering to be the one who
            # finds out.
            return QuoteRefusal(
                ticker, f"market spread {market_spread:.1f}c over the "
                        f"{q.max_market_spread_cents:.0f}c limit")

        if seconds_to_expiry <= q.stop_quote_seconds:
            # Near expiry a resting quote cannot be repriced fast enough to
            # stay ahead of the settlement it is about to be measured by.
            return QuoteRefusal(
                ticker, f"{seconds_to_expiry:.0f}s to expiry is inside the "
                        f"{q.stop_quote_seconds:.0f}s stop-quote window")

        zone = zone_for(fair_value_cents)
        if zone is MID_RANGE and q.skip_mid_range:
            return QuoteRefusal(ticker, "mid-range quoting disabled")

        edge = q.zone_edge_cents if zone != MID_RANGE else q.mid_edge_cents
        bid = fair_value_cents - edge
        ask = fair_value_cents + edge

        # The spread floor is applied by widening symmetrically around fair
        # value, never by moving one side only: shifting a single side would
        # quietly change the position this quote expresses, which is a
        # different decision from quoting it more cautiously.
        if ask - bid < q.min_spread_cents:
            half = q.min_spread_cents / 2.0
            bid, ask = fair_value_cents - half, fair_value_cents + half

        bid = max(1.0, round(bid, 1))
        ask = min(CONTRACT_PAYOUT_CENTS - 1.0, round(ask, 1))
        if ask - bid < q.min_spread_cents:
            return QuoteRefusal(
                ticker, f"clamping to the 1-99c range left a "
                        f"{ask - bid:.1f}c spread, under the "
                        f"{q.min_spread_cents:.0f}c floor")

        bid_size, ask_size = self._sizes(zone, net_inventory)
        quote = TwoSidedQuote(
            ticker=ticker, zone=zone, fair_value_cents=fair_value_cents,
            bid_cents=bid, ask_cents=ask,
            bid_size=bid_size, ask_size=ask_size,
        )
        if quote.is_empty:
            return QuoteRefusal(
                ticker, f"inventory {net_inventory:+d} suppressed both sides "
                        f"in the {zone} zone")
        return quote

    @staticmethod
    def _sizes(zone: str, net_inventory: int) -> tuple[int, int]:
        """Contracts per side, after zone asymmetry and inventory skew.

        Zone asymmetry is the whole strategy: in the longshot zone we want to
        be the seller of YES to optimistic takers, so the ask rests and the bid
        does not. Near certainty the same flow runs the other way — takers
        overpay for the NO longshot — so the bid rests instead.

        Inventory skew is applied after, and only ever REMOVES size from the
        side that would add to an existing position. It can zero a side; it can
        never grow one, so no combination of zone and inventory can make this
        quote larger than size_per_side.
        """
        q = CONFIG.quoting
        base = max(0, q.size_per_side)

        if zone == LONGSHOT:
            bid_size, ask_size = 0, base
        elif zone == NEAR_CERTAIN:
            bid_size, ask_size = base, 0
        else:
            bid_size, ask_size = base, base

        limit = q.max_inventory
        if limit > 0:
            if net_inventory >= limit:
                bid_size = 0       # already long; stop adding
            if net_inventory <= -limit:
                ask_size = 0       # already short; stop adding
        return bid_size, ask_size
