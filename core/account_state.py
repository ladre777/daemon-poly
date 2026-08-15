"""
Account state: the bot's reconciled view of what it actually owns and owes.

This replaces ``open_positions = 0  # TODO: seed from client.get_positions()``
in main.py — an integer that started at zero on every process start,
incremented on any order response that didn't raise, and never learned about
fills, cancellations, partial fills or positions opened by a previous run.
On a platform that redeploys on every push, that counter was reset to zero
far more often than it was correct.

Everything here is built from exchange truth (balance, positions, orders,
fills) and cross-checked against locally stored fills. The bot trades only
when this component says it has a fresh, self-consistent picture; every
failure path leaves it refusing to trade rather than guessing.

Exposure convention
-------------------
This bot only ever *buys* binary contracts (YES or NO). A contract bought at
price ``p`` cents can lose at most ``p`` cents — it settles at 0 or 100. So
worst-case loss equals cash committed:

    position worst case = quantity * avg_fill_price + fees
    pending worst case  = outstanding_quantity * limit_price (+ slippage)
    total               = sum of both across all tickers

Selling to close would cap the loss lower, but assuming we can exit is
assuming liquidity that may not be there at settlement time, so the
conservative figure is the one risk enforces against.

Mark-to-market
--------------
Worst-case exposure answers "how much could this lose in total"; it does not
answer "how much has it lost so far", and the daily-loss control needs the
second question. Each open position is therefore also priced against the
current book:

    unrealized = quantity * bid_on_the_side_held - (cost basis + fees)

Marked to the bid rather than the midpoint, for the same reason entries are
priced at the ask: the number that matters is the one someone will actually
transact at. A position whose book cannot be read is marked ``None`` — not
zero — and the exposure sitting behind those positions is reported
separately, so a loss figure with a hole in it is never mistaken for a
complete one.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from config import CONFIG
from core.kalshi_client import KalshiAPIError, KalshiClient, KalshiTimeoutError
from core.order_state import Fill, OrderRecord, OrderState
from core.validation import mark_price_cents
from memory.order_store import OrderStore

log = logging.getLogger("daemon_kalshi.account")


class ReconciliationError(Exception):
    """Account state could not be verified against the exchange.

    Callers must treat this as "do not trade", never as "carry on with the
    last known numbers".
    """


@dataclass
class Position:
    ticker: str
    side: str                       # "yes" | "no"
    quantity: int
    avg_price_cents: float = 0.0
    fees_cents: float = 0.0
    event_ticker: str = ""
    category: str = ""
    #: Current exit price for this side, in cents, or None if the market
    #: quotes nothing usable. None means "unknown", never "worthless".
    mark_price_cents: Optional[float] = None

    @property
    def worst_case_loss_cents(self) -> float:
        return max(self.quantity, 0) * self.avg_price_cents + self.fees_cents

    @property
    def cost_basis_cents(self) -> float:
        """Everything this position has cost so far, fees included."""
        return max(self.quantity, 0) * self.avg_price_cents + self.fees_cents

    @property
    def market_value_cents(self) -> Optional[float]:
        """What the position could be liquidated for now, or None if unknown."""
        if self.mark_price_cents is None:
            return None
        return max(self.quantity, 0) * self.mark_price_cents

    @property
    def unrealized_pnl_cents(self) -> Optional[float]:
        """Mark-to-market gain or loss, or None when the mark is unknown.

        Fees already paid count as the loss they are: that money has left the
        account and no future price recovers it. Excluding them would let a
        position that has merely broken even on price read as flat when it is
        in fact down by the commission.
        """
        value = self.market_value_cents
        if value is None:
            return None
        return value - self.cost_basis_cents


@dataclass
class AccountSnapshot:
    balance_cents: float
    available_balance_cents: float
    positions: list[Position] = field(default_factory=list)
    open_orders: list[OrderRecord] = field(default_factory=list)
    limits: dict = field(default_factory=dict)
    reconciled_at: float = 0.0
    unknown_order_ids: list[str] = field(default_factory=list)
    inconsistencies: list[str] = field(default_factory=list)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.reconciled_at

    def is_stale(self, max_age_seconds: float = None) -> bool:
        max_age = (
            max_age_seconds
            if max_age_seconds is not None
            else CONFIG.risk.max_reconciliation_age_seconds
        )
        return self.age_seconds > max_age

    @property
    def is_tradeable(self) -> bool:
        """Every condition that must hold before any new order is allowed."""
        return (
            not self.is_stale()
            and not self.unknown_order_ids
            and not self.inconsistencies
            and self.reconciled_at > 0
        )

    def blocking_reason(self) -> Optional[str]:
        if self.reconciled_at <= 0:
            return "account state has never been reconciled with Kalshi"
        if self.is_stale():
            return (
                f"account state is stale ({self.age_seconds:.0f}s old, limit "
                f"{CONFIG.risk.max_reconciliation_age_seconds:.0f}s)"
            )
        if self.unknown_order_ids:
            return (
                f"{len(self.unknown_order_ids)} order(s) in unknown state — the "
                f"exchange may hold orders this bot cannot see: "
                f"{', '.join(self.unknown_order_ids[:3])}"
            )
        if self.inconsistencies:
            return f"local state disagrees with exchange: {'; '.join(self.inconsistencies)}"
        return None

    # -- exposure ---------------------------------------------------------

    def position_exposure_cents(self) -> float:
        return sum(p.worst_case_loss_cents for p in self.positions)

    def pending_exposure_cents(self) -> float:
        return sum(o.pending_cost_cents for o in self.open_orders)

    def worst_case_exposure_cents(self) -> float:
        return self.position_exposure_cents() + self.pending_exposure_cents()

    def exposure_by_ticker_cents(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.positions:
            out[p.ticker] = out.get(p.ticker, 0.0) + p.worst_case_loss_cents
        for o in self.open_orders:
            out[o.ticker] = out.get(o.ticker, 0.0) + o.pending_cost_cents
        return out

    def exposure_by_event_cents(self) -> dict[str, float]:
        """Per-event exposure. Markets in one event are usually mutually
        exclusive outcomes of the same question, so several positions there
        are one correlated bet, not several independent ones."""
        out: dict[str, float] = {}
        for p in self.positions:
            key = p.event_ticker or _event_of(p.ticker)
            out[key] = out.get(key, 0.0) + p.worst_case_loss_cents
        for o in self.open_orders:
            key = o.event_ticker or _event_of(o.ticker)
            out[key] = out.get(key, 0.0) + o.pending_cost_cents
        return out

    def exposure_by_category_cents(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.positions:
            if p.category:
                out[p.category] = out.get(p.category, 0.0) + p.worst_case_loss_cents
        for o in self.open_orders:
            if o.category:
                out[o.category] = out.get(o.category, 0.0) + o.pending_cost_cents
        return out

    def open_position_count(self) -> int:
        return len([p for p in self.positions if p.quantity > 0])

    # -- mark-to-market ----------------------------------------------------

    def unrealized_pnl_cents(self) -> float:
        """Mark-to-market PnL across every position we can currently price.

        Positions without a mark contribute nothing here, which is why
        ``unmarked_exposure_cents`` exists alongside it: a caller that reads
        this number without also asking how much of the book it covers is
        reading a loss figure with an unknown hole in it.
        """
        return sum(
            pnl
            for pnl in (p.unrealized_pnl_cents for p in self.positions)
            if pnl is not None
        )

    def unmarked_positions(self) -> list[Position]:
        """Open positions whose current price could not be determined."""
        return [p for p in self.positions if p.quantity > 0 and p.mark_price_cents is None]

    def unmarked_exposure_cents(self) -> float:
        """Cost basis sitting behind positions we could not mark.

        This bounds the error in ``unrealized_pnl_cents``: the unknown loss
        cannot exceed it, because a contract cannot fall below zero.
        """
        return sum(p.cost_basis_cents for p in self.unmarked_positions())

    def is_fully_marked(self) -> bool:
        return not self.unmarked_positions()


def _event_of(ticker: str) -> str:
    """Kalshi market tickers are ``EVENT-SUBMARKET``; the event is everything
    before the last dash. Falls back to the whole ticker when there is no
    dash, which just means the market is its own event for exposure purposes."""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


class AccountState:
    """Owns reconciliation with Kalshi and hands out immutable snapshots."""

    def __init__(self, client: KalshiClient = None, store: OrderStore = None):
        self.client = client or KalshiClient()
        self.store = store or OrderStore()
        self._snapshot: Optional[AccountSnapshot] = AccountSnapshot(
            balance_cents=0.0, available_balance_cents=0.0, reconciled_at=0.0
        )

    @property
    def snapshot(self) -> AccountSnapshot:
        return self._snapshot

    def require_tradeable(self) -> AccountSnapshot:
        """Fail closed. Returns the snapshot only if it is safe to trade on."""
        snap = self._snapshot
        if snap is None or not snap.is_tradeable:
            reason = snap.blocking_reason() if snap else "no account snapshot"
            raise ReconciliationError(f"refusing to trade: {reason}")
        return snap

    # -- reconciliation ----------------------------------------------------

    def reconcile(self) -> AccountSnapshot:
        """Rebuild the account picture from the exchange.

        Order matters: resolve unknown orders *first*, because an unresolved
        one means the position and order lists we are about to read may be
        missing something the exchange has already accepted. Then balance,
        positions, open orders and fills.

        Raises ReconciliationError on any failure — the caller must not fall
        back to stale numbers.
        """
        try:
            unknown_ids = self._resolve_unknown_orders()
            balance_cents, available_cents = self._fetch_balance()
            limits = self._fetch_limits()
            exchange_positions = self._fetch_positions()
            open_orders = self._sync_open_orders()
            self._sync_recent_fills()
        except KalshiTimeoutError as e:
            raise ReconciliationError(f"Kalshi did not respond: {e}") from e
        except KalshiAPIError as e:
            raise ReconciliationError(f"Kalshi rejected a reconciliation call: {e}") from e

        inconsistencies = self._compare_local_to_exchange(exchange_positions)

        snap = AccountSnapshot(
            balance_cents=balance_cents,
            available_balance_cents=available_cents,
            positions=exchange_positions,
            open_orders=open_orders,
            limits=limits,
            reconciled_at=time.time(),
            unknown_order_ids=unknown_ids,
            inconsistencies=inconsistencies,
        )
        self._snapshot = snap
        self.store.save_account_snapshot(
            balance_cents=balance_cents,
            available_balance_cents=available_cents,
            limits=limits,
            positions=[
                {
                    "ticker": p.ticker, "side": p.side, "quantity": p.quantity,
                    "avg_price_cents": p.avg_price_cents, "fees_cents": p.fees_cents,
                }
                for p in exchange_positions
            ],
            reconciled_at=snap.reconciled_at,
        )
        log.info(
            "Reconciled: balance $%.2f | %d position(s) worth $%.2f worst case | "
            "%d live order(s) reserving $%.2f | %d unknown | %d inconsistency(ies)",
            balance_cents / 100, len(exchange_positions),
            snap.position_exposure_cents() / 100, len(open_orders),
            snap.pending_exposure_cents() / 100, len(unknown_ids), len(inconsistencies),
        )
        if inconsistencies:
            for msg in inconsistencies:
                log.error("Reconciliation inconsistency: %s", msg)
        return snap

    def _fetch_balance(self) -> tuple[float, float]:
        data = self.client.get_balance() or {}
        balance = data.get("balance")
        if balance is None:
            raise ReconciliationError(
                f"balance response had no 'balance' field: {list(data)[:6]}"
            )
        # Kalshi reports balance in cents. available_balance is what is left
        # after resting orders reserve collateral; when absent, fall back to
        # the total (the pending-order term in exposure covers the gap).
        available = data.get("available_balance", balance)
        return float(balance), float(available)

    def _fetch_limits(self) -> dict:
        try:
            return self.client.get_account_limits() or {}
        except KalshiAPIError as e:
            # Limits are advisory next to our own caps, and demo accounts do
            # not always expose the endpoint. A 4xx here should not stop
            # trading; anything else is a real reconciliation failure.
            if e.is_client_error:
                log.warning("Account limits unavailable (%s) — continuing without them", e)
                return {}
            raise

    def _fetch_positions(self) -> list[Position]:
        data = self.client.get_positions(settlement_status="unsettled") or {}
        positions: list[Position] = []
        for row in data.get("market_positions", []):
            ticker = row.get("ticker")
            if not ticker:
                continue
            qty = int(row.get("position", 0))
            if qty == 0:
                continue
            # Kalshi signs `position`: positive is long YES, negative long NO.
            side = "yes" if qty > 0 else "no"
            quantity = abs(qty)
            exposure_cents = abs(float(row.get("market_exposure", 0)))
            avg_price = exposure_cents / quantity if quantity else 0.0
            positions.append(
                Position(
                    ticker=ticker,
                    side=side,
                    quantity=quantity,
                    avg_price_cents=avg_price,
                    fees_cents=abs(float(row.get("fees_paid", 0))),
                    event_ticker=row.get("event_ticker", ""),
                )
            )
        self._attach_metadata(positions)
        self._attach_marks(positions)
        return positions

    def _attach_marks(self, positions: list[Position]) -> None:
        """Price every open position against the current book.

        One request per position. That is bounded by how many positions the
        bot actually holds — single digits under the concentration caps — not
        by the thousands of markets Scout scans.

        A market that cannot be fetched or cannot be priced leaves the mark
        as None and does not abort reconciliation. Refusing to trade at all
        because one position's book is momentarily unreadable would convert a
        pricing gap into an outage; the gap is instead reported, and callers
        decide what to do about not knowing.
        """
        for p in positions:
            if p.quantity <= 0:
                continue
            try:
                payload = self.client.get_market(p.ticker) or {}
            except (KalshiAPIError, KalshiTimeoutError) as e:
                log.warning("Couldn't price open position %s: %s", p.ticker, e)
                continue
            market = payload.get("market", payload)
            if not isinstance(market, dict):
                continue
            p.mark_price_cents = mark_price_cents(market, p.side)
            if p.mark_price_cents is None:
                log.warning(
                    "No usable %s bid for open position %s — its unrealized PnL "
                    "is unknown, not zero", p.side.upper(), p.ticker,
                )

    def _attach_metadata(self, positions: list[Position]) -> None:
        """Fill in event/category from our own order history where the
        positions endpoint doesn't carry them, so per-event and per-category
        caps still apply to positions opened by an earlier run."""
        known: dict[str, tuple[str, str]] = {}
        for order in self.store.live_orders():
            known.setdefault(order.ticker, (order.event_ticker, order.category))
        for p in positions:
            event, category = known.get(p.ticker, ("", ""))
            p.event_ticker = p.event_ticker or event or _event_of(p.ticker)
            p.category = p.category or category

    def _sync_open_orders(self) -> list[OrderRecord]:
        """Refresh every locally live order against the exchange.

        An order we think is open but the exchange has never heard of is a
        genuine inconsistency, not something to quietly drop.
        """
        refreshed: list[OrderRecord] = []
        for record in self.store.live_orders():
            if record.state is OrderState.UNKNOWN:
                continue  # handled by _resolve_unknown_orders
            if record.dry_run:
                continue
            updated = self.refresh_order(record)
            if updated.is_live:
                refreshed.append(updated)
        return refreshed

    def _sync_recent_fills(self) -> int:
        """Pull recent fills and store them. Idempotent on ``fill_id``."""
        data = self.client.get_fills(limit=200) or {}
        fills = [_parse_fill(f) for f in data.get("fills", [])]
        fills = [f for f in fills if f is not None]
        new = self.store.record_fills(fills)
        if new:
            log.info("Recorded %d new fill(s) from Kalshi", new)
        return new

    def _resolve_unknown_orders(self) -> list[str]:
        """Ask the exchange about every order whose fate we don't know.

        Returns the client order IDs still unresolved. A non-empty list means
        trading stays blocked: we cannot compute exposure while the exchange
        might hold an order we can't see.
        """
        unresolved: list[str] = []
        for record in self.store.unknown_orders():
            try:
                resolved = self._lookup_by_client_order_id(record)
            except (KalshiAPIError, KalshiTimeoutError) as e:
                log.error(
                    "Could not resolve unknown order %s: %s", record.client_order_id, e
                )
                unresolved.append(record.client_order_id)
                continue
            if resolved is None:
                # The exchange has no such order. Since the client order ID was
                # persisted before the request went out, "not found" now means
                # it never landed — safe to mark rejected rather than leave it
                # blocking forever.
                record.state = OrderState.REJECTED
                record.last_error = "not found on exchange after timeout — never accepted"
                record.remaining_count = 0
                record.terminal_at = time.time()
                record.last_reconciled_at = time.time()
                self.store.update_order(record)
                log.warning(
                    "Unknown order %s was never accepted by Kalshi — marked rejected",
                    record.client_order_id,
                )
            else:
                log.warning(
                    "Unknown order %s WAS accepted by Kalshi (%s) — recovered as %s",
                    record.client_order_id, resolved.exchange_order_id,
                    resolved.state.value,
                )
        return unresolved

    def _lookup_by_client_order_id(self, record: OrderRecord) -> Optional[OrderRecord]:
        data = self.client.get_orders(client_order_id=record.client_order_id) or {}
        orders = data.get("orders", [])
        match = next(
            (
                o for o in orders
                if o.get("client_order_id") == record.client_order_id
            ),
            None,
        )
        if match is None:
            return None
        return self.apply_exchange_order(record, match)

    # -- order refresh -----------------------------------------------------

    def refresh_order(self, record: OrderRecord) -> OrderRecord:
        """Re-read one order and its fills from the exchange and persist."""
        payload = None
        if record.exchange_order_id:
            try:
                resp = self.client.get_order(record.exchange_order_id) or {}
                payload = resp.get("order", resp)
            except KalshiAPIError as e:
                if not e.is_client_error:
                    raise
                log.warning("Order %s not readable: %s", record.exchange_order_id, e)
        if payload is None:
            data = self.client.get_orders(client_order_id=record.client_order_id) or {}
            payload = next(
                (
                    o for o in data.get("orders", [])
                    if o.get("client_order_id") == record.client_order_id
                ),
                None,
            )
        if payload is None:
            record.last_reconciled_at = time.time()
            record.last_error = "order not found on exchange"
            self.store.update_order(record)
            return record
        return self.apply_exchange_order(record, payload)

    def apply_exchange_order(self, record: OrderRecord, payload: dict) -> OrderRecord:
        """Merge an exchange order payload into the local record.

        Fill quantity comes from the fills endpoint, not from the order
        payload, because fills are the settlement-grade record and carry the
        fee and price detail the order summary can omit.
        """
        record.exchange_order_id = payload.get("order_id") or record.exchange_order_id
        if record.exchange_order_id:
            fills = self._fetch_fills_for_order(record)
            if fills:
                self.store.record_fills(fills)
        stored_fills = self.store.fills_for_order(record.client_order_id)
        filled = sum(f.count for f in stored_fills)
        if filled:
            gross = sum(f.count * f.price_cents for f in stored_fills)
            record.avg_fill_price_cents = gross / filled
            record.fees_cents = sum(f.fees_cents for f in stored_fills)
        record.filled_count = filled

        # Prefer the exchange's own counts when it reports them; they include
        # quantity filled through paths our fills query may not have caught yet.
        exch_filled = _int_or_none(payload.get("filled_count"))
        if exch_filled is not None and exch_filled > record.filled_count:
            record.filled_count = exch_filled
        record.remaining_count = max(record.requested_count - record.filled_count, 0)
        exch_remaining = _int_or_none(payload.get("remaining_count"))
        if exch_remaining is not None:
            record.remaining_count = exch_remaining

        status = (payload.get("status") or "").lower()
        record.state = _state_from_status(status, record)
        record.last_reconciled_at = time.time()
        if record.is_terminal and record.terminal_at is None:
            record.terminal_at = time.time()
        self.store.update_order(record)
        return record

    def _fetch_fills_for_order(self, record: OrderRecord) -> list[Fill]:
        data = self.client.get_fills(order_id=record.exchange_order_id, limit=200) or {}
        out = []
        for raw in data.get("fills", []):
            fill = _parse_fill(raw)
            if fill is None:
                continue
            # The fills endpoint may omit the client order ID; attach ours so
            # per-order aggregation and settlement matching both work.
            if not fill.client_order_id:
                fill = Fill(
                    fill_id=fill.fill_id, ticker=fill.ticker, count=fill.count,
                    price_cents=fill.price_cents, side=fill.side, action=fill.action,
                    fees_cents=fill.fees_cents,
                    exchange_order_id=fill.exchange_order_id or record.exchange_order_id,
                    client_order_id=record.client_order_id, created_at=fill.created_at,
                )
            out.append(fill)
        return out

    # -- consistency -------------------------------------------------------

    def _compare_local_to_exchange(self, exchange_positions: list[Position]) -> list[str]:
        """Cross-check locally reconstructed positions against the exchange.

        The safety brief's requirement is that reconstructed exposure matches
        exchange state after a restart. A mismatch is reported rather than
        silently overwritten: exchange numbers win for trading decisions, but
        a divergence means something is wrong with our fill record and the bot
        should stop until a human looks.
        """
        problems: list[str] = []
        local = self.store.position_from_fills()
        exchange = {(p.ticker, p.side): p for p in exchange_positions}

        for key, pos in exchange.items():
            local_qty = local.get(key, {}).get("quantity", 0)
            if local_qty != pos.quantity:
                problems.append(
                    f"{pos.ticker}/{pos.side}: exchange says {pos.quantity} contracts, "
                    f"local fills reconstruct {local_qty}"
                )
        for key, row in local.items():
            if row["quantity"] > 0 and key not in exchange:
                problems.append(
                    f"{key[0]}/{key[1]}: local fills show {row['quantity']} contracts, "
                    f"exchange reports no position"
                )
        if CONFIG.risk.allow_position_drift:
            # Escape hatch for the first run against an account that already
            # had positions from manual trading — logs loudly, does not block.
            for msg in problems:
                log.warning("Ignoring position drift (ALLOW_POSITION_DRIFT=true): %s", msg)
            return []
        return problems


def _state_from_status(status: str, record: OrderRecord) -> OrderState:
    """Map Kalshi's order status onto the local lifecycle.

    Kalshi's exact status vocabulary is not fully pinned down in this repo
    (see the client's module docstring), so unrecognised statuses fall back to
    inferring from quantities rather than assuming the optimistic case.
    """
    filled, requested = record.filled_count, record.requested_count
    if status in ("rejected",):
        return OrderState.REJECTED
    if status in ("executed", "filled") or (requested and filled >= requested):
        return OrderState.FILLED
    if status in ("canceled", "cancelled"):
        return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.CANCELLED
    if status in ("expired",):
        return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.EXPIRED
    if status in ("resting", "open", "pending"):
        return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.OPEN
    # Unrecognised status: an IOC order gets exactly one shot, so anything not
    # fully filled is done. A GTC order might still be working.
    if record.time_in_force.upper() == "IOC":
        if filled >= requested and requested:
            return OrderState.FILLED
        return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.EXPIRED
    return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.OPEN


def _parse_fill(raw: dict) -> Optional[Fill]:
    fill_id = raw.get("trade_id") or raw.get("fill_id") or raw.get("id")
    ticker = raw.get("ticker")
    if not fill_id or not ticker:
        return None
    count = _int_or_none(raw.get("count")) or 0
    if count <= 0:
        return None
    side = (raw.get("side") or "").lower()
    # Kalshi reports both yes_price and no_price on a fill; take the one
    # matching the side we actually hold, since that is what we paid.
    price = raw.get("no_price") if side == "no" else raw.get("yes_price")
    if price is None:
        price = raw.get("price", 0)
    return Fill(
        fill_id=str(fill_id),
        ticker=ticker,
        count=count,
        price_cents=float(price or 0),
        side=side,
        action=(raw.get("action") or "").lower(),
        fees_cents=float(raw.get("fee_paid", raw.get("fees_paid", 0)) or 0),
        exchange_order_id=raw.get("order_id"),
        client_order_id=raw.get("client_order_id"),
        created_at=_ts(raw.get("created_time")),
    )


def _int_or_none(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ts(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
