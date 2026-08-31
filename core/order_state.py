"""
Order lifecycle model: the vocabulary the rest of the system uses to talk
about an order, and the deterministic identity that makes submission
idempotent.

The central safety property here is that *nothing* counts as exposure until
the exchange confirms a fill. A successful HTTP response is not a fill. A
200 on POST /portfolio/orders means "the exchange accepted the order", which
for an IOC order can still mean zero contracts changed hands. The old
execution path incremented an in-memory position counter on any non-throwing
response, so a zero-fill IOC and a full fill were indistinguishable to risk.

States
------
    intent           persisted locally, not yet sent to the exchange
    submitted        request sent, exchange acknowledged, fills not yet read
    open             resting on the book with quantity remaining
    partially_filled some quantity filled; remainder open or cancelled
    filled           requested quantity fully filled
    cancelled        cancelled before filling the full quantity
    expired          IOC/TTL expiry without filling the full quantity
    rejected         exchange refused it (4xx, invalid price, etc.)
    unknown          request did not return a usable answer (timeout) and the
                     exchange may or may not hold the order
    dry_run          simulated only; never sent, never exposure

``unknown`` is the important one. A timeout on order submission is the case
where the bot's view and the exchange's view can silently diverge, so it is
modelled explicitly rather than folded into "error": while any order sits in
``unknown``, the account is not reconcilable and trading must stop until a
lookup by client order ID resolves it either way.

``dry_run`` is not in the safety brief's list; it is added so a paper order
is a first-class terminal state rather than a real state with a flag hanging
off it. Dry-run orders participate in duplicate prevention (so paper mode
exercises the same dedupe path as live) but never contribute exposure, since
exposure is only ever reconstructed from exchange-confirmed fills.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OrderState(str, Enum):
    INTENT = "intent"
    SUBMITTED = "submitted"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    DRY_RUN = "dry_run"


#: States that can never change again. ``partially_filled`` is deliberately
#: absent: for an IOC order the remainder is cancelled and it is terminal,
#: but for a GTC order the rest can still fill, so terminality depends on the
#: time-in-force and is decided by :func:`is_terminal`.
_ALWAYS_TERMINAL = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
        OrderState.DRY_RUN,
    }
)

#: States where the exchange may still hold quantity against us. Anything in
#: this set counts toward pending exposure, and ``unknown`` blocks trading.
LIVE_STATES = frozenset(
    {
        OrderState.SUBMITTED,
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.UNKNOWN,
    }
)


def is_terminal(state: OrderState, time_in_force: str = "GTC") -> bool:
    if state in _ALWAYS_TERMINAL:
        return True
    # An IOC order gets exactly one shot: whatever did not fill immediately is
    # cancelled by the exchange, so a partial fill is the end of its life.
    if state is OrderState.PARTIALLY_FILLED and time_in_force.upper() == "IOC":
        return True
    return False


@dataclass(frozen=True)
class OrderIntent:
    """A decision to trade, before it has any exchange identity.

    The intent is what gets persisted before the network call, so a crash
    between "decided to trade" and "exchange accepted" leaves a record to
    reconcile against instead of a silent gap.
    """

    ticker: str
    action: str            # "buy" | "sell"
    side: str              # "yes" | "no"
    count: int
    limit_price_cents: float
    time_in_force: str     # "IOC" | "GTC"
    source: str = "llm"    # which Maker produced the signal
    event_ticker: str = ""
    category: str = ""
    edge_id: Optional[int] = None
    #: Bucketed timestamp. See :meth:`intent_key`.
    dedupe_bucket: int = 0

    def intent_key(self) -> str:
        """Stable identity for "this signal, this order".

        Two scan passes 30 seconds apart that re-derive the same candidate
        produce the same key, which is what stops the loop from stacking
        duplicate orders on one ticker. Deliberately *excludes* anything that
        drifts between passes (the LLM's exact probability, wall-clock time),
        because a key that changes every pass is the same as having no key.

        ``dedupe_bucket`` is a coarse time bucket
        (``int(now / dedupe_window_seconds)``) so that a genuinely new trade
        on the same market at the same price is still possible later in the
        day, while everything inside one window collapses to one order. It is
        computed once when the intent is built and then persisted — retries
        reuse the stored client order ID rather than recomputing it, so a
        retry that straddles a bucket boundary cannot mint a second identity.
        """
        raw = "|".join(
            [
                self.ticker,
                self.action,
                self.side,
                str(int(self.count)),
                f"{self.limit_price_cents:.2f}",
                self.time_in_force.upper(),
                self.source,
                str(self.dedupe_bucket),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def client_order_id(self) -> str:
        """Deterministic client order ID derived from the intent key.

        Kalshi treats ``client_order_id`` as an idempotency key, so re-sending
        the same intent after a timeout cannot create a second order. This
        replaces ``uuid4()`` per attempt, which made every retry a brand new
        order from the exchange's point of view — the exact shape of the bug
        where a timeout on a filled order leads to a doubled position.
        """
        return self.intent_key()[:32]


@dataclass
class OrderRecord:
    """Local view of one order, reconciled against the exchange."""

    client_order_id: str
    intent_key: str
    ticker: str
    action: str
    side: str
    requested_count: int
    limit_price_cents: float
    time_in_force: str
    state: OrderState = OrderState.INTENT
    exchange_order_id: Optional[str] = None
    event_ticker: str = ""
    category: str = ""
    source: str = "llm"
    edge_id: Optional[int] = None
    filled_count: int = 0
    remaining_count: int = 0
    cancelled_count: int = 0
    expired_count: int = 0
    avg_fill_price_cents: Optional[float] = None
    fees_cents: float = 0.0
    dry_run: bool = False
    created_at: float = 0.0
    submitted_at: Optional[float] = None
    last_reconciled_at: Optional[float] = None
    terminal_at: Optional[float] = None
    expires_at: Optional[float] = None
    #: When the market closes, epoch seconds. Carried on the order so the
    #: close sweep is a local comparison rather than a network read per
    #: resting order — at maker cadence that difference is the whole
    #: rate-limit budget.
    close_time: Optional[float] = None
    #: When a cancel was requested but has not yet been confirmed by the
    #: exchange. Until it clears, the order still reserves exposure and must
    #: not be repriced: an unconfirmed cancel is not a cancel.
    cancel_requested_at: Optional[float] = None
    last_error: Optional[str] = None
    fills: list = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.state, self.time_in_force)

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES and not self.is_terminal

    @property
    def filled_cost_cents(self) -> float:
        """Cash actually committed by confirmed fills, including fees."""
        price = self.avg_fill_price_cents or 0.0
        return self.filled_count * price + self.fees_cents

    @property
    def pending_cost_cents(self) -> float:
        """Worst-case cash the exchange can still take from us on this order.

        For an ``unknown`` order we have to assume the whole requested
        quantity was accepted and may fill — assuming the optimistic case is
        precisely how an outage turns into an unbudgeted position.
        """
        if not self.is_live:
            return 0.0
        if self.state is OrderState.UNKNOWN:
            outstanding = self.requested_count
        else:
            outstanding = max(self.requested_count - self.filled_count, 0)
        return outstanding * self.limit_price_cents


@dataclass(frozen=True)
class Fill:
    """One exchange-confirmed execution. ``fill_id`` is unique per exchange
    fill and is what makes settlement writes idempotent."""

    fill_id: str
    ticker: str
    count: int
    price_cents: float
    side: str = ""
    action: str = ""
    fees_cents: float = 0.0
    exchange_order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    created_at: Optional[float] = None
