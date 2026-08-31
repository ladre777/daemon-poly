"""Maintenance of resting orders: the first MAKER_PREREQUISITE.

Why this exists
---------------
The evidence points at market making. Becker's microstructure work — cited in
``_longshot_bias_guard`` and implemented independently in
``newyorkcompute/kalshi``'s optimism-tax strategy — finds that at longshot YES
prices (1-15c) YES has an expected value of -41% while NO at the same prices
has +23%, and that the profit accrues to the **maker** who is the counterparty
to optimistic taker flow, not to a taker crossing the spread.

This bot's own ledger agrees on both halves. Its long-YES book under 20c is
1,144 settled rows with **zero** wins. And the taker version of the other side
— buying NO at 80-100c, which is selling a YES longshot — is measured over 174
distinct events at -$0.0102 a row, t = -1.05. Buying NO at 90c needs a 91% win
rate to break even against the fee; the observed rate is 87.3%. The shortfall
is almost exactly the spread a taker pays and a maker earns.

So the edge, if there is one, is in resting quotes. ``ORDER_STRATEGY=maker``
is refused until the lifecycle around them exists, and this module is the
first item on that list.

Scope, deliberately narrow
--------------------------
Three things, all of which only ever **remove** exposure:

* **TTL expiry.** ``Execution.expire_stale_orders`` was written, tested, and
  then never called by anything. Dead code in a safety path is worse than
  absent code: it reads as covered.
* **Market-close cancellation.** A resting order on a market about to close
  cannot be repriced or withdrawn once trading halts, and whatever it is
  holding becomes an unhedged position taken by a clock rather than a view.
* **Cancel confirmation.** An unconfirmed cancel is not a cancel. Until the
  exchange says so the order still reserves exposure and must not be
  repriced — otherwise a reprice doubles the position at the moment the
  original fills.

What it does not do: place, reprice upward, size, or quote. Repricing is
offered only as ``may_reprice``, a question, so the decision to re-enter stays
with the caller and cannot happen implicitly inside a sweep.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from config import CONFIG
from core.kalshi_client import KalshiAPIError
from core.order_state import OrderState

log = logging.getLogger("daemon_kalshi.order_lifecycle")


@dataclass
class LifecycleReport:
    """What one sweep did. Every list holds client_order_ids."""

    expired: list[str] = field(default_factory=list)
    closing: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    filled_while_cancelling: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def cancels_requested(self) -> int:
        return len(self.expired) + len(self.closing)

    def describe(self) -> str:
        return (
            f"ttl={len(self.expired)} closing={len(self.closing)} "
            f"confirmed={len(self.confirmed)} "
            f"filled_while_cancelling={len(self.filled_while_cancelling)} "
            f"unconfirmed={len(self.unconfirmed)} failed={len(self.failed)}"
        )


class OrderLifecycle:
    """Sweeps resting orders. Can only ever cancel, never place."""

    def __init__(self, client, store, account):
        self.client = client
        self.store = store
        self.account = account

    # -- the sweep ---------------------------------------------------------

    def sweep(self, now: float = None) -> LifecycleReport:
        """One maintenance pass over every live order.

        Ordered deliberately: outstanding cancels are resolved *before* new
        ones are requested, so an order whose cancel is already in flight is
        never asked to cancel twice, and its exposure is released as early as
        the exchange allows.
        """
        now = time.time() if now is None else now
        report = LifecycleReport()

        for record in self.store.live_orders():
            if record.dry_run:
                continue
            if record.cancel_requested_at is not None:
                self._resolve_pending_cancel(record, now, report)

        for record in self.store.live_orders():
            if not self._is_cancellable(record):
                continue
            if self._past_ttl(record, now):
                if self._request_cancel(record, now, "TTL", report):
                    report.expired.append(record.client_order_id)
            elif self._closing_soon(record, now):
                if self._request_cancel(record, now, "market close", report):
                    report.closing.append(record.client_order_id)

        if report.cancels_requested or report.confirmed or report.unconfirmed:
            log.info("Order lifecycle sweep: %s", report.describe())
        return report

    # -- predicates --------------------------------------------------------

    @staticmethod
    def _is_cancellable(record) -> bool:
        """Only a resting order with an exchange identity can be cancelled.

        An order in UNKNOWN state is excluded on purpose: we do not know
        whether the exchange holds it, and cancelling something whose state we
        cannot read is how a position is opened by accident. Those are
        resolved by AccountState._resolve_unknown_orders first.
        """
        if record.dry_run or record.cancel_requested_at is not None:
            return False
        if record.state is OrderState.UNKNOWN:
            return False
        return bool(record.exchange_order_id)

    @staticmethod
    def _past_ttl(record, now: float) -> bool:
        return bool(record.expires_at) and record.expires_at <= now

    @staticmethod
    def _closing_soon(record, now: float) -> bool:
        """Is the market close near enough that resting is no longer safe?

        Guarded on the buffer being positive so setting it to zero disables
        close-cancellation outright rather than cancelling everything the
        instant a close time is known.
        """
        buffer = CONFIG.risk.order_close_cancel_buffer_seconds
        if buffer <= 0 or not record.close_time:
            return False
        return record.close_time - now <= buffer

    # -- cancel, and confirmation ------------------------------------------

    def _request_cancel(self, record, now: float, why: str,
                        report: LifecycleReport) -> bool:
        """Ask the exchange to cancel. Records the request before the call.

        Written first for the same reason ``record_intent`` is: a crash or
        timeout between asking and hearing back must leave a record that a
        cancel is outstanding, or the next sweep asks again and the order is
        repriced against a cancel that may already have landed.
        """
        record.cancel_requested_at = now
        self.store.update_order(record)
        try:
            self.client.cancel_order(record.exchange_order_id)
        except KalshiAPIError as e:
            # The request is left marked as outstanding rather than cleared.
            # A failed cancel is not a live order restored to health; the next
            # sweep re-reads it from the exchange and acts on what it finds.
            log.error("Cancel (%s) failed for %s: %s",
                      why, record.client_order_id, e)
            report.failed.append(record.client_order_id)
            return False
        log.info("Cancel requested (%s) for %s on %s",
                 why, record.client_order_id, record.ticker)
        self._resolve_pending_cancel(record, now, report)
        return True

    def _resolve_pending_cancel(self, record, now: float,
                                report: LifecycleReport) -> None:
        """Re-read the order and decide whether the cancel actually landed.

        Never assumes it won. Between the TTL check and the cancel reaching
        the exchange the order may have filled, and treating that fill as a
        cancellation would leave a real position the bot believes it does not
        hold — the single most expensive mistake available here.
        """
        # Captured BEFORE the refresh, and deliberately not compared against
        # the returned object. AccountState.apply_exchange_order merges the
        # payload into the record IN PLACE and returns the same instance, so
        # `updated.filled_count > record.filled_count` compares a value to
        # itself and is always False. That would have silently disabled the
        # one check that stops a fill racing a cancel from being booked as a
        # cancellation.
        filled_before = record.filled_count
        try:
            updated = self.account.refresh_order(record)
        except KalshiAPIError as e:
            log.warning("Could not re-read %s after cancel: %s",
                        record.client_order_id, e)
            report.unconfirmed.append(record.client_order_id)
            return

        if updated.filled_count > filled_before:
            # Raced. Report it loudly: a fill during a cancel is exactly the
            # adverse-selection event a maker needs to measure, and it is
            # invisible if it is filed as an ordinary fill.
            log.warning(
                "Order %s filled %d while its cancel was in flight",
                updated.client_order_id,
                updated.filled_count - filled_before,
            )
            report.filled_while_cancelling.append(updated.client_order_id)

        if updated.is_terminal:
            updated.cancel_requested_at = None
            self.store.update_order(updated)
            report.confirmed.append(updated.client_order_id)
            return

        timeout = CONFIG.risk.cancel_confirm_timeout_seconds
        if timeout > 0 and now - (updated.cancel_requested_at or now) > timeout:
            # Still live well after the cancel was requested. Left live and
            # left marked: it keeps reserving exposure, and may_reprice keeps
            # refusing, which is the safe direction. Escalation is an operator
            # decision, not something to resolve by assuming.
            log.error(
                "Cancel for %s has not confirmed after %.0fs — order still "
                "live and still reserving exposure",
                updated.client_order_id, now - updated.cancel_requested_at,
            )
        report.unconfirmed.append(updated.client_order_id)

    # -- repricing ---------------------------------------------------------

    def may_reprice(self, record) -> bool:
        """Is it safe to place a replacement for this order?

        Only once the original is terminal. A replacement placed while the
        original is still live — or while its cancel is merely requested —
        doubles the position at exactly the moment the original fills, which
        is the failure this whole module exists to prevent.

        A question rather than an action: the caller decides whether to
        re-enter, and it can never happen implicitly inside a sweep.
        """
        if record.cancel_requested_at is not None:
            return False
        return bool(record.is_terminal)
