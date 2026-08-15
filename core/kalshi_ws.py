"""
Kalshi WebSocket client. Handles the auth handshake (RSA-PSS signature over
the WS path itself, method GET, no query string), subscribe/unsubscribe,
and — critically — orderbook sequence tracking: if a seq number is skipped,
the local book is stale and must be dropped and re-snapshotted rather than
patched with a guess. Not tested live from this environment; verify against
the demo host before trusting it for real execution decisions.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Callable, Optional

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import CONFIG

log = logging.getLogger("daemon_kalshi.ws")

WS_PATH = "/trade-api/ws/v2"


class OrderbookState:
    """Tracks one market's book via snapshot + ordered deltas."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.yes: dict[str, float] = {}
        self.no: dict[str, float] = {}
        self.seq: Optional[int] = None
        self.stale = True

    def apply_snapshot(self, msg: dict):
        self.yes = {lvl["price"]: lvl["size"] for lvl in msg.get("yes", [])}
        self.no = {lvl["price"]: lvl["size"] for lvl in msg.get("no", [])}
        self.seq = msg.get("seq")
        self.stale = False

    def apply_delta(self, msg: dict) -> bool:
        """Returns False if a seq gap was detected (caller must resubscribe)."""
        incoming_seq = msg.get("seq")
        if self.seq is None or incoming_seq is None or incoming_seq != self.seq + 1:
            self.stale = True
            return False
        side = self.yes if msg.get("side") == "yes" else self.no
        price, delta = msg.get("price"), msg.get("delta", 0)
        side[price] = side.get(price, 0) + delta
        if side[price] <= 0:
            side.pop(price, None)
        self.seq = incoming_seq
        return True

    @property
    def best_yes_bid(self) -> Optional[str]:
        return max(self.yes, key=lambda p: float(p)) if self.yes else None

    @property
    def best_no_bid(self) -> Optional[str]:
        return max(self.no, key=lambda p: float(p)) if self.no else None


class KalshiWebSocket:
    def __init__(self, cfg=None, on_message: Optional[Callable[[dict], None]] = None):
        self.cfg = cfg or CONFIG.kalshi
        self._private_key = serialization.load_pem_private_key(
            self.cfg.load_private_key_bytes(), password=None
        )
        self.on_message = on_message
        self.books: dict[str, OrderbookState] = {}
        self._ws = None
        self._cmd_id = 0

    def _sign_ws_auth(self) -> dict:
        ts_ms = int(time.time() * 1000)
        message = f"{ts_ms}GET{WS_PATH}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }

    async def connect(self):
        headers = self._sign_ws_auth()
        self._ws = await websockets.connect(
            self.cfg.ws_base, additional_headers=headers, ping_interval=None
        )
        log.info("Connected to Kalshi WS (%s)", self.cfg.env)

    async def subscribe(self, channels: list[str], tickers: Optional[list[str]] = None):
        self._cmd_id += 1
        cmd = {
            "id": self._cmd_id,
            "cmd": "subscribe",
            "params": {"channels": channels},
        }
        if tickers:
            cmd["params"]["market_tickers"] = tickers
        await self._ws.send(json.dumps(cmd))

    async def run(self, channels: list[str], tickers: Optional[list[str]] = None):
        """Main receive loop. Kalshi sends a Ping control frame ~every 10s;
        the websockets library answers Pong automatically — no app-level
        keepalive needed."""
        await self.connect()
        await self.subscribe(channels, tickers)
        async for raw in self._ws:
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "orderbook_snapshot":
                ticker = msg["msg"]["market_ticker"]
                book = self.books.setdefault(ticker, OrderbookState(ticker))
                book.apply_snapshot(msg["msg"])

            elif mtype == "orderbook_delta":
                ticker = msg["msg"]["market_ticker"]
                book = self.books.setdefault(ticker, OrderbookState(ticker))
                ok = book.apply_delta(msg["msg"])
                if not ok:
                    log.warning("Seq gap on %s — resubscribing for fresh snapshot", ticker)
                    await self.subscribe(["orderbook_delta"], [ticker])

            if self.on_message:
                self.on_message(msg)

    async def close(self):
        if self._ws:
            await self._ws.close()


async def run_forever(cfg, channels: list[str], tickers: list[str], on_message=None):
    """Reconnect-with-backoff wrapper for production use."""
    backoff = 1.0
    while True:
        client = KalshiWebSocket(cfg, on_message=on_message)
        try:
            await client.run(channels, tickers)
            backoff = 1.0
        except (websockets.ConnectionClosed, OSError) as e:
            log.warning("WS dropped (%s), reconnecting in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
