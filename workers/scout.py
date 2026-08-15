"""
Scout: pulls the current open-event catalog from Kalshi (with nested
markets), filters by Kalshi's own `category` field, and surfaces candidates
worth Maker's attention — minimum liquidity, non-trivial time-to-close.

Categories come from `core.kalshi_categories`, a taxonomy ported from Jon
Becker's 72.1M-trade Kalshi analysis. Two earlier approaches both had holes:

- Guessing from ticker prefixes by hand (KXBTC, KXPGA) covered crypto, golf
  and sports and silently dropped everything else.
- Filtering on Kalshi's own `category` field looked right, but the strings it
  was compared against were guessed from screenshots. `SCOUT_CATEGORIES`
  defaulted to "Sports,Crypto,Politics,Economics,Climate,Culture" and three of
  those six do not exist. A wrong string is invisible: Scout just never
  surfaces that vertical, no error anywhere.

The ported table classifies by ticker, which needs no network call and is
stable. Kalshi's own category string is still recorded on the Candidate as
`kalshi_category` and a disagreement is logged, so the table can be corrected
against live data rather than becoming a third guess.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.kalshi_client import KalshiClient
from core.kalshi_categories import GROUPS, classify_ticker, ticker_prefix
from core.validation import MarketDataInvalid, Quote, validate_market
from config import CONFIG

log = logging.getLogger("daemon_kalshi.scout")


@dataclass
class Candidate:
    ticker: str
    title: str
    #: Taxonomy group from core.kalshi_categories — the canonical string that
    #: SCOUT_CATEGORIES, LLM_REASONING_CATEGORIES and risk's per-category
    #: exposure cap all key off.
    category: str
    yes_bid: float
    yes_ask: float
    volume: float
    close_time: str
    series_ticker: str = ""
    # Markets inside one event are usually mutually exclusive outcomes of the
    # same question, so risk treats the event as the correlation unit and
    # caps exposure across it. Carried from Kalshi's own event object rather
    # than parsed out of the ticker where possible.
    event_ticker: str = ""
    #: Whatever Kalshi's event object called this, kept for cross-checking the
    #: ported taxonomy against live data. Never used for filtering or risk.
    kalshi_category: str = ""
    #: Finer-grained taxonomy levels, e.g. ("Golf", "PGA Tour").
    taxonomy_category: str = ""
    taxonomy_subcategory: str = ""
    strike_type: str = ""          # "greater" | "less" | "between"
    floor_strike: Optional[float] = None
    cap_strike: Optional[float] = None
    #: The exact quote this candidate was built from, with its timestamp.
    #: Carried all the way to submission so risk can reject a proposal whose
    #: quote went stale while the LLM was thinking, instead of trading at a
    #: price that no longer exists.
    quote: Optional[Quote] = None

    def __post_init__(self):
        if self.quote is None:
            # Candidates built by hand (tests, replays) still need a quote.
            self.quote = Quote(
                yes_bid=self.yes_bid, yes_ask=self.yes_ask,
                captured_at=time.time(), source="scan",
            )

    @property
    def implied_yes_probability(self) -> float:
        """Midpoint-implied probability.

        Kept for reporting and for the edge-memory record, but no longer the
        basis for approval — see executable_probability below and P1 item 7.
        The midpoint is not a price anyone can trade at.
        """
        return (self.yes_bid + self.yes_ask) / 2 / 100.0

    def executable_price_cents(self, direction: str) -> float:
        """Price actually payable per contract for `direction`."""
        return self.quote.executable_price_cents(direction)

    def executable_probability(self, direction: str) -> float:
        """Break-even probability implied by the price we would really pay.

        Buying YES at the ask, the market is charging ``ask/100`` for a
        contract worth 1 if YES. Buying NO at ``100 - bid``, the implied
        probability of YES is ``bid/100``. Comparing the model's estimate
        against *this* is what makes an edge tradeable rather than notional.
        """
        price = self.executable_price_cents(direction)
        return price / 100.0 if direction == "yes" else 1.0 - (price / 100.0)

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid

    @property
    def seconds_to_close(self) -> Optional[float]:
        if not self.close_time:
            return None
        try:
            close_dt = datetime.fromisoformat(self.close_time.replace("Z", "+00:00"))
            return (close_dt - datetime.now(timezone.utc)).total_seconds()
        except ValueError:
            return None


class Scout:
    def __init__(self, client: KalshiClient = None):
        self.client = client or KalshiClient()

    def list_available_categories(self) -> list[str]:
        """Call this once to see what's actually live before setting
        SCOUT_CATEGORIES — Kalshi's categories change as new verticals launch."""
        data = self.client.list_categories()
        return sorted(data.get("tags_by_categories", {}).keys())

    def scan(self) -> list[Candidate]:
        candidates: list[Candidate] = []
        cursor = None
        # Matched against taxonomy group names, not Kalshi's raw category
        # field. Empty means "every group".
        wanted = {c.strip().lower() for c in CONFIG.scout_categories if c.strip()}
        unknown = self.unknown_configured_categories()
        if unknown:
            # Loud, because a typo here is otherwise invisible: Scout would
            # simply never surface that vertical and report a smaller count.
            log.error(
                "SCOUT_CATEGORIES contains %s, which no market can ever match. "
                "Valid groups: %s",
                ", ".join(sorted(unknown)), ", ".join(GROUPS),
            )
        skipped_by_group: dict[str, int] = {}
        rejected: dict[str, int] = {}

        # GET /markets, not GET /events?with_nested_markets=true.
        #
        # VERIFIED AGAINST PRODUCTION, 2026-08-15: the nested market objects on
        # the events response carry no price fields. A live scan pulled 50,422
        # markets and validation rejected every single one with
        # "yes_bid is not a finite number (None)". The events endpoint returns
        # market structure; /markets returns the quote.
        #
        # Nothing is lost by the switch. Categories come from the ticker
        # taxonomy rather than the event's category field, so the only thing
        # the events response was still providing was the raw category string
        # kept for auditing — and audit_taxonomy_against_kalshi() still reads
        # it directly when you want it.
        while True:
            page = self.client.list_markets(status="open", limit=200, cursor=cursor)
            for m in page.get("markets", []):
                ticker = m.get("ticker")
                if not ticker:
                    rejected["missing ticker"] = rejected.get("missing ticker", 0) + 1
                    continue
                market_event_ticker = m.get("event_ticker", "")
                group, taxonomy_category, subcategory = classify_ticker(
                    ticker, market_event_ticker
                )
                if wanted and group.lower() not in wanted:
                    skipped_by_group[group] = skipped_by_group.get(group, 0) + 1
                    continue

                # Everything past here is external data being turned into
                # numbers the trading logic will act on, so it is validated
                # first. One malformed market is skipped, not allowed to
                # abort the scan.
                try:
                    valid = validate_market(m)
                except MarketDataInvalid as e:
                    reason = str(e).split(":", 1)[-1].strip()
                    rejected[reason] = rejected.get(reason, 0) + 1
                    log.debug("Rejected market: %s", e)
                    continue
                for warning in valid.warnings:
                    log.debug("%s: %s", valid.ticker, warning)

                if valid.volume < CONFIG.risk.min_liquidity_usd:
                    continue
                candidates.append(
                    Candidate(
                        ticker=valid.ticker,
                        title=valid.title,
                        category=group,
                        yes_bid=valid.quote.yes_bid,
                        yes_ask=valid.quote.yes_ask,
                        volume=valid.volume,
                        close_time=valid.close_time,
                        quote=valid.quote,
                        series_ticker=m.get("series_ticker", ""),
                        event_ticker=market_event_ticker,
                        taxonomy_category=taxonomy_category,
                        taxonomy_subcategory=subcategory,
                        strike_type=valid.strike_type,
                        floor_strike=valid.floor_strike,
                        cap_strike=valid.cap_strike,
                    )
                )

            cursor = page.get("cursor")
            if not cursor:
                break

        self._log_unclassified(candidates)
        if rejected:
            # Aggregated rather than per-market: a feed problem shows up as a
            # count that jumps, and a silent drop to zero candidates now has
            # a visible cause.
            log.warning(
                "Rejected %d market(s) as invalid: %s",
                sum(rejected.values()),
                ", ".join(f"{n}x {reason}" for reason, n in
                          sorted(rejected.items(), key=lambda kv: -kv[1])[:8]),
            )
        log.info(
            "Scout found %d candidates across %s (skipped by group: %s, invalid: %d)",
            len(candidates), wanted or "all groups",
            skipped_by_group or "none", sum(rejected.values()),
        )
        return candidates

    @staticmethod
    def unknown_configured_categories() -> set[str]:
        """SCOUT_CATEGORIES entries that match no taxonomy group."""
        valid = {g.lower() for g in GROUPS}
        return {
            c.strip() for c in CONFIG.scout_categories
            if c.strip() and c.strip().lower() not in valid
        }

    @staticmethod
    def _log_unclassified(candidates: list[Candidate]) -> None:
        """Report markets the taxonomy could not place.

        The ported table is a snapshot of Kalshi's catalog as it was analysed;
        new series will appear that it has never seen. Those land in "Other"
        rather than being guessed at, and this is how you find out the table
        needs extending instead of silently trading a misfiled market.
        """
        unmatched = [c for c in candidates if c.category == "Other"]
        if not unmatched:
            return
        sample = sorted({ticker_prefix(c.event_ticker or c.ticker) for c in unmatched})
        log.warning(
            "%d candidate(s) fell outside the ported taxonomy (group=Other). "
            "Unrecognised ticker prefixes: %s — extend "
            "core/kalshi_categories.py if any of these should be traded.",
            len(unmatched), ", ".join(sample[:20]),
        )

    def audit_taxonomy_against_kalshi(self, limit_events: int = 200) -> list[dict]:
        """Compare the ported taxonomy against Kalshi's own category field.

        Not called by the trading loop — a diagnostic to run once against a
        live account. The taxonomy is a snapshot of someone else's analysis of
        a historical dataset, so it is worth checking rather than trusting,
        and this is how you find the rows that need updating.
        """
        page = self.client.list_events(
            status="open", limit=limit_events, with_nested_markets=True
        )
        rows = []
        for event in page.get("events", []):
            kalshi_category = (event.get("category") or "").strip()
            event_ticker = event.get("event_ticker", "")
            for m in event.get("markets", []):
                ticker = m.get("ticker")
                if not ticker:
                    continue
                group, cat, sub = classify_ticker(
                    ticker, m.get("event_ticker") or event_ticker
                )
                rows.append({
                    "ticker": ticker,
                    "prefix": ticker_prefix(m.get("event_ticker") or event_ticker),
                    "kalshi_category": kalshi_category,
                    "taxonomy_group": group,
                    "taxonomy_category": cat,
                    "taxonomy_subcategory": sub,
                    "agrees": bool(kalshi_category)
                    and kalshi_category.lower() == group.lower(),
                })
        return rows
