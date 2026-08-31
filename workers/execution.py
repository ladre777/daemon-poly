"""
Execution: turns an approved RiskDecision into an order, idempotently.

Three properties this file is responsible for, each replacing a specific way
the previous version could lose money:

1. **Idempotent submission.** The client order ID is derived deterministically
   from the order intent and persisted *before* the request goes out. The old
   version generated a fresh ``uuid4()`` per attempt, so a retry after a
   timeout looked like a brand new order to Kalshi — the classic path to a
   doubled position.

2. **Fills, not acknowledgements.** Nothing counts as exposure or as an
   executed trade until fills are read back from the exchange. The old
   version returned the raw order response and the caller incremented a
   position counter, which treated a zero-fill IOC exactly like a full fill.

3. **Duplicate prevention across scan passes.** The 30-second loop re-derives
   the same candidate every pass while a signal persists. Intents carry a
   stable key, so the second pass finds the first order instead of stacking
   another one on the same ticker.

Strategy note (unchanged from the original analysis): Becker's 72.1M-trade
Kalshi study found takers lose ~1.12% on average while makers gain ~1.12%, so
the documented edge is on the passive side. That is why maker mode exists as a
config option — and why it stays refused until the resting-order lifecycle
(lookup, TTL, cancellation, requote, reservation) is built and tested, since
posting unmanaged GTC orders from a 30-second loop is an unbounded-exposure
bug, not a smaller edge.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from config import CONFIG
from core.account_state import AccountState
from core.kalshi_client import KalshiAPIError, KalshiClient, KalshiTimeoutError
from core.order_state import OrderIntent, OrderRecord, OrderState
from core.pricing import price_dollars_string
from memory.order_store import OrderStore
from workers.checker import Verdict
from workers.risk_guardrail import RiskDecision

log = logging.getLogger("daemon_kalshi.execution")


class UnmanagedMakerMode(Exception):
    """Raised when maker mode is requested without lifecycle management."""


class DuplicateOrderBlocked(Exception):
    """A live order for this exact intent already exists."""

    def __init__(self, existing: OrderRecord):
        self.existing = existing
        super().__init__(
            f"live order {existing.client_order_id} already exists for "
            f"{existing.ticker} {existing.side} x{existing.requested_count} "
            f"@ {existing.limit_price_cents:.0f}c (state={existing.state.value})"
        )


def assert_order_strategy_supported(strategy: str = None) -> str:
    """Refuse maker mode until its order lifecycle exists.

    Checked at startup *and* here at the chokepoint. Startup alone is not
    enough: config is mutable at runtime and a future caller could construct
    Execution directly, so the gate lives where orders are actually placed.
    """
    strategy = (strategy or CONFIG.risk.order_strategy or "taker").lower()
    if strategy == "maker":
        raise UnmanagedMakerMode(
            "ORDER_STRATEGY=maker is not safe to run: posting resting GTC "
            "orders requires existing-order lookup before submission, "
            "duplicate-order prevention across scan passes, one active order "
            "per strategy/ticker/side/price intent, TTL expiration, "
            "cancellation when the signal goes stale or the market closes, "
            "repricing only after a confirmed cancel, and exposure "
            "reservation for every outstanding order. None of that is "
            "implemented and tested yet. Set ORDER_STRATEGY=taker."
        )
    if strategy != "taker":
        raise UnmanagedMakerMode(
            f"unknown ORDER_STRATEGY={strategy!r} — expected 'taker' or 'maker'"
        )
    return strategy


class Execution:
    def __init__(
        self,
        client: KalshiClient = None,
        store: OrderStore = None,
        account: AccountState = None,
    ):
        self.client = client or KalshiClient()
        self.store = store or OrderStore()
        self.account = account or AccountState(self.client, self.store)

    # -- intent construction ------------------------------------------------

    def build_intent(self, verdict: Verdict, decision: RiskDecision) -> OrderIntent:
        c = verdict.proposal.candidate
        strategy = assert_order_strategy_supported()
        # taker: cross the spread with IOC so the order either fills now or
        # dies. Nothing rests on the book, so there is no unmanaged order to
        # track — the property that makes taker mode safe to run today.
        window = max(CONFIG.risk.dedupe_window_seconds, 1.0)
        return OrderIntent(
            ticker=c.ticker,
            action="buy",
            side=verdict.proposal.direction,
            count=decision.size_contracts,
            limit_price_cents=decision.limit_price_cents,
            time_in_force="IOC" if strategy == "taker" else "GTC",
            source=verdict.proposal.source,
            event_ticker=getattr(c, "event_ticker", "") or "",
            category=c.category,
            dedupe_bucket=int(time.time() / window),
        )

    def find_duplicate(self, intent: OrderIntent) -> Optional[OrderRecord]:
        """Any order already carrying this intent key, whatever became of it.

        One attempt per intent per dedupe window, regardless of outcome. The
        looser rule — block only live or filled orders — leaves a zero-fill
        IOC re-submittable on the very next pass, so a signal that persists
        while the price sits just out of reach becomes a submission every 30
        seconds for as long as it lasts. Each of those is a real order that
        can fill on an adverse tick.

        A genuinely new attempt is still reachable: the intent key includes
        price and size, so a re-quote at a different price is a different
        intent, and the hourly bucket lets an unchanged signal try again
        later. That is a deliberate trade of fill rate for bounded submission
        volume, which is the right side to err on for an unattended bot.
        """
        existing = self.store.find_by_intent_key(intent.intent_key())
        return existing[0] if existing else None

    # -- submission ---------------------------------------------------------

    def execute(self, verdict: Verdict, decision: RiskDecision) -> OrderRecord:
        """Place one order for an approved decision.

        Returns the reconciled OrderRecord. The caller must read
        ``record.filled_count`` to learn what actually happened — a returned
        record is not evidence of a fill.
        """
        assert_order_strategy_supported()
        if not decision.approved or decision.size_contracts < 1:
            raise ValueError("execute() called with an unapproved or zero-size decision")

        # Risk already checked this, but so does the chokepoint: an order must
        # never leave this process on the strength of an upstream component
        # having verified account state. Raises ReconciliationError.
        self.account.require_tradeable()

        intent = self.build_intent(verdict, decision)

        duplicate = self.find_duplicate(intent)
        if duplicate is not None:
            raise DuplicateOrderBlocked(duplicate)

        # Persist before the network call. If the process dies between here
        # and the response, startup reconciliation finds this row and asks
        # Kalshi what happened to it.
        record = self.store.record_intent(intent)
        record.edge_id = decision.edge_id
        record.dry_run = CONFIG.risk.dry_run

        if CONFIG.risk.dry_run:
            record.state = OrderState.DRY_RUN
            record.terminal_at = time.time()
            record.remaining_count = 0
            self.store.update_order(record)
            log.info(
                "[DRY RUN] would BUY %d %s on %s @ %.0fc (client_order_id=%s) — "
                "no order sent, no exposure recorded",
                record.requested_count, record.side.upper(), record.ticker,
                record.limit_price_cents, record.client_order_id,
            )
            return record

        return self._submit(record)

    def _submit(self, record: OrderRecord) -> OrderRecord:
        price_field = (
            "yes_price_dollars" if record.side == "yes" else "no_price_dollars"
        )
        price_dollars = price_dollars_string(record.limit_price_cents)
        record.submitted_at = time.time()
        if record.time_in_force.upper() == "GTC":
            record.expires_at = record.submitted_at + CONFIG.risk.order_ttl_seconds
        self.store.update_order(record)

        try:
            response = self.client.place_order(
                ticker=record.ticker,
                action=record.action,
                side=record.side,
                count=record.requested_count,
                order_type="limit",
                time_in_force=record.time_in_force,
                client_order_id=record.client_order_id,
                **{price_field: price_dollars},
            )
        except KalshiTimeoutError as e:
            # The dangerous case: Kalshi may hold this order. Do NOT retry —
            # mark unknown, which blocks all further trading until
            # reconciliation resolves it by client order ID.
            record.state = OrderState.UNKNOWN
            record.last_error = str(e)
            self.store.update_order(record)
            log.error(
                "Order %s timed out — state UNKNOWN, exchange may hold it. "
                "Trading is blocked until reconciliation resolves it.",
                record.client_order_id,
            )
            recovered = self._recover_unknown(record)
            if recovered.state is OrderState.UNKNOWN:
                raise
            return recovered
        except KalshiAPIError as e:
            if e.is_client_error:
                record.state = OrderState.REJECTED
                record.last_error = str(e)
                record.remaining_count = 0
                record.terminal_at = time.time()
                self.store.update_order(record)
                log.error("Order %s rejected by Kalshi: %s", record.ticker, e)
                return record
            # 5xx says nothing about whether the order landed — same hazard as
            # a timeout, so the same conservative handling.
            record.state = OrderState.UNKNOWN
            record.last_error = str(e)
            self.store.update_order(record)
            recovered = self._recover_unknown(record)
            if recovered.state is OrderState.UNKNOWN:
                raise
            return recovered

        payload = response.get("order", response) if isinstance(response, dict) else {}
        record.exchange_order_id = payload.get("order_id")
        record.state = OrderState.SUBMITTED
        self.store.update_order(record)

        # An accepted order is not an executed trade. Read the fills back
        # before anyone treats this as exposure.
        reconciled = self.account.apply_exchange_order(record, payload)
        self._log_outcome(reconciled)
        return reconciled

    def _recover_unknown(self, record: OrderRecord) -> OrderRecord:
        """Resolve an unknown order by asking Kalshi about the client order ID.

        This is the whole reason the ID is deterministic and persisted first:
        after a timeout it is the only handle we have on an order that may or
        may not exist.
        """
        try:
            data = self.client.get_orders(client_order_id=record.client_order_id) or {}
        except (KalshiAPIError, KalshiTimeoutError) as e:
            log.error(
                "Recovery lookup for %s failed (%s) — staying UNKNOWN",
                record.client_order_id, e,
            )
            return record

        match = next(
            (
                o for o in data.get("orders", [])
                if o.get("client_order_id") == record.client_order_id
            ),
            None,
        )
        if match is None:
            record.state = OrderState.REJECTED
            record.last_error = "not found on exchange after timeout — never accepted"
            record.remaining_count = 0
            record.terminal_at = time.time()
            self.store.update_order(record)
            log.warning(
                "Order %s never reached Kalshi — marked rejected, no exposure",
                record.client_order_id,
            )
            return record

        log.warning(
            "Order %s WAS accepted despite the timeout — recovering its true state",
            record.client_order_id,
        )
        recovered = self.account.apply_exchange_order(record, match)
        self._log_outcome(recovered)
        return recovered

    def _log_outcome(self, record: OrderRecord) -> None:
        if record.filled_count <= 0:
            log.info(
                "Order %s on %s: %s with ZERO fills — no exposure taken",
                record.client_order_id, record.ticker, record.state.value,
            )
            return
        partial = record.filled_count < record.requested_count
        log.info(
            "Order %s on %s: %s %d/%d contracts @ avg %.1fc (fees %.1fc)%s",
            record.client_order_id, record.ticker, record.state.value,
            record.filled_count, record.requested_count,
            record.avg_fill_price_cents or 0.0, record.fees_cents,
            " — PARTIAL" if partial else "",
        )

    # -- resting-order maintenance -----------------------------------------
    #
    # Owned by workers/order_lifecycle.OrderLifecycle, which is swept once per
    # pass from main. The version that lived here was never called by anything
    # — dead code in a safety path, which reads as covered and is not.
