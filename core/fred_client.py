"""
FRED (Federal Reserve Economic Data) client — free API key from
https://fred.stlouisfed.org/docs/api/api_key.html (instant signup, no cost).

Covers the economic indicators Kalshi runs markets on: CPI prints, jobs
reports, Fed rate decisions, GDP. FRED itself lags real-time by however long
the underlying agency (BLS, BEA, Fed) takes to publish — it's a source of
the last *official* reading, not a predictor of the next one. Useful for
Maker to know "what's the current trend/level" as context, not as a forecast
of an unreleased number.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import CONFIG

log = logging.getLogger("daemon_kalshi.fred")

# Common Kalshi economics-market keywords -> FRED series ID. Not exhaustive —
# browse https://fred.stlouisfed.org for others as you add market types.
SERIES_MAP = {
    "cpi": "CPIAUCSL",
    "inflation": "CPIAUCSL",
    "core cpi": "CPILFESL",
    "unemployment": "UNRATE",
    "jobs": "PAYEMS",
    "nonfarm payrolls": "PAYEMS",
    "fed funds": "FEDFUNDS",
    "fed rate": "DFEDTARU",       # upper bound of Fed target range
    "gdp": "GDP",
    "pce": "PCEPI",
}


class FredClient:
    def __init__(self, api_key: str = None, timeout: float = 10.0):
        self.api_key = api_key or CONFIG.models.fred_api_key
        self._http = httpx.Client(
            base_url="https://api.stlouisfed.org/fred", timeout=timeout
        )

    def close(self):
        self._http.close()

    def get_latest_observation(self, series_id: str) -> Optional[dict]:
        resp = self._http.get(
            "/series/observations",
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"FRED request failed [{resp.status_code}]: {resp.text[:200]}")
        obs = resp.json().get("observations", [])
        return obs[0] if obs else None

    def get_series_for_keyword(self, keyword: str) -> Optional[dict]:
        keyword = keyword.lower()
        series_id = next((sid for kw, sid in SERIES_MAP.items() if kw in keyword), None)
        if not series_id:
            return None
        latest = self.get_latest_observation(series_id)
        if not latest:
            return None
        return {"series_id": series_id, "date": latest["date"], "value": latest["value"]}
