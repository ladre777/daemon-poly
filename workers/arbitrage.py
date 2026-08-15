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

    def scan(self, candidates) -> list[LockedArb]:
        """Return every locked arb among these candidates, best first."""
        if not CONFIG.arbitrage.enabled:
            return []

        found: list[LockedArb] = []
        for c in candidates:
            quote = getattr(c, "quote", None)
            if quote is None:
                continue
            # The NO ask derived from the YES bid. Kalshi quotes a real NO
            # book too; when the Scout starts carrying it, prefer that and
            # pass it straight through — see find_locked_arb's docstring.
            no_ask = CONTRACT_PAYOUT_CENTS - quote.yes_bid
            arb = find_locked_arb(c.ticker, quote.yes_ask, no_ask)
            if arb is not None:
                found.append(arb)

        found.sort(key=lambda a: a.profit_cents, reverse=True)
        for arb in found:
            self._report(arb)
        return found

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
