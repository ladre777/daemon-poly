"""
Spot price sources for the quant path — fast, numeric, no LLM involved.

Crypto: CoinGecko's free tier, no key. Rate-limited (roughly 10-30 calls/min
depending on current policy) — don't hammer it every poll cycle for every
market, cache and share across candidates in the same pass.

Commodities: Kalshi's "15 minute commodities" markets (GLD/SLV badges you
saw in the app) track gold/silver ETF prices, not spot metal directly, so an
ETF quote is actually the *right* reference, not a proxy. Sourced from
Yahoo Finance's unofficial v8 chart endpoint — free, no key, same category
of "undocumented but widely used" as the ESPN endpoints, with the same
caveat: it can break or start rate-limiting without notice, and production
use should have a fallback path.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Optional

import httpx

log = logging.getLogger("daemon_kalshi.spot")

COINGECKO_IDS = {
    "btc": "bitcoin", "bitcoin": "bitcoin",
    "eth": "ethereum", "ethereum": "ethereum",
    "sol": "solana", "solana": "solana",
}

YAHOO_SYMBOLS = {
    "gold": "GLD", "gld": "GLD",
    "silver": "SLV", "slv": "SLV",
}


class PriceHistory:
    """Rolling buffer of (timestamp, price) so QuantMaker can estimate
    short-term realized volatility instead of needing a separate paid
    historical-data source. Volatility estimates start weak with an empty
    buffer and improve as the bot runs — this is one of the two 'gets
    smarter over time' mechanisms, distinct from the LLM calibration loop."""

    def __init__(self, maxlen: int = 500):
        self._buf: deque[tuple[float, float]] = deque(maxlen=maxlen)

    def add(self, price: float):
        self._buf.append((time.time(), price))

    def realized_vol(self, lookback_seconds: float = 3600) -> Optional[float]:
        """Annualized-style vol from log returns over the lookback window.
        Returns None if there isn't enough history yet to trust it."""
        cutoff = time.time() - lookback_seconds
        points = [(t, p) for t, p in self._buf if t >= cutoff]
        if len(points) < 5:
            return None
        log_returns = [
            math.log(points[i][1] / points[i - 1][1])
            for i in range(1, len(points))
            if points[i - 1][1] > 0
        ]
        if len(log_returns) < 4:
            return None
        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        return math.sqrt(max(variance, 0.0))  # per-observation-interval stdev


class SpotPriceClient:
    def __init__(self, timeout: float = 8.0):
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        self.history: dict[str, PriceHistory] = {}

    def close(self):
        self._http.close()

    def _record(self, symbol: str, price: float):
        self.history.setdefault(symbol, PriceHistory()).add(price)

    def crypto_price(self, symbol: str) -> Optional[float]:
        cg_id = COINGECKO_IDS.get(symbol.lower())
        if not cg_id:
            return None
        resp = self._http.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd"},
        )
        if resp.status_code != 200:
            log.warning("CoinGecko request failed [%s]", resp.status_code)
            return None
        price = resp.json().get(cg_id, {}).get("usd")
        if price is not None:
            self._record(symbol.lower(), price)
        return price

    def etf_price(self, symbol: str) -> Optional[float]:
        yf_symbol = YAHOO_SYMBOLS.get(symbol.lower(), symbol.upper())
        resp = self._http.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}")
        if resp.status_code != 200:
            log.warning("Yahoo chart request failed [%s] for %s", resp.status_code, yf_symbol)
            return None
        try:
            result = resp.json()["chart"]["result"][0]
            price = result["meta"]["regularMarketPrice"]
        except (KeyError, IndexError, TypeError):
            return None
        self._record(symbol.lower(), price)
        return price

    def get_history(self, symbol: str) -> Optional[PriceHistory]:
        return self.history.get(symbol.lower())
