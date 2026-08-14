"""
Hand-rolled Kalshi REST client. RSA-PSS request signing per Kalshi's
documented spec:

  message = f"{timestamp_ms}{METHOD}{path}"   # path excludes query string,
                                                # includes /trade-api/v2 prefix
  signature = base64(RSA_PSS_SHA256_sign(private_key, message))
  headers = {
      "KALSHI-ACCESS-KEY": api_key_id,
      "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
      "KALSHI-ACCESS-SIGNATURE": signature,
  }

Public endpoints (markets, events, series, orderbooks) don't require these
headers but accept them. Everything under /portfolio requires them.

This has NOT been run against a live Kalshi endpoint from this environment
(no network access here) — verify against demo-api.kalshi.co before trusting
it with production keys.
"""
from __future__ import annotations

import base64
import time
import logging
from typing import Any, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import CONFIG

log = logging.getLogger("daemon_kalshi.client")


class KalshiAPIError(Exception):
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Kalshi API error {status_code}: {body}")

    @property
    def is_client_error(self) -> bool:
        """4xx: the exchange understood us and refused. Retrying the same
        request will be refused the same way, so callers treat this as a
        terminal rejection rather than something to back off on."""
        return 400 <= self.status_code < 500


class RateLimitError(KalshiAPIError):
    pass


class KalshiTimeoutError(Exception):
    """The request did not come back with a usable answer.

    Distinct from :class:`KalshiAPIError` because the failure modes are
    opposite: an API error tells us the exchange's state (it rejected us), a
    timeout tells us nothing — the order may be live, may never have arrived.
    Order submission turns this into the ``unknown`` state and refuses to
    trade again until a lookup by client order ID settles the question. Never
    blind-retry a write on this.
    """

    def __init__(self, method: str, endpoint: str, cause: Exception):
        self.method = method
        self.endpoint = endpoint
        self.cause = cause
        super().__init__(f"Kalshi request {method} {endpoint} did not complete: {cause!r}")


class KalshiClient:
    def __init__(self, cfg=None, timeout: float = 10.0):
        self.cfg = cfg or CONFIG.kalshi
        self._private_key = serialization.load_pem_private_key(
            self.cfg.load_private_key_bytes(), password=None
        )
        self._http = httpx.Client(base_url=self.cfg.rest_base, timeout=timeout)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- signing -----------------------------------------------------------

    def _sign(self, method: str, path_with_prefix: str, ts_ms: int) -> str:
        message = f"{ts_ms}{method}{path_with_prefix}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> dict:
        # path must be the full path from API root, e.g. "/trade-api/v2/portfolio/orders"
        # WITHOUT query params, even for GET requests with a querystring.
        ts_ms = int(time.time() * 1000)
        sig = self._sign(method.upper(), path, ts_ms)
        return {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    # -- core request with retry/backoff ------------------------------------

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        max_retries: int = 3,
    ) -> dict:
        full_path = f"/trade-api/v2{endpoint}"
        headers = self._auth_headers(method, full_path)

        backoff = 1.0
        for attempt in range(max_retries + 1):
            try:
                resp = self._http.request(
                    method, endpoint, params=params, json=json_body, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                # Deliberately not retried here. For reads the caller can retry
                # safely; for POST /portfolio/orders a retry would be a second
                # submission attempt against an exchange that may already hold
                # the first one. Execution resolves it by lookup instead.
                raise KalshiTimeoutError(method, endpoint, e) from e
            if resp.status_code == 429:
                if attempt == max_retries:
                    raise RateLimitError(429, resp.text)
                log.warning("429 from Kalshi, backing off %.1fs", backoff)
                time.sleep(backoff)
                backoff *= 2
                # re-sign: timestamp must be fresh on retry
                headers = self._auth_headers(method, full_path)
                continue
            if resp.status_code >= 400:
                raise KalshiAPIError(resp.status_code, resp.text)
            return resp.json() if resp.content else {}
        raise KalshiAPIError(500, "exhausted retries")

    # -- public market data --------------------------------------------------

    def get_exchange_status(self) -> dict:
        return self._request("GET", "/exchange/status")

    def list_events(
        self, status: str = "open", limit: int = 200, cursor: str = None,
        with_nested_markets: bool = False,
    ) -> dict:
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        return self._request("GET", "/events", params=params)

    def list_categories(self) -> dict:
        """Real category -> tags mapping straight from Kalshi, e.g.
        {"Politics": ["elections", ...], "Sports": [...], "Economics": [...]}.
        Use this instead of guessing category from ticker prefixes."""
        return self._request("GET", "/search/tags_by_categories")

    def list_markets(
        self, series_ticker: str = None, status: str = "open", limit: int = 200, cursor: str = None
    ) -> dict:
        params = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # -- portfolio / trading (requires auth) ---------------------------------

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self, settlement_status: str = "unsettled") -> dict:
        return self._request(
            "GET", "/portfolio/positions", params={"settlement_status": settlement_status}
        )

    def get_fills(
        self, ticker: str = None, order_id: str = None, limit: int = 100
    ) -> dict:
        """Exchange-confirmed executions. This is the only source of truth for
        exposure — order acknowledgements are not fills."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        return self._request("GET", "/portfolio/fills", params=params)

    def get_orders(
        self,
        ticker: str = None,
        status: str = None,
        client_order_id: str = None,
        limit: int = 200,
        cursor: str = None,
    ) -> dict:
        """List orders. ``client_order_id`` is the lookup that resolves an
        ``unknown`` order after a submission timeout: it asks the exchange
        whether it holds the order we may or may not have sent."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if client_order_id:
            params["client_order_id"] = client_order_id
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/portfolio/orders", params=params)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/portfolio/orders/{order_id}")

    def get_settlements(self, limit: int = 200, cursor: str = None) -> dict:
        """Exchange-confirmed market settlements — the authoritative outcome,
        replacing the old inference from PnL sign or resting-order counts."""
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/portfolio/settlements", params=params)

    def get_account_limits(self) -> dict:
        return self._request("GET", "/account/limits")

    def place_order(
        self,
        ticker: str,
        action: str,          # "buy" | "sell"
        side: str,             # "yes" | "no"
        count: int,
        order_type: str = "limit",
        yes_price_dollars: Optional[str] = None,
        no_price_dollars: Optional[str] = None,
        client_order_id: Optional[str] = None,
        time_in_force: str = "GTC",
        post_only: Optional[bool] = None,
    ) -> dict:
        """
        Places an order. Prices are dollar-denominated strings per current
        Kalshi API (integer-cent fields were removed March 2026). Set
        client_order_id for idempotent retries — always set it in production.

        time_in_force: confirmed real values from Kalshi's own order-panel
        docs are GTC (default, rests on the book), IOC (immediate-or-cancel,
        fills what it can right now and cancels the rest), EOD, and
        event-scoped/custom expiries. An earlier version of this method
        defaulted to "fill_or_kill", which isn't in that vocabulary and was
        never verified against Kalshi's actual API reference — fixed here,
        but double-check the exact accepted string values in Kalshi's API
        docs before trusting this with size, since I'm inferring from their
        UI documentation, not the literal request schema.

        post_only: Kalshi's UI exposes a "resting order only" toggle that
        blocks a limit order from crossing the spread even accidentally —
        the maker-side counterpart to IOC's taker behavior. I don't have the
        exact API field name confirmed (may not be `post_only` literally) —
        verify against Kalshi's API reference before relying on it to
        guarantee maker-side execution.
        """
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
        }
        if yes_price_dollars is not None:
            body["yes_price_dollars"] = yes_price_dollars
        if no_price_dollars is not None:
            body["no_price_dollars"] = no_price_dollars
        if client_order_id:
            body["client_order_id"] = client_order_id
        if post_only is not None:
            body["post_only"] = post_only
        if order_type == "limit":
            body["time_in_force"] = time_in_force
        return self._request("POST", "/portfolio/orders", json_body=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}")

    def batch_place_orders(self, orders: list[dict]) -> dict:
        # Each order in the batch still costs its own rate-limit token —
        # batching saves round trips, not rate-limit budget.
        return self._request("POST", "/portfolio/events/orders/batched", json_body={"orders": orders})
