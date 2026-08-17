"""
Drives the CF Benchmarks index feed that :mod:`core.rti_client` caches.

Why this is separate from the feed
----------------------------------
``RTIFeed`` is a cache with no I/O: something must push frames into it. That
something is a WebSocket, and the scan loop is synchronous, so the socket
lives on its own thread with its own event loop and the scan loop only ever
reads the newest value the thread has stored.

Without this module the RTI work is inert. ``SpotPriceClient`` refuses to
price an ``kalshi_rti`` family when no feed is configured — correctly, since
the alternative is substituting the spot price the exchange states does not
settle these markets — so merging the pricing change without a runner would
silently stop the bot pricing crypto at all.

Failure posture
---------------
The runner never blocks the scan loop and never raises into it. A socket that
will not connect, a channel the account cannot subscribe to, a feed that goes
quiet — all of them end the same way: the feed holds no fresh value, and the
quant path declines. That is the safe outcome, but it is also a *silent* one,
so the runner tracks enough state (``last_error``, ``connects``, ``is_ready``)
for startup and health checks to say out loud that crypto is unpriced and
why.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Optional

from config import CONFIG
from core.kalshi_ws import KalshiWebSocket
from core.rti_client import CHANNEL, INDEX_FOR_SYMBOL, RTIFeed, subscribe_command

log = logging.getLogger("daemon_kalshi.rti_runner")

#: Reverse of INDEX_FOR_SYMBOL, so an arriving frame can be attributed to the
#: symbol whose volatility history it belongs in.
SYMBOL_FOR_INDEX: dict[str, str] = {v: k for k, v in INDEX_FOR_SYMBOL.items()}

#: Envelope types Kalshi replies with that are not index values.
_ACK_TYPES = ("subscribed", "ok")
_ERROR_TYPES = ("error", "unsubscribed")


class RTIFeedRunner:
    """Keeps an :class:`RTIFeed` fed from Kalshi's ``cfbenchmarks_value``.

    One daemon thread, one event loop, reconnect with backoff. Start it once
    at boot and read ``runner.feed`` from the scan loop; the two threads share
    only the feed, whose accessors are already locked.
    """

    def __init__(self, feed: RTIFeed = None, cfg=None, index_ids: list[str] = None,
                 ws_factory=None, on_quote=None):
        self.feed = feed or RTIFeed()
        #: Called with (symbol, price, observed_at) for every accepted frame.
        #: Wired to SpotPriceClient.record_tick so the volatility estimate is
        #: built from the index's own tick stream rather than from one sample
        #: per scan pass — see that method for why that mattered.
        self.on_quote = on_quote
        self.ticks_recorded = 0
        self.cfg = cfg or CONFIG.kalshi
        #: Every index any configured family settles against. Subscribing to
        #: all of them once is cheaper than tracking which are in play, and
        #: an index nothing asks for costs one idle stream.
        self.index_ids = list(index_ids or sorted(set(INDEX_FOR_SYMBOL.values())))
        #: Injectable for tests; production builds a real signed client.
        self._ws_factory = ws_factory or (lambda: KalshiWebSocket(self.cfg))

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping = threading.Event()

        self.connects = 0
        self.last_error: Optional[str] = None
        self.last_frame_at: Optional[float] = None
        self.started_at: Optional[float] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self.started_at = time.time()
        self._thread = threading.Thread(
            target=self._thread_main, name="rti-feed", daemon=True,
        )
        self._thread.start()
        log.info("RTI feed thread started, subscribing to %s on %s",
                 ", ".join(self.index_ids), self.cfg.env)

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the thread to finish. Never raises — this runs in a `finally`.

        Daemon thread, so a socket that refuses to close cannot hold the
        process open; the timeout only bounds how long shutdown waits to be
        tidy about it.
        """
        self._stopping.set()
        loop, thread = self._loop, self._thread
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass                      # already closed
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    # -- health ------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        """True when at least one index has a value fresh enough to price on.

        This is the question the rest of the system actually asks: not "is the
        socket up" but "do we know what these contracts settle against right
        now". A connected socket that has sent nothing is not ready.
        """
        return self.feed.is_healthy

    def status(self) -> str:
        """One line for logs and alerts, saying why crypto is or is not priced."""
        if self.is_ready:
            return (
                f"RTI feed live: {self.feed.frames_applied} frame(s) applied "
                f"across {len(self.index_ids)} index/indices"
            )
        detail = self.last_error or "no frames received yet"
        return (
            f"RTI feed NOT live ({detail}) — crypto families settle on the CF "
            f"Benchmarks index and will not be priced until it is. This "
            f"suppresses trades; it does not risk any."
        )

    # -- the socket --------------------------------------------------------

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._supervise())
        except Exception:                       # pragma: no cover - defensive
            # Nothing above this frame can catch it: this is the top of a
            # thread. Losing the feed must not lose the process.
            log.exception("RTI feed thread died — crypto will not be priced")
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    async def _supervise(self) -> None:
        """Reconnect with backoff until told to stop.

        Backoff is capped well below the spot-age limit so a flapping socket
        still recovers inside the window where its values would be usable.
        """
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("RTI feed dropped (%s) — reconnecting in %.1fs",
                            self.last_error, backoff)
            if self._stopping.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        client = self._ws_factory()
        await client.connect()
        self.connects += 1
        await client.send(subscribe_command(self.index_ids, cmd_id=self.connects))
        log.info("Subscribed to %s for %s", CHANNEL, ", ".join(self.index_ids))
        try:
            async for raw in client.messages():
                if self._stopping.is_set():
                    break
                self._handle(raw)
        finally:
            await client.close()

    def _handle(self, raw) -> None:
        try:
            envelope = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            self.feed.frames_rejected += 1
            return
        if not isinstance(envelope, dict):
            self.feed.frames_rejected += 1
            return

        mtype = envelope.get("type")
        if mtype in _ERROR_TYPES:
            # The most likely one in practice: an account or environment that
            # is not entitled to this channel. Worth saying plainly, because
            # the symptom otherwise is just "crypto never trades".
            self.last_error = f"{mtype}: {envelope.get('msg') or envelope}"
            log.error("Kalshi refused the %s subscription — %s", CHANNEL,
                      self.last_error)
            return
        if mtype in _ACK_TYPES:
            log.info("Kalshi acknowledged the %s subscription", CHANNEL)
            return

        # Values arrive wrapped in the standard envelope; tolerate a bare
        # frame so a shape change does not silently drop every quote.
        payload = envelope.get("msg")
        if not isinstance(payload, dict):
            payload = envelope
        quote = self.feed.apply_frame(payload)
        if quote is None:
            return
        self.last_frame_at = time.time()
        self.last_error = None
        self._record(quote)
        log.debug("RTI %s = %.2f%s", quote.index_id, quote.value,
                  "" if quote.windowed_15min is None
                  else f" (15m window {quote.windowed_15min:.2f})")

    def _record(self, quote) -> None:
        """Hand the observation to the volatility history, if one is wired.

        Contained: a failure here is a lost data point, not a lost feed. The
        socket must keep running whatever the consumer does with a tick.
        """
        if self.on_quote is None:
            return
        symbol = SYMBOL_FOR_INDEX.get(quote.index_id)
        if symbol is None:
            return
        try:
            if self.on_quote(symbol, quote.value, quote.received_at):
                self.ticks_recorded += 1
        except Exception:
            log.exception("Recording an RTI tick failed — the feed continues")
