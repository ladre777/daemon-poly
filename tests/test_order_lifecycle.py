"""Resting-order maintenance.

Every property here is about what the sweep REFUSES to do. A lifecycle that
cancels enthusiastically is easy; one that never loses track of a position is
the hard part, and it is the only part that matters, because a resting order
the bot has miscounted is an unhedged position it does not know it holds.

The three failure modes under test, in order of expense:

1. A fill that races the cancel is booked as a cancellation. The bot then
   believes it is flat while holding a real position.
2. A replacement is placed while the original is still live, doubling the
   position at the moment the original fills.
3. A cancel that never confirms silently frees its exposure reservation.
"""
from __future__ import annotations

import time

from config import CONFIG
from core.kalshi_client import KalshiAPIError
from core.order_state import OrderIntent, OrderState
from workers.order_lifecycle import OrderLifecycle


class _Account:
    """Stands in for AccountState.refresh_order.

    Returns whatever the test says the exchange now reports, so the sweep's
    handling of each outcome is exercised without a network.
    """

    def __init__(self, store, outcome=None, raises=None):
        self.store = store
        self.outcome = outcome        # callable(record) -> record
        self.raises = raises
        self.refreshed: list[str] = []

    def refresh_order(self, record):
        self.refreshed.append(record.client_order_id)
        if self.raises:
            raise self.raises
        if self.outcome:
            record = self.outcome(record)
        self.store.update_order(record)
        return record


class _Client:
    def __init__(self, fail=False):
        self.fail = fail
        self.cancelled: list[str] = []

    def cancel_order(self, exchange_order_id):
        self.cancelled.append(exchange_order_id)
        if self.fail:
            raise KalshiAPIError(503, "exchange down")
        return {"ok": True}


def _resting(store, *, ticker="KXT-1", ttl_offset=+600, close_offset=+86400,
             state=OrderState.OPEN, count=10, dry_run=False):
    """A live GTC order on the book."""
    now = time.time()
    record = store.record_intent(OrderIntent(
        ticker=ticker, action="buy", side="yes", count=count,
        limit_price_cents=40.0, time_in_force="GTC",
    ))
    record.state = state
    record.exchange_order_id = f"ex-{record.client_order_id[:8]}"
    record.submitted_at = now
    record.expires_at = now + ttl_offset
    record.close_time = now + close_offset
    record.remaining_count = count
    record.dry_run = dry_run
    store.update_order(record)
    return record


def _terminal(new_state=OrderState.CANCELLED, filled=0):
    def apply(record):
        record.state = new_state
        record.filled_count = filled
        record.remaining_count = max(0, record.requested_count - filled)
        return record
    return apply


def _still_open(record):
    return record


# -- TTL ------------------------------------------------------------------


def test_an_order_past_its_ttl_is_cancelled(order_store):
    r = _resting(order_store, ttl_offset=-1)
    client = _Client()
    rep = OrderLifecycle(client, order_store, _Account(order_store, _terminal())).sweep()

    assert client.cancelled == [r.exchange_order_id]
    assert rep.expired == [r.client_order_id]
    assert rep.confirmed == [r.client_order_id]


def test_an_order_inside_its_ttl_is_left_alone(order_store):
    _resting(order_store, ttl_offset=+600)
    client = _Client()
    rep = OrderLifecycle(client, order_store, _Account(order_store)).sweep()

    assert client.cancelled == []
    assert rep.cancels_requested == 0


def test_an_order_with_no_ttl_is_never_expired(order_store):
    """IOC orders carry no expires_at. The sweep must not invent one."""
    r = _resting(order_store)
    r.expires_at = None
    order_store.update_order(r)

    client = _Client()
    OrderLifecycle(client, order_store, _Account(order_store)).sweep()
    assert client.cancelled == []


# -- market close ---------------------------------------------------------


def test_an_order_on_a_closing_market_is_cancelled(order_store):
    r = _resting(order_store, ttl_offset=+9999, close_offset=+60)
    client = _Client()
    rep = OrderLifecycle(client, order_store, _Account(order_store, _terminal())).sweep()

    assert client.cancelled == [r.exchange_order_id]
    assert rep.closing == [r.client_order_id]
    assert rep.expired == []


def test_a_far_off_close_does_not_cancel(order_store):
    _resting(order_store, ttl_offset=+9999, close_offset=+86400)
    client = _Client()
    OrderLifecycle(client, order_store, _Account(order_store)).sweep()
    assert client.cancelled == []


def test_a_zero_buffer_disables_close_cancellation(order_store, monkeypatch):
    """Not 'cancel everything the instant a close time is known'."""
    monkeypatch.setattr(CONFIG.risk, "order_close_cancel_buffer_seconds", 0)
    _resting(order_store, ttl_offset=+9999, close_offset=+1)
    client = _Client()
    OrderLifecycle(client, order_store, _Account(order_store)).sweep()
    assert client.cancelled == []


def test_an_order_with_no_close_time_is_not_cancelled_for_closing(order_store):
    r = _resting(order_store, ttl_offset=+9999)
    r.close_time = None
    order_store.update_order(r)

    client = _Client()
    OrderLifecycle(client, order_store, _Account(order_store)).sweep()
    assert client.cancelled == []


# -- the expensive failure: a fill racing the cancel ----------------------


def test_a_fill_during_the_cancel_is_reported_not_swallowed(order_store):
    """The single most expensive mistake available here.

    If a fill that lands between the TTL check and the cancel is filed as a
    cancellation, the bot believes it is flat while holding a real position.
    """
    r = _resting(order_store, ttl_offset=-1, count=10)
    account = _Account(order_store, _terminal(OrderState.FILLED, filled=10))
    rep = OrderLifecycle(_Client(), order_store, account).sweep()

    assert rep.filled_while_cancelling == [r.client_order_id]
    assert order_store.get_order(r.client_order_id).filled_count == 10


def test_a_partial_fill_during_the_cancel_is_also_reported(order_store):
    r = _resting(order_store, ttl_offset=-1, count=10)
    account = _Account(order_store, _terminal(OrderState.CANCELLED, filled=4))
    rep = OrderLifecycle(_Client(), order_store, account).sweep()

    assert rep.filled_while_cancelling == [r.client_order_id]
    assert rep.confirmed == [r.client_order_id]
    assert order_store.get_order(r.client_order_id).filled_count == 4


# -- unconfirmed cancels --------------------------------------------------


def test_an_unconfirmed_cancel_leaves_the_order_live_and_marked(order_store):
    """Still live, still reserving exposure, still refusing to reprice."""
    r = _resting(order_store, ttl_offset=-1)
    rep = OrderLifecycle(_Client(), order_store, _Account(order_store, _still_open)).sweep()

    assert rep.unconfirmed == [r.client_order_id]
    assert rep.confirmed == []
    stored = order_store.get_order(r.client_order_id)
    assert stored.cancel_requested_at is not None
    assert stored.state is OrderState.OPEN
    assert stored in order_store.live_orders() or any(
        o.client_order_id == r.client_order_id for o in order_store.live_orders()
    )


def test_a_pending_cancel_is_not_requested_twice(order_store):
    """The second sweep re-reads it; it does not fire another cancel."""
    _resting(order_store, ttl_offset=-1)
    client = _Client()
    life = OrderLifecycle(client, order_store, _Account(order_store, _still_open))

    life.sweep()
    assert len(client.cancelled) == 1
    life.sweep()
    assert len(client.cancelled) == 1, "cancel was requested twice"


def test_a_pending_cancel_that_later_confirms_is_cleared(order_store):
    r = _resting(order_store, ttl_offset=-1)
    account = _Account(order_store, _still_open)
    life = OrderLifecycle(_Client(), order_store, account)
    life.sweep()

    account.outcome = _terminal()
    rep = life.sweep()

    assert rep.confirmed == [r.client_order_id]
    assert order_store.get_order(r.client_order_id).cancel_requested_at is None


def test_a_cancel_the_exchange_rejects_stays_marked_outstanding(order_store):
    """A failed cancel is not a live order restored to health."""
    r = _resting(order_store, ttl_offset=-1)
    rep = OrderLifecycle(_Client(fail=True), order_store,
                         _Account(order_store)).sweep()

    assert rep.failed == [r.client_order_id]
    assert rep.expired == []
    assert order_store.get_order(r.client_order_id).cancel_requested_at is not None


def test_an_unreadable_order_after_cancel_is_unconfirmed_not_assumed(order_store):
    r = _resting(order_store, ttl_offset=-1)
    account = _Account(order_store, raises=KalshiAPIError(503, "down"))
    rep = OrderLifecycle(_Client(), order_store, account).sweep()

    assert rep.unconfirmed == [r.client_order_id]
    assert rep.confirmed == []


# -- what the sweep must never touch --------------------------------------


def test_dry_run_orders_are_never_cancelled(order_store):
    _resting(order_store, ttl_offset=-1, dry_run=True)
    client = _Client()
    OrderLifecycle(client, order_store, _Account(order_store)).sweep()
    assert client.cancelled == []


def test_an_unknown_state_order_is_never_cancelled(order_store):
    """We cannot read it, so we cannot know what cancelling would do.
    AccountState._resolve_unknown_orders owns these."""
    _resting(order_store, ttl_offset=-1, state=OrderState.UNKNOWN)
    client = _Client()
    OrderLifecycle(client, order_store, _Account(order_store)).sweep()
    assert client.cancelled == []


def test_an_order_without_an_exchange_id_is_never_cancelled(order_store):
    r = _resting(order_store, ttl_offset=-1)
    r.exchange_order_id = None
    order_store.update_order(r)

    client = _Client()
    OrderLifecycle(client, order_store, _Account(order_store)).sweep()
    assert client.cancelled == []


def test_the_sweep_can_only_cancel(order_store):
    """Structural: no placement path exists on this object at all."""
    life = OrderLifecycle(_Client(), order_store, _Account(order_store))
    for forbidden in ("place_order", "submit", "execute", "reprice", "quote"):
        assert not hasattr(life, forbidden)


# -- repricing ------------------------------------------------------------


def test_reprice_is_refused_while_the_original_is_live(order_store):
    """The second-most expensive mistake: a replacement placed while the
    original can still fill doubles the position."""
    r = _resting(order_store)
    assert OrderLifecycle(_Client(), order_store, _Account(order_store)).may_reprice(r) is False


def test_reprice_is_refused_while_a_cancel_is_merely_requested(order_store):
    r = _resting(order_store, ttl_offset=-1)
    life = OrderLifecycle(_Client(), order_store, _Account(order_store, _still_open))
    life.sweep()

    assert life.may_reprice(order_store.get_order(r.client_order_id)) is False


def test_reprice_is_allowed_only_once_the_cancel_confirms(order_store):
    r = _resting(order_store, ttl_offset=-1)
    life = OrderLifecycle(_Client(), order_store, _Account(order_store, _terminal()))
    life.sweep()

    stored = order_store.get_order(r.client_order_id)
    assert stored.is_terminal
    assert life.may_reprice(stored) is True
