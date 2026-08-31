"""
Structural (locked) arbitrage detection.

From ``brandononchain/kalshibot``, whose dual-side strategy is the one
model-free edge in the whole survey: buy YES and NO on the same market when
the two asks together cost less than the $1.00 that exactly one of them will
pay. Whichever way the event resolves, one leg settles at 100c and the other
at 0c, so the profit is locked at trade time and does not depend on being
right about anything.

That repo's condition is::

    combined = kalshi_yes_ask + kalshi_no_ask
    IF combined < $0.98:   # "guarantees ~2% return after fees"

**That test is wrong, and it is wrong in the direction that loses money.**
The 2c allowance is a flat guess at fees. Kalshi's taker fee is
``ceil(0.07 * P * (1-P) * 100)`` per contract, which peaks near 50c — and a
locked arb is *by construction* two contracts priced near the middle, i.e.
exactly where the fee is largest. Two legs at 49c and 49c carry roughly
1.75c of fee each: 3.5c total, not 2c. So a 98c combined cost that repo calls
a 2% profit is actually a ~1.5c **loss** per pair.

This implementation therefore charges the real fee on both legs, using the
same ``fee_cents_per_contract`` the rest of the bot prices with, and requires
the profit to survive it. Nothing here is declared an edge before fees.

Scope, deliberately: this module **detects and reports**. It does not place
orders. Executing a two-legged trade needs both legs to fill or neither, and
this bot's execution path submits one intent at a time with no order-lifecycle
management (see the P0-3 refusal in ``workers/execution.py``). A half-filled
arb is not a smaller arb — it is an unhedged directional position taken for
reasons that have nothing to do with a view on the outcome, which is strictly
worse than not trading. Auto-execution is blocked on the same order-lifecycle
work that gates maker mode; until then this surfaces opportunities to the
operator rather than quietly taking leg risk.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config import CONFIG
from core.pricing import CONTRACT_PAYOUT_CENTS, fee_cents_per_contract

log = logging.getLogger("daemon_kalshi.arbitrage")


@dataclass
class LockedArb:
    """A dual-side opportunity whose profit does not depend on the outcome."""

    ticker: str
    yes_ask_cents: float
    no_ask_cents: float
    yes_fee_cents: float
    no_fee_cents: float
    #: Cost of one YES+NO pair, fees included.
    total_cost_cents: float
    #: Guaranteed profit per pair after fees. Positive by construction — a
    #: LockedArb is only ever built when this clears the configured floor.
    profit_cents: float
    max_pairs: int

    @property
    def profit_per_pair_pct(self) -> float:
        """Return on capital committed, per pair."""
        return self.profit_cents / self.total_cost_cents if self.total_cost_cents else 0.0

    @property
    def total_profit_cents(self) -> float:
        return self.profit_cents * self.max_pairs

    def describe(self) -> str:
        return (
            f"{self.ticker}: YES {self.yes_ask_cents:.0f}c + NO "
            f"{self.no_ask_cents:.0f}c + fees "
            f"{self.yes_fee_cents + self.no_fee_cents:.2f}c = "
            f"{self.total_cost_cents:.2f}c per pair, locking "
            f"{self.profit_cents:.2f}c ({self.profit_per_pair_pct:.2%}) "
            f"x{self.max_pairs} pairs"
        )


def find_locked_arb(
    ticker: str,
    yes_ask_cents: Optional[float],
    no_ask_cents: Optional[float],
    available_pairs: int = 1,
) -> Optional[LockedArb]:
    """Return a LockedArb if buying both sides is profitable after real fees.

    ``no_ask_cents`` is the NO book's own ask. Callers holding only a YES
    quote can derive it as ``100 - yes_bid``, but should prefer the real NO
    ask when the exchange gives one: the derived number assumes a two-sided
    book that a thin market may not have, and an arb detected against an
    assumed price is not an arb.

    Returns None — never a zero-profit or negative LockedArb — so a caller
    cannot accidentally act on one by forgetting to check the sign.
    """
    if yes_ask_cents is None or no_ask_cents is None:
        return None
    if not (0 < yes_ask_cents < CONTRACT_PAYOUT_CENTS):
        return None
    if not (0 < no_ask_cents < CONTRACT_PAYOUT_CENTS):
        return None
    if available_pairs < 1:
        return None

    yes_fee = fee_cents_per_contract(yes_ask_cents)
    no_fee = fee_cents_per_contract(no_ask_cents)
    total_cost = yes_ask_cents + no_ask_cents + yes_fee + no_fee

    # Exactly one leg settles at 100c; the other expires worthless.
    profit = CONTRACT_PAYOUT_CENTS - total_cost
    if profit < CONFIG.arbitrage.min_profit_cents:
        return None

    max_pairs = min(available_pairs, CONFIG.arbitrage.max_pairs)
    if max_pairs < 1:
        return None

    return LockedArb(
        ticker=ticker,
        yes_ask_cents=yes_ask_cents,
        no_ask_cents=no_ask_cents,
        yes_fee_cents=yes_fee,
        no_fee_cents=no_fee,
        total_cost_cents=total_cost,
        profit_cents=profit,
        max_pairs=max_pairs,
    )


class ArbitrageScanner:
    """Watches candidates for locked arbs and reports the ones it finds.

    Stateless apart from per-pass deduplication, so a market that stays
    mispriced for several passes produces one report rather than one per scan.
    """

    def __init__(self, notifier=None):
        self.notifier = notifier
        self._reported: set[str] = set()
        self.real_no_ask = 0
        self.derived_no_ask = 0
        #: Net cost of a YES+NO pair minus the 100c payout, per market, in
        #: cents. Negative is a lock. Kept per pass so the distribution can be
        #: reported rather than just its sign.
        self.gaps: list[tuple[float, str]] = []

    def scan(self, candidates) -> list[LockedArb]:
        """Return every locked arb among these candidates, best first."""
        if not CONFIG.arbitrage.enabled:
            return []

        found: list[LockedArb] = []
        for c in candidates:
            quote = getattr(c, "quote", None)
            if quote is None:
                continue
            # Kalshi's own NO ask when the payload carried one. The derived
            # `100 - yes_bid` is kept only as a fallback and is counted
            # separately, because it cannot detect a real arb: substituting
            # it makes the test `yes_ask - yes_bid + fees < 0`, i.e. a book
            # crossed by more than the fees. That is why this scanner logged
            # nothing at all in its first ten days of production.
            real_no_ask = getattr(quote, "no_ask", None)
            if real_no_ask is None:
                self.derived_no_ask += 1
                no_ask = CONTRACT_PAYOUT_CENTS - quote.yes_bid
            else:
                self.real_no_ask += 1
                no_ask = real_no_ask
            arb = find_locked_arb(c.ticker, quote.yes_ask, no_ask)
            if arb is not None:
                found.append(arb)
            # How far the book is from a lock, whether or not one exists.
            #
            # "Found zero" is a binary that cannot distinguish a market one
            # fee-tick away from a lock from one twenty cents away, and those
            # call for opposite decisions: the first says keep watching at a
            # finer cadence, the second says this venue does not offer the
            # trade and the scanner is dead weight. Ten days of silence was
            # uninformative for exactly this reason.
            self._record_gap(c.ticker, quote.yes_ask, no_ask)

        found.sort(key=lambda a: a.profit_cents, reverse=True)
        for arb in found:
            self._report(arb)
        return found

    def _record_gap(self, ticker: str, yes_ask: float, no_ask: float) -> None:
        """Distance from a lock, in cents, fees included.

        Skips books this scanner could not price either way, so the
        distribution describes markets that were genuinely evaluated rather
        than being diluted by ones that were never candidates.
        """
        if not (0 < yes_ask < CONTRACT_PAYOUT_CENTS):
            return
        if not (0 < no_ask < CONTRACT_PAYOUT_CENTS):
            return
        cost = (yes_ask + no_ask
                + fee_cents_per_contract(yes_ask)
                + fee_cents_per_contract(no_ask))
        self.gaps.append((cost - CONTRACT_PAYOUT_CENTS, ticker))

    def gap_summary(self) -> Optional[dict]:
        """Per-pass shape of how close the book came to a lock.

        Returns None when nothing was priced, which is not the same as
        "everything was far away" and must not be reported as a number.
        """
        if not self.gaps:
            return None
        ordered = sorted(self.gaps)
        cents = [g for g, _ in ordered]

        def q(f):
            return cents[min(len(cents) - 1, int(f * len(cents)))]

        return {
            "n": len(cents),
            "best": ordered[0][0],
            "best_ticker": ordered[0][1],
            "p10": q(0.10),
            "median": q(0.50),
            "within_1c": sum(1 for c in cents if c < 1.0),
            "within_3c": sum(1 for c in cents if c < 3.0),
            "within_10c": sum(1 for c in cents if c < 10.0),
        }

    def _report(self, arb: LockedArb) -> None:
        if arb.ticker in self._reported:
            return
        self._reported.add(arb.ticker)
        log.warning("LOCKED ARB: %s", arb.describe())
        if self.notifier is not None:
            try:
                self.notifier.notify_locked_arb(arb)
            except Exception:
                # Same rule as every other alert path: a notification failure
                # must not take down the pass that found the opportunity.
                log.exception("Locked-arb alert failed for %s", arb.ticker)

    def begin_pass(self) -> None:
        """Clear per-pass dedupe. Called once per scan, like QuantMaker."""
        self._reported.clear()
        # Counted per pass so the log can say how much of the book this
        # scanner can actually see. A scan that priced every market off a
        # derived NO ask has not looked for arbs, it has looked for crossed
        # books, and the two are not the same search.
        self.real_no_ask = 0
        self.derived_no_ask = 0
        #: Net cost of a YES+NO pair minus the 100c payout, per market, in
        #: cents. Negative is a lock. Kept per pass so the distribution can be
        #: reported rather than just its sign.
        self.gaps: list[tuple[float, str]] = []
