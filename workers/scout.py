"""
Scout: pulls the current open-event catalog from Kalshi (with nested
markets), filters by Kalshi's own `category` field, and surfaces candidates
worth Maker's attention — minimum liquidity, non-trivial time-to-close.

Earlier version of this file guessed category from ticker prefixes (KXBTC,
KXPGA, etc.) — that only covered crypto/golf/sports and silently dropped
politics, elections, economics, weather, and culture markets, which is
exactly the part of Kalshi's catalog Polymarket US doesn't have. This version
uses the category Kalshi itself assigns to each event via GET /events, so
nothing gets missed by a heuristic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.kalshi_client import KalshiClient
from config import CONFIG

log = logging.getLogger("daemon_kalshi.scout")


@dataclass
class Candidate:
    ticker: str
    title: str
    category: str
    yes_bid: float
    yes_ask: float
    volume: float
    close_time: str
    series_ticker: str = ""
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
        # Case-insensitive match against Kalshi's own category strings
        # (e.g. "Politics", "Sports", "Economics", "Crypto", "Weather", "Culture").
        wanted = {c.strip().lower() for c in CONFIG.scout_categories if c.strip()}

        while True:
            page = self.client.list_events(
                status="open", limit=200, cursor=cursor, with_nested_markets=True
            )
            for event in page.get("events", []):
                category = (event.get("category") or "other").strip()
                if wanted and category.lower() not in wanted:
                    continue

                for m in event.get("markets", []):
                    volume = float(m.get("volume", 0))
                    if volume < CONFIG.risk.min_liquidity_usd:
                        continue
                    candidates.append(
                        Candidate(
                            ticker=m["ticker"],
                            title=m.get("title", event.get("title", m["ticker"])),
                            category=category,
                            yes_bid=float(m.get("yes_bid", 0)),
                            yes_ask=float(m.get("yes_ask", 100)),
                            volume=volume,
                            close_time=m.get("close_time", event.get("close_time", "")),
                            series_ticker=event.get("series_ticker", ""),
                            strike_type=m.get("strike_type", ""),
                            floor_strike=m.get("floor_strike"),
                            cap_strike=m.get("cap_strike"),
                        )
                    )

            cursor = page.get("cursor")
            if not cursor:
                break

        log.info("Scout found %d candidates across %s", len(candidates), wanted or "all categories")
        return candidates
