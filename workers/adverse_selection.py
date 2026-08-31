"""Did the market come to us, and were we glad when it did?

The measurement that decides whether the optimism-tax quoting edge is real.

A maker's problem is not whether the spread is wide enough — that is
arithmetic, and ``QuoteGenerator`` already refuses anything that cannot clear
the round-trip fee. The problem is that a resting quote fills when the market
comes to you, which is disproportionately when you are about to be wrong. If
the adverse fills outweigh the spread captured, a strategy with a positive
theoretical edge loses money on every one of them.

Nothing here assumes a fill. Both questions have observable answers:

    filled a resting BID at B   <=>  the market's ask fell to B or below
                                     (somebody sold into us)
    filled a resting ASK at A   <=>  the market's bid rose to A or above
                                     (somebody bought from us)

and the mark is where the mid sat LATER STILL, at a third reading:

    bought at B   mark = mid2 - B      positive means the price rose after
    sold at A     mark = A - mid2      positive means the price fell after

The fill and the mark must come from different readings, and the first version
of this got that wrong in a way worth recording. Marking against the same
snapshot that established the fill is degenerate: filling a bid at B requires
the ask to fall to B or below, and the mid is always below the ask, so
mid - B is negative for arithmetic reasons that have nothing to do with the
market. Every fill would have read as adverse and the metric would have been
measuring its own definition. Adverse selection is about where the price went
AFTER the fill, so it needs a later look.

Three biases, stated plainly because they cannot be netted
----------------------------------------------------------
* **Sampling understates fills.** Passes are minutes apart. A market that
  traded through our price and reverted inside the interval is invisible here
  and counts as no fill.
* **Queue position overstates fills.** Being priced at a level is not being
  first in line at it. A real resting order can sit unfilled through a print
  that this model counts as a fill.
* **One reading per stage understates round trips.** Both sides can only be
  recorded as filled together if the book was crossed at the single instant of
  the fill check, which real books are not. ``round_trips`` is therefore a
  floor near zero and is not the measure of spread capture; the per-side fill
  counts and their marks are.

The first two point in opposite directions and there is no basis in this data
for claiming they cancel. The output is therefore a bound on the shape of the
problem, not a fill simulator, and it is labelled that way wherever it is
reported.
"""
from __future__ import annotations

import logging
import time

from config import CONFIG

log = logging.getLogger("daemon_kalshi.adverse_selection")


def would_fill_bid(bid_cents: float, market_ask_cents: float) -> bool:
    """A resting buy fills when a seller crosses down to it."""
    return market_ask_cents <= bid_cents


def would_fill_ask(ask_cents: float, market_bid_cents: float) -> bool:
    """A resting sell fills when a buyer crosses up to it."""
    return market_bid_cents >= ask_cents


def mid_cents(yes_bid: float, yes_ask: float) -> float:
    return (yes_bid + yes_ask) / 2.0


class AdverseSelectionResolver:
    """Resolves recorded quotes against a later reading of the same market.

    Holds a quote store and a way to read a market. It cannot place, cancel or
    size anything — it is a measurement, and the only thing it writes is the
    resolution of an observation it already had.
    """

    def __init__(self, quote_store, read_market):
        self.store = quote_store
        #: callable(ticker) -> (yes_bid, yes_ask) or None when unreadable.
        self.read_market = read_market

    def resolve_due(self, now: float = None) -> dict:
        """Advance every observation that is due for its next stage.

        Two stages, in order: the fill check, then — only for fills, and only
        at a strictly later reading — the mark. An observation whose market
        cannot be read is left where it is rather than recorded as "did not
        fill": a market we could not see is not a market that stood still.
        """
        now = time.time() if now is None else now
        q = CONFIG.quoting
        out = {"filled_checked": 0, "marked": 0, "unreadable": 0}

        for obs in self.store.awaiting_fill_check(
                older_than=now - q.observation_min_age_seconds):
            book = self.read_market(obs["ticker"])
            if not book:
                out["unreadable"] += 1
                continue
            self._check_fill(obs, now, *book)
            out["filled_checked"] += 1

        for obs in self.store.awaiting_mark(
                older_than=now - q.mark_horizon_seconds):
            book = self.read_market(obs["ticker"])
            if not book:
                out["unreadable"] += 1
                continue
            self._mark(obs, now, *book)
            out["marked"] += 1

        if any(out.values()):
            log.info(
                "Adverse-selection resolver: %d fill check(s), %d mark(s), "
                "%d left open (market unreadable)",
                out["filled_checked"], out["marked"], out["unreadable"],
            )
        return out

    def _check_fill(self, obs: dict, now: float,
                    yes_bid: float, yes_ask: float) -> None:
        # A side that was never quoted cannot have filled. Checking SIZE here
        # rather than price is what keeps a suppressed side — the whole
        # mechanism of the longshot zone — out of the fill statistics.
        self.store.record_fill_check(
            obs["id"], t1=now, yes_bid=yes_bid, yes_ask=yes_ask,
            filled_bid=bool(obs["bid_size"]) and would_fill_bid(
                obs["bid_cents"], yes_ask),
            filled_ask=bool(obs["ask_size"]) and would_fill_ask(
                obs["ask_cents"], yes_bid),
        )

    def _mark(self, obs: dict, now: float,
              yes_bid: float, yes_ask: float) -> None:
        """Mark a fill against a reading strictly later than the fill check."""
        mid = mid_cents(yes_bid, yes_ask)
        self.store.record_mark(
            obs["id"], t2=now, yes_bid=yes_bid, yes_ask=yes_ask,
            bid_mark_cents=(mid - obs["bid_cents"]) if obs["filled_bid"] else None,
            ask_mark_cents=(obs["ask_cents"] - mid) if obs["filled_ask"] else None,
        )

    def report(self, zones=("longshot", "near_certain", "mid_range")) -> None:
        """Log the shape per zone. Never aggregated across them.

        The zones are the strategy: they rest different sides for different
        reasons, so a fill rate averaged over all three describes none of
        them.
        """
        for zone in zones:
            s = self.store.summary(zone)
            if not s["n"]:
                continue
            fills = s["bid_fills"] + s["ask_fills"]
            adverse = s["bid_adverse"] + s["ask_adverse"]
            log.info(
                "Adverse selection [%s]: n=%d quotes | fills bid=%d ask=%d "
                "round_trips=%d | adverse=%d (%.0f%% of fills) | "
                "mean mark bid=%s ask=%s cents "
                "(bounded estimate: sampling understates fills, queue "
                "position overstates them)",
                zone, s["n"], s["bid_fills"], s["ask_fills"], s["round_trips"],
                adverse, (100.0 * adverse / fills) if fills else 0.0,
                _fmt(s["bid_mark"]), _fmt(s["ask_mark"]),
            )


def _fmt(v):
    return "n/a" if v is None else f"{v:+.2f}"
