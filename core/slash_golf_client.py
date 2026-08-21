"""
Slash Golf (Live Golf Data) client — RapidAPI.

Provides live PGA Tour and LIV leaderboards. ESPN is permanently blocked
from Railway IPs, so this is the only viable golf data source for the bot.

Auth: x-rapidapi-key + x-rapidapi-host headers.
Base: https://live-golf-data.p.rapidapi.com

Key flow for current event:
  1. GET /schedule?year=YYYY&orgId=1  → find current tournId
  2. GET /leaderboard?tournId=...&year=YYYY&orgId=1 → live leaderboard
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("daemon_kalshi.slash_golf")

RAPIDAPI_HOST = "live-golf-data.p.rapidapi.com"
BASE_URL = f"https://{RAPIDAPI_HOST}"

# Cache the current tournament id for a few minutes so we don't burn quota
# on every candidate.
_TOURN_CACHE_TTL = 300  # seconds
_tourn_cache: dict[str, Any] = {"tourn_id": None, "name": None, "year": None, "fetched_at": 0.0}


class SlashGolfClient:
    def __init__(self, api_key: str, timeout: float = 12.0):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "x-rapidapi-key": self.api_key,
                "x-rapidapi-host": RAPIDAPI_HOST,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        ) if self.api_key else None

    @property
    def available(self) -> bool:
        return bool(self.api_key and self._client)

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        if not self.available:
            return None
        try:
            r = self._client.get(path, params=params or {})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            log.warning("Slash Golf HTTP %s on %s: %s", e.response.status_code, path, e.response.text[:200])
            return None
        except Exception:
            log.exception("Slash Golf request failed: %s", path)
            return None

    def current_tournament(self, year: int | None = None, org_id: str = "1") -> dict | None:
        """Return the most relevant live/upcoming tournament for the given year.

        Prefers a tournament whose dates cover today. Falls back to the
        nearest future event if nothing is currently active.
        """
        year = year or datetime.now(timezone.utc).year
        now = time.time()
        if (
            _tourn_cache["tourn_id"]
            and _tourn_cache["year"] == year
            and now - _tourn_cache["fetched_at"] < _TOURN_CACHE_TTL
        ):
            return {
                "tournId": _tourn_cache["tourn_id"],
                "name": _tourn_cache["name"],
                "year": year,
            }

        data = self._get("/schedule", {"year": str(year), "orgId": org_id})
        if not data:
            return None

        schedule = data.get("schedule") or data.get("tournaments") or []
        today = date.today()

        # Prefer a tournament that is currently underway
        live = None
        upcoming = None
        for t in schedule:
            tourn_id = t.get("tournId") or t.get("id")
            name = t.get("name") or t.get("tournamentName") or ""
            if not tourn_id:
                continue
            # Try common date field shapes
            start = _parse_date(t.get("startDate") or t.get("date") or t.get("start"))
            end = _parse_date(t.get("endDate") or t.get("end"))
            if start and end and start <= today <= end:
                live = {"tournId": str(tourn_id), "name": name, "year": year}
                break
            if start and start >= today and upcoming is None:
                upcoming = {"tournId": str(tourn_id), "name": name, "year": year}

        chosen = live or upcoming
        if chosen:
            _tourn_cache.update({
                "tourn_id": chosen["tournId"],
                "name": chosen["name"],
                "year": year,
                "fetched_at": now,
            })
            log.info("Slash Golf current tournament: %s (%s)", chosen["name"], chosen["tournId"])
        return chosen

    def leaderboard(
        self,
        tourn_id: str | None = None,
        year: int | None = None,
        org_id: str = "1",
        round_id: str | None = None,
    ) -> dict | None:
        """Fetch live leaderboard. If tourn_id omitted, resolves current event."""
        year = year or datetime.now(timezone.utc).year
        if not tourn_id:
            current = self.current_tournament(year=year, org_id=org_id)
            if not current:
                return None
            tourn_id = current["tournId"]

        params = {"tournId": str(tourn_id), "year": str(year), "orgId": org_id}
        if round_id:
            params["roundId"] = str(round_id)

        data = self._get("/leaderboard", params)
        if not data:
            return None

        # Normalise a few possible response shapes
        rows = (
            data.get("leaderboardRows")
            or data.get("leaderboard")
            or data.get("players")
            or []
        )
        return {
            "tournId": tourn_id,
            "year": year,
            "name": data.get("name") or data.get("tournamentName") or _tourn_cache.get("name"),
            "rows": rows,
            "raw": data,
        }

    def close(self) -> None:
        if self._client:
            self._client.close()


def _parse_date(raw) -> date | None:
    if not raw:
        return None
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
