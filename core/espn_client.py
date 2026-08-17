"""
ESPN client — wraps the free, keyless, unofficial endpoints documented at
https://github.com/pseudo-r/Public-ESPN-API. No API key exists for these;
ESPN's old official Developer Center (and its apikey param) was retired
years ago. This is exactly what Maker needs for independent grounding on
sports/golf markets instead of anchoring on Kalshi's own price.

Caveat baked into the design: these are unofficial endpoints ESPN can change
without notice. Every method raises on non-200 rather than silently
returning stale/empty data, so a broken endpoint fails loudly in your logs
instead of quietly feeding Maker garbage.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("daemon_kalshi.espn")

SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports"
SITE_WEB_BASE = "https://site.web.api.espn.com/apis/site/v2/sports"
COMMON_V3_BASE = "https://site.web.api.espn.com/apis/common/v3/sports"

# Golf and tennis take a tour SLUG, not a numeric league id.
GOLF_TOURS = {"pga", "lpga", "champions-tour", "korn-ferry-tour"}


#: Sent on every request.
#:
#: The previous value was a bare ``User-Agent: Mozilla/5.0`` and nothing else.
#: That string is not what any browser sends — it is the prefix of one — and a
#: request carrying it with no Accept, no Accept-Language and no Referer is a
#: recognisable automated-client signature.
#:
#: Whether that is *why* production started returning 403 is NOT established.
#: A 403 on a keyless public endpoint can equally come from datacenter-IP
#: reputation, in which case no header set helps at all. ``_get`` below logs
#: enough of the refusal to tell those cases apart — the headers are the cheap
#: hypothesis, the logging is what actually answers the question.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
}

#: Response headers worth calling out by name when a request is refused. These
#: identify *who* refused it: an edge/CDN layer names itself here, and that
#: determines whether the request or the client IP is the problem.
_DIAGNOSTIC_HEADERS = (
    "server", "cf-ray", "cf-cache-status", "x-cache", "via", "x-served-by",
    "x-amz-cf-id", "akamai-grn", "retry-after", "content-type",
    "x-ratelimit-remaining", "x-error", "x-request-id",
)

#: Bound on the logged body. A block page is HTML and can be large; the
#: distinguishing text is always near the top.
MAX_BODY_LOG_CHARS = 2000


class ESPNClient:
    def __init__(self, timeout: float = 10.0, headers: Optional[dict] = None):
        self._http = httpx.Client(
            timeout=timeout, headers=dict(headers or BROWSER_HEADERS)
        )

    def close(self):
        self._http.close()

    def _describe_refusal(self, resp) -> str:
        """Everything about a non-200 that is worth having in the log.

        The old message was the status code and the URL — enough to know
        something is wrong, and nothing else. A 403 from ESPN's application
        and a 403 from a CDN bot filter are the same integer with completely
        different fixes; the response headers and body are what separate them.

        Cookie VALUES are dropped and only names kept: a bot-detection cookie
        being set is the diagnostic signal, its contents are not. The body is
        bounded because a third party's response should not be piped verbatim
        and unlimited into a log stream.
        """
        named = {
            key: resp.headers.get(key)
            for key in _DIAGNOSTIC_HEADERS
            if resp.headers.get(key)
        }
        other = sorted(
            k for k in resp.headers
            if k.lower() not in _DIAGNOSTIC_HEADERS and k.lower() != "set-cookie"
        )
        cookies = sorted(
            c.split("=", 1)[0].strip()
            for c in resp.headers.get_list("set-cookie")
        )

        try:
            body = resp.text or ""
        except Exception:                       # noqa: BLE001 - diagnostics only
            body = "<undecodable>"
        total = len(body)
        if total > MAX_BODY_LOG_CHARS:
            body = (f"[{total} chars, first {MAX_BODY_LOG_CHARS}] "
                    f"{body[:MAX_BODY_LOG_CHARS]}")
        else:
            body = f"[{total} chars, complete] {body}"

        return (
            f"status={resp.status_code} reason={resp.reason_phrase!r} | "
            f"diagnostic headers={named} | "
            f"set-cookie names={cookies or 'none'} | "
            f"other headers={other} | "
            f"request headers sent={sorted(self._http.headers)} | "
            f"body={body}"
        )

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        resp = self._http.get(url, params=params)
        if resp.status_code != 200:
            log.error("ESPN refused %s — %s", url, self._describe_refusal(resp))
            raise RuntimeError(f"ESPN request failed [{resp.status_code}]: {url}")
        return resp.json()

    # -- general scoreboard / standings (any sport/league) -------------------

    def scoreboard(self, sport: str, league: str, dates: Optional[str] = None) -> dict:
        """dates format: YYYYMMDD, or a range YYYYMMDD-YYYYMMDD. Omit for 'today'."""
        params = {"dates": dates} if dates else None
        return self._get(f"{SITE_BASE}/{sport}/{league}/scoreboard", params=params)

    def standings(self, sport: str, league: str) -> dict:
        return self._get(f"https://site.api.espn.com/apis/v2/sports/{sport}/{league}/standings")

    def game_summary(self, sport: str, league: str, event_id: str) -> dict:
        return self._get(f"{SITE_BASE}/{sport}/{league}/summary", params={"event": event_id})

    def athlete_overview(self, sport: str, league: str, athlete_id: str) -> dict:
        return self._get(f"{COMMON_V3_BASE}/{sport}/{league}/athletes/{athlete_id}/overview")

    # -- golf specifically ----------------------------------------------------

    def golf_leaderboard(self, tour: str = "pga") -> dict:
        """Current/active tournament leaderboard. tour: pga, lpga, champions-tour,
        korn-ferry-tour — a slug, not a numeric id."""
        if tour not in GOLF_TOURS:
            log.warning("Unrecognized golf tour slug '%s' — passing through anyway", tour)
        return self._get(f"{SITE_BASE}/golf/{tour}/scoreboard")

    def golf_player_round(
        self, tour: str, event_id: str, player_id: str, season: int
    ) -> dict:
        """Hole-by-hole scoring for one player in one event — this is the
        granular data DÆMON-POLY's partial-round parsing worked against.
        Returns profile, rounds[] (each with linescores[]: per-hole
        strokes/par/scoreType), and stats[]."""
        url = f"{SITE_WEB_BASE}/golf/{tour}/leaderboard/{event_id}/playersummary"
        return self._get(url, params={"season": season, "player": player_id})

    # -- search -----------------------------------------------------------

    def search(self, query: str, sport: Optional[str] = None, limit: int = 10) -> dict:
        params = {"query": query, "limit": limit}
        if sport:
            params["sport"] = sport
        return self._get("https://site.api.espn.com/apis/search/v2", params=params)
