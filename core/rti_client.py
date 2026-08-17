"""
CF Benchmarks Real-Time Index values, relayed by Kalshi.

Kalshi's crypto contracts settle on a 60-second simple average of a CF
Benchmarks RTI — confirmed verbatim from `rules_primary` on three live
markets, see core/contract_specs.py. `rules_secondary` goes further and names
the mistake this module exists to correct:

    "While checking a source like Google or Coinbase may help guide your
     decision, the price used to determine this market is based on CF
     Benchmarks' corresponding Real Time Index (RTI)."

The quant path priced these off CoinGecko spot, which is the instrument the
exchange explicitly says does not settle them.

Transport
---------
Kalshi relays the index on an authenticated WebSocket channel,
``cfbenchmarks_value``, subscribed by ``index_ids``. Two fields matter:

- ``avg_60s_data.value`` — trailing 60-second average, always present. This
  is the current state of the index, and what pricing runs against.
- ``last_60s_windowed_average_15min`` — the 60-second average *ending at* a
  quarter-hour boundary, published only during the final minute before
  :00/:15/:30/:45. For KXBTC15M this is the settling value itself, and it is
  also the strike (that market's strike is the same quantity taken at its
  open, which is likewise a quarter-hour boundary).

The windowed value is therefore observable only as it forms, inside the
settlement blackout. It cannot forecast; it is recorded for audit and for
after-the-fact settlement checks, not used to price.

Fail-closed
-----------
Every path here returns ``None`` rather than a fallback number. A missing or
stale index value must make the quant path decline, never make it substitute
a different instrument — silently reverting to spot is precisely the bug
being fixed. This module never falls back to CoinGecko.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from config import CONFIG
from core.validation import finite, parse_timestamp

log = logging.getLogger("daemon_kalshi.rti")

#: Kalshi's WebSocket channel name for the relay.
CHANNEL = "cfbenchmarks_value"

#: Which CF Benchmarks index settles which asset. Confirmed from rules text:
#: bitcoin families name "BRTI"; the ether family names "Ethereum Real-Time
#: Index (ERTI)", which is informal prose for the index whose API identifier
#: is ETHUSD_RTI.
#:
#: Keyed by the spec's `symbol`, so an asset with no confirmed index simply
#: has no entry and is refused rather than defaulted onto bitcoin's.
INDEX_FOR_SYMBOL: dict[str, str] = {
    "btc": "BRTI",
    "eth": "ETHUSD_RTI",
}


def index_for_symbol(symbol: str) -> Optional[str]:
    return INDEX_FOR_SYMBOL.get((symbol or "").lower())


@dataclass(frozen=True)
class RTIQuote:
    """One observation of a CF Benchmarks index, as relayed by Kalshi."""

    index_id: str
    #: Trailing 60-second average — the current state of the index.
    value: float
    #: The quarter-hour windowed average, present only in the final minute
    #: before a :00/:15/:30/:45 close. None the rest of the time, which is
    #: the normal case and not an error.
    windowed_15min: Optional[float]
    received_at: float

    @property
    def age_seconds(self) -> float:
        return max(time.time() - self.received_at, 0.0)

    def is_stale(self, max_age: float = None) -> bool:
        limit = max_age if max_age is not None else CONFIG.risk.max_spot_age_seconds
        return self.age_seconds > limit


def parse_frame(frame: dict) -> Optional[RTIQuote]:
    """Turn one `cfbenchmarks_value` message into an RTIQuote, or None.

    Deliberately strict, and strict in the same way as core/validation: a
    field that is present but unusable is a refusal, not a coerced zero. A
    NaN index value that slipped through would make every edge NaN and fail
    every threshold comparison silently.
    """
    if not isinstance(frame, dict):
        return None

    index_id = frame.get("index_id") or frame.get("indexId")
    if not isinstance(index_id, str) or not index_id:
        return None

    # avg_60s_data is documented as an object carrying `value`; tolerate a
    # bare number too rather than dropping a usable quote on shape alone.
    avg = frame.get("avg_60s_data")
    raw_value = avg.get("value") if isinstance(avg, dict) else avg
    value = finite(raw_value)
    if value is None or value <= 0:
        return None

    windowed = finite(frame.get("last_60s_windowed_average_15min"))
    if windowed is not None and windowed <= 0:
        windowed = None

    received = parse_timestamp(frame.get("timestamp")) or time.time()
    return RTIQuote(index_id=index_id, value=value, windowed_15min=windowed,
                    received_at=received)


def subscribe_command(index_ids: list[str], cmd_id: int = 1) -> dict:
    """The subscribe frame for this channel.

    Uses `index_ids`, NOT `market_tickers` — the channel rejects the latter,
    and core/kalshi_ws.subscribe() sends only market_tickers, which is why it
    could not be reused unchanged.
    """
    return {
        "id": cmd_id,
        "cmd": "subscribe",
        "params": {"channels": [CHANNEL], "index_ids": list(index_ids)},
    }


class RTIFeed:
    """Latest index value per index_id, updated by the WebSocket reader.

    A cache, not a fetcher. The scan loop reads the newest value it has;
    whatever drives the socket calls ``apply_frame``. Reads and writes are
    guarded because the reader and the scan loop are different threads.

    Holds no fallback and no default. An index that has never reported, or
    whose last report has aged out, returns None and the caller declines.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._latest: dict[str, RTIQuote] = {}
        self.frames_applied = 0
        self.frames_rejected = 0

    def apply_frame(self, frame: dict) -> Optional[RTIQuote]:
        quote = parse_frame(frame)
        if quote is None:
            self.frames_rejected += 1
            return None
        with self._lock:
            self._latest[quote.index_id] = quote
            self.frames_applied += 1
        return quote

    def latest(self, index_id: str) -> Optional[RTIQuote]:
        with self._lock:
            return self._latest.get(index_id)

    def value_for_symbol(self, symbol: str) -> Optional[float]:
        """Current index level for an asset, or None.

        None covers every failure the same way on purpose: no index mapped,
        never connected, nothing received yet, or the last value has gone
        stale. The caller cannot act differently on any of them — all four
        mean "we do not know what this settles against right now".
        """
        index_id = index_for_symbol(symbol)
        if index_id is None:
            log.debug("No CF Benchmarks index is confirmed for %r", symbol)
            return None
        quote = self.latest(index_id)
        if quote is None:
            log.warning("No %s value received yet — cannot price %s off the "
                        "settling index", index_id, symbol)
            return None
        if quote.is_stale():
            log.warning("%s value is %.0fs old (limit %.0fs) — declining "
                        "rather than pricing off a stale index",
                        index_id, quote.age_seconds,
                        CONFIG.risk.max_spot_age_seconds)
            return None
        return quote.value

    @property
    def is_healthy(self) -> bool:
        """True when at least one index has a fresh value.

        Used to decide whether RTI-settled families may be priced at all.
        """
        with self._lock:
            quotes = list(self._latest.values())
        return any(not q.is_stale() for q in quotes)
