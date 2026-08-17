"""
Spot price sources for the quant path — fast, numeric, no LLM involved.

Crypto: CoinGecko's free tier, no key. Rate-limited (roughly 10-30 calls/min
depending on current policy).

Commodities: Kalshi's "15 minute commodities" markets track gold/silver ETF
prices, so an ETF quote may be the *right* reference rather than a proxy —
though see core/contract_specs.py, where that assumption is recorded as
unverified. Sourced from Yahoo Finance's unofficial v8 chart endpoint — free,
no key, same category of "undocumented but widely used" as the ESPN
endpoints, with the same caveat: it can break or start rate-limiting without
notice.

Data quality (P1 item 8)
------------------------
The previous version called the API once per candidate, recorded whatever
came back into the volatility buffer, and returned None on failure with no
backoff. That produced four distinct problems:

1. **Duplicated observations.** Ten BTC markets in one scan meant ten
   identical prices appended to the history buffer. Realized volatility is
   computed from that buffer, so duplicates drove the estimate toward zero —
   and vol appears in the denominator of the probability calculation, so
   understated vol means overconfident probabilities on every market.
2. **No rate-limit handling.** Ten calls per pass against a ~10/min budget
   gets throttled, and a 429 returned None, which the quant path read as
   "no price" rather than "back off".
3. **Unknown sampling interval.** ``realized_vol`` scaled by the number of
   observations, assuming they were evenly spaced at the poll interval. With
   duplicates and gaps they were not, so the scaling was wrong by an unknown
   factor.
4. **No staleness or sanity check.** A cached quote from an hour ago, or a
   feed glitch printing 0.0 or 10x, was used exactly like a good one.

Fixes: one fetch per symbol per scan pass, timestamps and quote age on every
observation, staleness rejection, bounded backoff with a circuit breaker,
outlier filtering, and a volatility estimate that uses the actual elapsed
time between observations instead of assuming.
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import httpx

from config import CONFIG

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


class StaleQuote(Exception):
    """A quote exists but is too old to trade on."""


@dataclass(frozen=True)
class SpotQuote:
    symbol: str
    price: float
    #: When the price was observed by us. The free feeds do not reliably
    #: report their own print time, so this is our read time — it bounds
    #: staleness by our clock only.
    observed_at: float
    source: str

    @property
    def age_seconds(self) -> float:
        return max(time.time() - self.observed_at, 0.0)

    def is_stale(self, max_age: float = None) -> bool:
        limit = max_age if max_age is not None else CONFIG.risk.max_spot_age_seconds
        return self.age_seconds > limit


class PriceHistory:
    """Rolling buffer of (timestamp, price) for realized-volatility estimation.

    Deliberately stores the timestamp with every point: the vol estimate needs
    the real elapsed time between observations, not an assumed poll interval.
    """

    def __init__(self, maxlen: int = 500):
        self._buf: deque[tuple[float, float]] = deque(maxlen=maxlen)
        # The RTI feed writes from its own thread while the scan loop reads.
        # A single deque append is atomic under CPython, but `add` inspects
        # the buffer for ordering and outliers before appending and
        # `realized_vol` walks the whole thing — neither is atomic.
        self._lock = threading.RLock()

    def __len__(self) -> int:
        return len(self._buf)

    @property
    def points(self) -> list[tuple[float, float]]:
        with self._lock:
            return list(self._buf)

    def add(self, price: float, at: float = None) -> bool:
        """Append an observation. Returns False if it was rejected.

        Rejects duplicates at the same instant and prices that are obvious
        outliers against the recent median — a feed glitch printing 0.0 or a
        10x tick would otherwise poison the volatility estimate for as long as
        it stays in the window.
        """
        at = at if at is not None else time.time()
        if price is None or not math.isfinite(price) or price <= 0:
            return False
        with self._lock:
            return self._add_locked(price, at)

    def _add_locked(self, price: float, at: float) -> bool:
        if self._buf and at <= self._buf[-1][0]:
            # Same-instant or out-of-order: several candidates in one pass
            # asking for the same symbol must not each append a point.
            return False
        if self.is_outlier(price):
            log.warning(
                "Rejecting outlier price %.4f (recent median %.4f)",
                price, self.median() or 0.0,
            )
            return False
        self._buf.append((at, price))
        return True

    def median(self, window: int = 20) -> Optional[float]:
        with self._lock:
            if not self._buf:
                return None
            recent = sorted(p for _, p in list(self._buf)[-window:])
        mid = len(recent) // 2
        if len(recent) % 2:
            return recent[mid]
        return (recent[mid - 1] + recent[mid]) / 2.0

    def is_outlier(self, price: float) -> bool:
        """True if `price` is implausibly far from the recent median.

        Needs a few points before it can judge anything; with an empty buffer
        every price is accepted, which is correct — there is nothing to
        compare against.
        """
        with self._lock:
            if len(self._buf) < 5:
                return False
        med = self.median()
        if not med or med <= 0:
            return False
        ratio = price / med
        limit = CONFIG.risk.spot_outlier_ratio
        return ratio > limit or ratio < 1.0 / limit

    def realized_vol(self, lookback_seconds: float = 3600) -> Optional[float]:
        """Per-second stdev of log returns over the lookback window.

        Returns a *per-second* figure, not "per observation". The old version
        returned per-observation-interval stdev and the caller scaled it by
        the number of poll intervals to expiry, which assumed observations
        were evenly spaced at exactly SCOUT_POLL_SECONDS. They are not: passes
        take variable time, fetches fail, and duplicates used to be recorded.
        Normalising each return by its own elapsed time removes the assumption
        and makes the units explicit.
        """
        cutoff = time.time() - lookback_seconds
        with self._lock:
            points = [(t, p) for t, p in self._buf if t >= cutoff and p > 0]
        if len(points) < CONFIG.risk.min_vol_observations:
            return None

        per_second: list[float] = []
        for i in range(1, len(points)):
            dt = points[i][0] - points[i - 1][0]
            if dt <= 0:
                continue
            log_return = math.log(points[i][1] / points[i - 1][1])
            # Diffusion scaling: a return over dt seconds has stdev
            # sigma*sqrt(dt), so dividing by sqrt(dt) puts every observation
            # on a common per-second footing regardless of spacing.
            per_second.append(log_return / math.sqrt(dt))
        if len(per_second) < CONFIG.risk.min_vol_observations - 1:
            return None

        mean = sum(per_second) / len(per_second)
        variance = sum((r - mean) ** 2 for r in per_second) / (len(per_second) - 1)
        vol = math.sqrt(max(variance, 0.0))
        return vol if vol > 0 else None

    def span_seconds(self) -> float:
        with self._lock:
            if len(self._buf) < 2:
                return 0.0
            return self._buf[-1][0] - self._buf[0][0]


class SpotPriceClient:
    def __init__(self, timeout: float = 8.0, http=None, rti_feed=None,
                 price_store=None):
        #: Optional RTIFeed. When present, families whose spec says
        #: source="kalshi_rti" are priced off the CF Benchmarks index Kalshi
        #: relays, which is what actually settles them. When absent, those
        #: families get no quote and the quant path declines — deliberately,
        #: rather than falling back to the spot feed the exchange says is the
        #: wrong instrument.
        self.rti_feed = rti_feed
        self._http = http or httpx.Client(
            timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}
        )
        self.history: dict[str, PriceHistory] = {}
        #: Latest quote per symbol, reused for the rest of the scan pass.
        self._quotes: dict[str, SpotQuote] = {}
        #: Per-source backoff state: (blocked_until, consecutive_failures).
        self._backoff: dict[str, tuple[float, int]] = {}
        self.fetch_counts: dict[str, int] = {}
        #: Last stored streaming observation per symbol, for downsampling.
        self._last_tick_at: dict[str, float] = {}
        #: Durable backing for the volatility buffers. Without it every
        #: restart resets the volatility clock and the quant path never warms
        #: up — see memory/price_store.py.
        self.price_store = price_store

    def close(self):
        self._http.close()

    # -- durable history ---------------------------------------------------

    def restore_history(self, symbols=None) -> dict[str, int]:
        """Reload observations persisted by a previous process.

        Returns points restored per symbol, so startup can say plainly
        whether the quant path begins warm or cold.

        Restoring is additive and safe: `PriceHistory.add` still rejects
        out-of-order and outlier points, so a stale or corrupt row cannot
        smuggle itself past the checks a live tick has to clear.
        """
        if self.price_store is None:
            return {}
        restored: dict[str, int] = {}
        for symbol in (symbols or list(self.history) or ["btc", "eth"]):
            points = self.price_store.load(symbol)
            if not points:
                continue
            history = self.history.setdefault(symbol, PriceHistory())
            added = sum(1 for at, price in points if history.add(price, at))
            if added:
                restored[symbol] = added
                self._last_tick_at[symbol] = points[-1][0]
        return restored

    def persist_history(self) -> int:
        """Write the current buffers out. Cheap enough to call once a pass.

        Saves the whole rolling buffer rather than a delta; the store's
        primary key makes re-saving overlapping points a no-op, which is what
        keeps this correct without tracking what was already written.
        """
        if self.price_store is None:
            return 0
        written = 0
        for symbol, history in self.history.items():
            written += self.price_store.save(symbol, history.points)
        if written:
            self.price_store.prune()
        return written

    # -- streaming observations --------------------------------------------

    def record_tick(self, symbol: str, price: float, at: float = None) -> bool:
        """Record an index observation that arrived without being asked for.

        The volatility estimate used to be fed only by :meth:`get_quote`, once
        per symbol per scan pass. At a five-minute poll interval, reaching the
        600-second span the estimator requires took the better part of an hour
        of uninterrupted uptime — and the buffer is in memory, so every
        redeploy set it back to zero. Production spent a whole session
        reporting::

            Only 0s of price history for eth (need 600s) — declining rather
            than pricing off noise

        with the CF Benchmarks feed simultaneously delivering roughly two
        observations a second of exactly the right instrument, all discarded.

        Ticks are downsampled to ``RTI_TICK_SAMPLE_SECONDS`` before being
        stored. That is not a performance concern: the buffer holds 500
        points, so recording every frame would give it a span of about four
        minutes — well under the 600 seconds required — and the estimator
        would never be satisfied no matter how long the process ran. Sampling
        every 5 seconds gives roughly 40 minutes of span in the same buffer,
        and clears the 600-second bar after ten minutes of uptime.

        Returns whether the tick was stored, so callers can count.
        """
        key = (symbol or "").lower()
        if not key:
            return False
        at = at if at is not None else time.time()
        interval = CONFIG.risk.rti_tick_sample_seconds
        if interval > 0:
            last = self._last_tick_at.get(key)
            if last is not None and at - last < interval:
                return False
        history = self.history.setdefault(key, PriceHistory())
        if not history.add(price, at):
            return False
        self._last_tick_at[key] = at
        return True

    # -- scan-pass caching -------------------------------------------------

    def begin_pass(self) -> None:
        """Start a new scan pass.

        Clears the per-pass quote cache so the next request for each symbol
        fetches once. Everything after that in the same pass reuses it, which
        is what stops ten BTC markets from making ten API calls and writing
        ten duplicate observations into the volatility buffer.
        """
        self._quotes.clear()

    def cached_quote(self, symbol: str) -> Optional[SpotQuote]:
        return self._quotes.get(symbol.lower())

    # -- backoff -----------------------------------------------------------

    def _blocked(self, source: str) -> bool:
        until, _ = self._backoff.get(source, (0.0, 0))
        return time.time() < until

    def _record_failure(self, source: str) -> None:
        _, failures = self._backoff.get(source, (0.0, 0))
        failures += 1
        # Exponential with jitter, capped. Jitter matters because every
        # symbol on a source fails together, and synchronised retries are how
        # a rate limit becomes a sustained one.
        delay = min(
            CONFIG.risk.spot_backoff_base_seconds * (2 ** (failures - 1)),
            CONFIG.risk.spot_backoff_max_seconds,
        )
        delay *= 0.5 + random.random()
        self._backoff[source] = (time.time() + delay, failures)
        log.warning(
            "%s failing (%d consecutive) — backing off %.0fs",
            source, failures, delay,
        )

    def _record_success(self, source: str) -> None:
        if source in self._backoff:
            del self._backoff[source]

    # -- fetching ----------------------------------------------------------

    def get_quote(self, symbol: str, source: str) -> Optional[SpotQuote]:
        """One quote per symbol per pass, with staleness and backoff handling."""
        symbol = symbol.lower()
        cached = self._quotes.get(symbol)
        if cached is not None:
            return None if cached.is_stale() else cached

        if self._blocked(source):
            log.debug("%s is in backoff — no quote for %s this pass", source, symbol)
            return None

        try:
            if source == "kalshi_rti":
                # The settling instrument itself, not a proxy. No HTTP call:
                # the index arrives over a WebSocket and this reads the
                # newest value held. Returns None when the feed is cold or
                # stale, which makes the quant path decline — it must never
                # fall back to spot, because spot is precisely the instrument
                # the exchange states does not settle these markets.
                price = self._fetch_rti(symbol)
            elif source == "crypto":
                price = self._fetch_crypto(symbol)
            else:
                price = self._fetch_etf(symbol)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.warning("%s request failed for %s: %r", source, symbol, e)
            self._record_failure(source)
            return None

        if price is None:
            self._record_failure(source)
            return None

        self._record_success(source)
        self.fetch_counts[symbol] = self.fetch_counts.get(symbol, 0) + 1
        quote = SpotQuote(symbol=symbol, price=price, observed_at=time.time(),
                          source=source)
        self._quotes[symbol] = quote
        # One observation per accepted fetch, timestamped.
        self.history.setdefault(symbol, PriceHistory()).add(price, quote.observed_at)
        return quote

    def _fetch_rti(self, symbol: str) -> Optional[float]:
        """Latest CF Benchmarks index level for this asset, or None.

        No fallback by design. If the feed is absent, cold or stale the
        answer is "we do not know", and the only safe response to that is to
        not price the market.
        """
        if self.rti_feed is None:
            log.warning(
                "No RTI feed configured — cannot price %s off its settling "
                "index. Declining rather than substituting spot.", symbol,
            )
            return None
        return self.rti_feed.value_for_symbol(symbol)

    def _fetch_crypto(self, symbol: str) -> Optional[float]:
        cg_id = COINGECKO_IDS.get(symbol)
        if not cg_id:
            return None
        resp = self._http.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd"},
        )
        if resp.status_code == 429:
            log.warning("CoinGecko rate limited")
            return None
        if resp.status_code != 200:
            log.warning("CoinGecko request failed [%s]", resp.status_code)
            return None
        try:
            price = resp.json().get(cg_id, {}).get("usd")
        except ValueError:
            return None
        return self._sanitise(price, symbol)

    def _fetch_etf(self, symbol: str) -> Optional[float]:
        yf_symbol = YAHOO_SYMBOLS.get(symbol, symbol.upper())
        resp = self._http.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
        )
        if resp.status_code == 429:
            log.warning("Yahoo rate limited for %s", yf_symbol)
            return None
        if resp.status_code != 200:
            log.warning("Yahoo chart request failed [%s] for %s",
                        resp.status_code, yf_symbol)
            return None
        try:
            result = resp.json()["chart"]["result"][0]
            price = result["meta"]["regularMarketPrice"]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        return self._sanitise(price, symbol)

    @staticmethod
    def _sanitise(price, symbol: str) -> Optional[float]:
        """Reject values that are not usable prices before they enter state."""
        try:
            out = float(price)
        except (TypeError, ValueError):
            log.warning("Non-numeric price %r for %s", price, symbol)
            return None
        if not math.isfinite(out) or out <= 0:
            log.warning("Implausible price %r for %s", price, symbol)
            return None
        return out

    # -- backwards-compatible helpers --------------------------------------

    def crypto_price(self, symbol: str) -> Optional[float]:
        quote = self.get_quote(symbol, "crypto")
        return quote.price if quote else None

    def etf_price(self, symbol: str) -> Optional[float]:
        quote = self.get_quote(symbol, "etf")
        return quote.price if quote else None

    def get_history(self, symbol: str) -> Optional[PriceHistory]:
        return self.history.get(symbol.lower())
