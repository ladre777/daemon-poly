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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.kalshi_client import KalshiClient
from core.kalshi_categories import GROUPS, classify_ticker, ticker_prefix
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

    @property
    def implied_yes_probability(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2 / 100.0

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

        while True:
            page = self.client.list_events(
                status="open", limit=200, cursor=cursor, with_nested_markets=True
            )
            for event in page.get("events", []):
                kalshi_category = (event.get("category") or "").strip()
                event_ticker = event.get("event_ticker", "")

                for m in event.get("markets", []):
                    ticker = m.get("ticker")
                    if not ticker:
                        continue
                    market_event_ticker = m.get("event_ticker") or event_ticker
                    group, taxonomy_category, subcategory = classify_ticker(
                        ticker, market_event_ticker
                    )
                    if wanted and group.lower() not in wanted:
                        skipped_by_group[group] = skipped_by_group.get(group, 0) + 1
                        continue

                    volume = float(m.get("volume", 0))
                    if volume < CONFIG.risk.min_liquidity_usd:
                        continue
                    candidates.append(
                        Candidate(
                            ticker=ticker,
                            title=m.get("title", event.get("title", ticker)),
                            category=group,
                            yes_bid=float(m.get("yes_bid", 0)),
                            yes_ask=float(m.get("yes_ask", 100)),
                            volume=volume,
                            close_time=m.get("close_time", event.get("close_time", "")),
                            series_ticker=event.get("series_ticker", ""),
                            event_ticker=market_event_ticker,
                            kalshi_category=kalshi_category,
                            taxonomy_category=taxonomy_category,
                            taxonomy_subcategory=subcategory,
                            strike_type=m.get("strike_type", ""),
                            floor_strike=m.get("floor_strike"),
                            cap_strike=m.get("cap_strike"),
                        )
                    )

            cursor = page.get("cursor")
            if not cursor:
                break

        self._log_unclassified(candidates)
        log.info(
            "Scout found %d candidates across %s (skipped by group: %s)",
            len(candidates), wanted or "all groups",
            skipped_by_group or "none",
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
