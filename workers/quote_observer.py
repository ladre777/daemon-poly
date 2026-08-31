"""Runs the quoting probe: record what we would have rested, then measure it.

This is the piece that turns ``QuoteGenerator`` from an argument into a
measurement. Each pass it takes the candidates the scout already fetched,
generates the quote it would have rested on each, writes it down, and resolves
the ones written down on earlier passes against the book as it stands now.

It places nothing. It holds no client and no execution path, so it cannot rest
an order however it is called — the same structural guarantee the arb scanner
and the quote generator have.

Fair value is the market mid, deliberately
------------------------------------------
The obvious thing would be to centre the probe on the bot's own fair value
from the quant or LLM path. That would be the wrong instrument, and the reason
matters.

Adverse selection is a property of the order flow, not of our model. A quote
centred on our fair value confounds the two: a fill that went against us could
mean the flow was informed, or it could mean our estimate was simply wrong,
and the resulting number would answer neither question. Centring on the mid
removes the model from the measurement entirely, leaving "what happens to a
resting order this far from the mid, in this price zone" — which is exactly
the quantity Becker's optimism-tax result is about, since that result is
indexed by price level and not by anyone's alpha.

It also fixes the sample size. Fair values exist only for the handful of
markets that reach the quant path; mids exist for every candidate, so the
probe sees hundreds of observations a pass instead of a few, on markets
selected by the book rather than by what the scout found interesting.

Two things follow and are worth stating rather than discovering later:

* the probe holds no inventory, so the generator's inventory skew is never
  exercised here and the observed fill rate is the un-skewed one;
* these quotes are a microstructure probe, not the strategy's quotes. If the
  probe shows a favourable edge, that is evidence about the flow, and a live
  quoter centred on a real fair value still has to earn its own number.
"""
from __future__ import annotations

import logging
import time

from config import CONFIG
from core.validation import parse_timestamp
from memory.quote_store import QuoteStore
from workers.adverse_selection import AdverseSelectionResolver
from workers.quoting import QuoteGenerator, QuoteRefusal

log = logging.getLogger("daemon_kalshi.quote_observer")


class QuoteObserver:
    """A pass of the quoting probe: observe, then resolve what is due."""

    def __init__(self, store=None, generator=None):
        self.store = store or QuoteStore()
        self.generator = generator or QuoteGenerator()

    def run(self, candidates, now: float = None) -> dict:
        """Record this pass's quotes and resolve earlier ones. Never raises.

        The probe is measurement wired into the trading loop, so a failure in
        it must degrade to "no data this pass" rather than to a pass that
        never reaches execution. The counters it returns are the record of
        which of those two happened.
        """
        now = time.time() if now is None else now
        stats = {"quoted": 0, "refused": 0, "filled_checked": 0,
                 "marked": 0, "unreadable": 0}
        if not CONFIG.quoting.observation_enabled:
            return stats
        try:
            books = _books(candidates)
            stats.update(self._observe(candidates, now))
            # Only this pass's books. A ticker the scout did not return today
            # is unreadable, which the resolver leaves open rather than
            # scoring as "did not fill" — see workers/adverse_selection.py.
            stats.update(AdverseSelectionResolver(
                self.store, books.get).resolve_due(now=now))
        except Exception:
            log.exception("Quote observation pass failed; trading continues")
        return stats

    def _observe(self, candidates, now: float) -> dict:
        entries = []
        refused = 0
        for c in candidates:
            book = _book_of(c)
            if book is None:
                refused += 1
                continue
            yes_bid, yes_ask = book
            expiry = parse_timestamp(getattr(c, "close_time", None))
            if expiry is None:
                refused += 1
                continue
            quote = self.generator.generate(
                ticker=c.ticker,
                fair_value_cents=(yes_bid + yes_ask) / 2.0,
                market_bid_cents=yes_bid,
                market_ask_cents=yes_ask,
                seconds_to_expiry=expiry - now,
                net_inventory=0,
            )
            if isinstance(quote, QuoteRefusal):
                refused += 1
                continue
            entries.append((quote, yes_bid, yes_ask))

        if entries:
            self.store.record_many(entries, at=now)
        return {"quoted": len(entries), "refused": refused}

    def report(self) -> None:
        AdverseSelectionResolver(self.store, lambda _t: None).report()


def _books(candidates) -> dict:
    books = {}
    for c in candidates:
        book = _book_of(c)
        if book is not None:
            books[c.ticker] = book
    return books


def _book_of(c):
    """The candidate's YES book, or None when it is not a two-sided market.

    A one-sided or crossed book is dropped rather than repaired. Both sides
    have to be real prices for "the market came to our price" to mean
    anything, and inventing the missing one would put a fabricated number at
    the base of the only measurement that decides whether quoting is real.
    """
    yes_bid = getattr(c, "yes_bid", None)
    yes_ask = getattr(c, "yes_ask", None)
    if yes_bid is None or yes_ask is None:
        return None
    if not (0 < yes_bid < yes_ask < 100):
        return None
    return (float(yes_bid), float(yes_ask))
