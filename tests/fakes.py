"""
In-memory stand-in for Kalshi.

Modelled on the response shapes in core/kalshi_client.py. The client has never
been run against a live endpoint from this repo, so these fakes encode what
the code *believes* Kalshi returns — they prove the bot's own logic is
correct, not that the wire format is right. Confirm the shapes against
demo-api.kalshi.co before trusting any of this with a production key.
"""
from __future__ import annotations

import itertools

from core.kalshi_client import KalshiAPIError, KalshiTimeoutError


class FakeKalshiClient:
    """Configurable exchange double.

    Fill behaviour is driven by ``fill_plan``: a per-call count of how many
    contracts the next order fills. That is what lets one test express "IOC
    that fills zero" and another "IOC that fills 3 of 10" without branching
    inside the client.
    """

    def __init__(self, balance_cents: float = 100_000.0):
        self.balance_cents = balance_cents
        self.available_balance_cents = balance_cents
        self.market_positions: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.fills: list[dict] = []
        self.settlements: list[dict] = []
        self.markets: dict[str, dict] = {}

        #: Contracts filled by each successive place_order call. None = fill
        #: the full requested quantity.
        self.fill_plan: list[int] = []
        #: Price (cents) the fake fills at; None = the order's limit price.
        self.fill_price_cents: float | None = None
        self.fee_per_contract_cents: float = 0.0

        #: Raise this from the next place_order call. The order is still
        #: registered first when ``accept_before_raising`` is set, which is
        #: how "exchange accepted it but we never got the response" is
        #: reproduced.
        self.raise_on_place: Exception | None = None
        self.accept_before_raising: bool = False

        self.place_order_calls: list[dict] = []
        self.cancelled: list[str] = []
        self._ids = itertools.count(1)
        self.call_counts: dict[str, int] = {}

    # -- helpers used by tests --------------------------------------------

    def add_position(self, ticker, side, quantity, avg_price_cents,
                     fees_cents=0.0, event_ticker=""):
        signed = quantity if side == "yes" else -quantity
        self.market_positions.append({
            "ticker": ticker,
            "position": signed,
            "market_exposure": quantity * avg_price_cents,
            "fees_paid": fees_cents,
            "event_ticker": event_ticker,
        })

    def add_settlement(self, ticker, result, settled_time=1_700_000_000):
        self.settlements.append({
            "ticker": ticker,
            "market_result": result,
            "market_id": f"mkt-{ticker}",
            "settled_time": settled_time,
        })

    # -- endpoints ---------------------------------------------------------

    def _count(self, name):
        self.call_counts[name] = self.call_counts.get(name, 0) + 1

    def get_balance(self):
        self._count("get_balance")
        return {
            "balance": self.balance_cents,
            "available_balance": self.available_balance_cents,
        }

    def get_account_limits(self):
        self._count("get_account_limits")
        return {}

    def get_positions(self, settlement_status="unsettled"):
        self._count("get_positions")
        return {"market_positions": list(self.market_positions)}

    def get_fills(self, ticker=None, order_id=None, limit=100):
        self._count("get_fills")
        out = self.fills
        if ticker:
            out = [f for f in out if f["ticker"] == ticker]
        if order_id:
            out = [f for f in out if f.get("order_id") == order_id]
        return {"fills": list(out)}

    def get_orders(self, ticker=None, status=None, client_order_id=None,
                   limit=200, cursor=None):
        self._count("get_orders")
        out = list(self.orders.values())
        if ticker:
            out = [o for o in out if o["ticker"] == ticker]
        if client_order_id:
            out = [o for o in out if o.get("client_order_id") == client_order_id]
        if status:
            out = [o for o in out if o.get("status") == status]
        return {"orders": out}

    def get_order(self, order_id):
        self._count("get_order")
        if order_id not in self.orders:
            raise KalshiAPIError(404, "order not found")
        return {"order": self.orders[order_id]}

    def get_settlements(self, limit=200, cursor=None):
        self._count("get_settlements")
        return {"settlements": list(self.settlements)}

    def get_market(self, ticker):
        self._count("get_market")
        if ticker not in self.markets:
            raise KalshiAPIError(404, "market not found")
        return {"market": self.markets[ticker]}

    def cancel_order(self, order_id):
        self._count("cancel_order")
        self.cancelled.append(order_id)
        if order_id in self.orders:
            self.orders[order_id]["status"] = "canceled"
        return {"order": self.orders.get(order_id, {})}

    def place_order(self, ticker, action, side, count, order_type="limit",
                    yes_price_dollars=None, no_price_dollars=None,
                    client_order_id=None, time_in_force="GTC", post_only=None):
        self._count("place_order")
        self.place_order_calls.append({
            "ticker": ticker, "action": action, "side": side, "count": count,
            "client_order_id": client_order_id, "time_in_force": time_in_force,
            "yes_price_dollars": yes_price_dollars,
            "no_price_dollars": no_price_dollars,
        })

        # Idempotency: Kalshi treats client_order_id as a dedupe key, so a
        # resend of the same ID returns the original order rather than a new
        # one. The fake enforces that too, otherwise a test could "pass" while
        # the real exchange would have created two orders.
        existing = next(
            (o for o in self.orders.values()
             if o.get("client_order_id") == client_order_id),
            None,
        )
        if existing is not None:
            return {"order": existing}

        price = float(yes_price_dollars or no_price_dollars or 0) * 100
        order = self._register(ticker, side, action, count, price, client_order_id,
                               time_in_force)

        if self.raise_on_place is not None:
            exc = self.raise_on_place
            self.raise_on_place = None
            if not self.accept_before_raising:
                # Never landed: undo the registration so the exchange really
                # does not know about it.
                self.orders.pop(order["order_id"], None)
            raise exc
        return {"order": order}

    def _register(self, ticker, side, action, count, price, client_order_id, tif):
        order_id = f"ord-{next(self._ids)}"
        fill_count = self.fill_plan.pop(0) if self.fill_plan else count
        fill_count = max(0, min(fill_count, count))
        fill_price = self.fill_price_cents if self.fill_price_cents is not None else price

        for _ in range(1 if fill_count else 0):
            trade_id = f"fill-{next(self._ids)}"
            self.fills.append({
                "trade_id": trade_id,
                "ticker": ticker,
                "order_id": order_id,
                "client_order_id": client_order_id,
                "side": side,
                "action": action,
                "count": fill_count,
                "yes_price": fill_price if side == "yes" else None,
                "no_price": fill_price if side == "no" else None,
                "fee_paid": self.fee_per_contract_cents * fill_count,
                "created_time": 1_700_000_000,
            })

        remaining = count - fill_count
        if fill_count >= count:
            status = "executed"
        elif tif.upper() == "IOC":
            # IOC cancels whatever did not fill immediately.
            status = "canceled"
            remaining = 0
        else:
            status = "resting"

        order = {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "ticker": ticker,
            "side": side,
            "action": action,
            "status": status,
            "filled_count": fill_count,
            "remaining_count": remaining,
            "time_in_force": tif,
        }
        self.orders[order_id] = order
        return order


class TimeoutOnce(KalshiTimeoutError):
    """Convenience constructor for a submission timeout."""

    def __init__(self):
        super().__init__("POST", "/portfolio/orders", TimeoutError("read timeout"))
