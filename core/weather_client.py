"""
NOAA/National Weather Service client (api.weather.gov) — free, no API key,
no rate-limit auth required (just a descriptive User-Agent, which NOAA asks
for and enforces).

Station mapping matters more than the forecast source here: Kalshi's weather
markets settle on the NWS Daily Climate Report for one SPECIFIC station per
city, and the obvious airport isn't always it — Chicago settles on Midway
(KMDW), not O'Hare; Houston settles on Hobby (KHOU), not Bush; NYC settles on
Central Park (KNYC), not LaGuardia. Getting this wrong doesn't just add
noise, it grounds Maker on the wrong city's weather entirely. The list below
covers the ~17 US cities I could confirm station-level from public sources —
verify each one against the specific market's rules before trusting it,
since Kalshi's exact roster of ~20 cities shifts and market rules are the
actual source of truth, not this file.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("daemon_kalshi.weather")

# city keyword (lowercase, as it'd appear in a Kalshi market title) ->
# (ICAO station id, lat, lon)
WEATHER_STATIONS: dict[str, tuple[str, float, float]] = {
    "atlanta": ("KATL", 33.6407, -84.4277),
    "austin": ("KAUS", 30.1975, -97.6664),
    "boston": ("KBOS", 42.3656, -71.0096),
    "chicago": ("KMDW", 41.7868, -87.7522),          # Midway, not O'Hare
    "dallas": ("KDFW", 32.8998, -97.0403),
    "washington": ("KDCA", 38.8512, -77.0402),        # Reagan National
    "denver": ("KDEN", 39.8561, -104.6737),
    "houston": ("KHOU", 29.6454, -95.2789),            # Hobby, not Bush
    "jacksonville": ("KJAX", 30.4941, -81.6879),
    "los angeles": ("KLAX", 33.9416, -118.4085),
    "miami": ("KMIA", 25.7959, -80.2870),
    "minneapolis": ("KMSP", 44.8848, -93.2223),
    "new york": ("KNYC", 40.7794, -73.9691),             # Central Park, not LGA
    "philadelphia": ("KPHL", 39.8721, -75.2411),
    "san antonio": ("KSAT", 29.5312, -98.4677),
    "san francisco": ("KSFO", 37.6213, -122.3790),
    "seattle": ("KSEA", 47.4502, -122.3088),
}

# Kalshi's actual market titles abbreviate ("LA", "NYC", "SF", "DC", "CHI")
# rather than spelling cities out — confirmed from real screenshots
# ("Highest temperature in LA today?", "...in NYC today?"). Matching only
# full names, as an earlier version of this file did, silently misses these
# and returns no grounding data at all with no error to notice it by.
CITY_ALIASES: dict[str, str] = {
    "nyc": "new york",
    "la": "los angeles",
    "sf": "san francisco",
    "dc": "washington",
    "chi": "chicago",
}


class NOAAClient:
    def __init__(self, user_agent: str = "daemon-kalshi (contact: set-your-email-here)", timeout: float = 10.0):
        # NOAA explicitly asks every consumer to set a real identifying
        # User-Agent — unset/generic ones get rate-limited harder.
        self._http = httpx.Client(
            base_url="https://api.weather.gov",
            headers={"User-Agent": user_agent, "Accept": "application/geo+json"},
            timeout=timeout,
        )

    def close(self):
        self._http.close()

    def _get(self, path: str, **kwargs) -> dict:
        resp = self._http.get(path, **kwargs)
        if resp.status_code != 200:
            raise RuntimeError(f"NOAA request failed [{resp.status_code}]: {path}")
        return resp.json()

    def get_point(self, lat: float, lon: float) -> dict:
        return self._get(f"/points/{lat},{lon}")

    def get_forecast(self, lat: float, lon: float) -> dict:
        """Official NWS forecast (the same source Kalshi's settlement report
        derives from) — returns periods[] including today's forecast high."""
        point = self.get_point(lat, lon)
        forecast_url = point["properties"]["forecast"]
        resp = self._http.get(forecast_url)
        if resp.status_code != 200:
            raise RuntimeError(f"NOAA forecast fetch failed [{resp.status_code}]: {forecast_url}")
        return resp.json()

    def get_latest_observation(self, station_id: str) -> dict:
        """Most recent actual reading at the station — useful intraday to see
        how close today's running high already is to a Kalshi threshold."""
        return self._get(f"/stations/{station_id}/observations/latest")

    def get_city_forecast(self, city_keyword: str) -> Optional[dict]:
        station = WEATHER_STATIONS.get(city_keyword.lower())
        if not station:
            return None
        icao, lat, lon = station
        forecast = self.get_forecast(lat, lon)
        try:
            observation = self.get_latest_observation(icao)
            current_temp_c = observation["properties"]["temperature"]["value"]
        except Exception:
            current_temp_c = None
        today = forecast["properties"]["periods"][0] if forecast["properties"]["periods"] else {}
        return {
            "station": icao,
            "forecast_today": today.get("detailedForecast"),
            "forecast_high_f": today.get("temperature") if today.get("isDaytime") else None,
            "current_temp_f": (current_temp_c * 9 / 5 + 32) if current_temp_c is not None else None,
        }
