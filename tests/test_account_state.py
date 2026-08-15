"""
P0-1: account and order reconciliation.

The requirement these cover: on restart, reconstructed exposure must match
exchange state, and the bot must not trade when it cannot verify that state.
The version this replaces held open positions in a process-local integer that
started at zero on every Railway redeploy.
"""
from __future__ import annotations

import time

import pytest

from config import CONFIG
from core.account_state import AccountState, ReconciliationError
from core.kalshi_client import KalshiAPIError
from core.order_state import OrderIntent, OrderState
from memory.order_store import OrderStore

from tests.conftest import make_verdict
from tests.fakes import FakeKalshiClient, TimeoutOnce
from tests.test_execution import approved


def test_restart_rebuilds_exposure_from_exchange_positions(client, db_path):
    """A brand new process with an empty local database must still see the
    positions the previous process opened."""
    client.add_position("KXA-1", "yes", quantity=10, avg_price_cents=40.0,
                        event_ticker="KXA")
    client.add_position("KXB-1", "no", quantity=5, avg_price_cents=30.0,
                        event_ticker="KXB")
    CONFIG.risk.allow_position_drift = True  # no local fill history to match

    account = AccountState(client, OrderStore(db_path))
    snap = account.reconcile()

    assert snap.open_position_count() == 2
    # 10 * 40c + 5 * 30c = 550c
    assert snap.position_exposure_cents() == pytest.approx(550.0)
    assert snap.exposure_by_ticker_cents()["KXA-1"] == pytest.approx(400.0)
    assert snap.exposure_by_event_cents()["KXB"] == pytest.approx(150.0)


def test_restart_reconstructs_the_same_exposure_the_first_process_took(
    client, db_path
):
    """Full round trip: place an order in one 'process', throw away the
    in-memory state, and check a fresh AccountState agrees with the exchange."""
    CONFIG.risk.dry_run = False
    from workers.execution import Execution

    store = OrderStore(db_path)
    account = AccountState(client, store)
    account.reconcile()
    record = Execution(client, store, account).execute(make_verdict(), approved(size=10))
    assert record.filled_count == 10

    # The exchange now reports the position that fill created.
    client.add_position("KXTEST-25AUG14-A", "yes", quantity=10,
                        avg_price_cents=52.0, event_ticker="KXTEST-25AUG14")

    restarted = AccountState(FakeKalshiClientFrom(client), OrderStore(db_path))
    snap = restarted.reconcile()

    assert snap.position_exposure_cents() == pytest.approx(10 * 52.0)
    assert snap.inconsistencies == [], "local fills should match exchange positions"
    assert snap.is_tradeable


def FakeKalshiClientFrom(other: FakeKalshiClient) -> FakeKalshiClient:
    """A 'new process' talking to the same exchange state."""
    fresh = FakeKalshiClient(balance_cents=other.balance_cents)
    fresh.market_positions = other.market_positions
    fresh.orders = other.orders
    fresh.fills = other.fills
    fresh.settlements = other.settlements
    fresh.markets = other.markets
    return fresh


def test_local_fills_disagreeing_with_the_exchange_blocks_trading(client, db_path):
    """A divergence between our fill record and the exchange means one of them
    is wrong. Trading through that is how a bot doubles a position it thinks
    it doesn't have."""
    store = OrderStore(db_path)
    from core.order_state import Fill

    store.record_fills([
        Fill(fill_id="f1", ticker="KXA-1", count=10, price_cents=40.0,
             side="yes", action="buy")
    ])
    # Exchange says the position is smaller than our fills reconstruct.
    client.add_position("KXA-1", "yes", quantity=4, avg_price_cents=40.0)

    snap = AccountState(client, store).reconcile()

    assert snap.inconsistencies
    assert not snap.is_tradeable
    assert "exchange says 4" in snap.blocking_reason()


def test_allow_position_drift_downgrades_the_mismatch_to_a_warning(client, db_path):
    from core.order_state import Fill

    store = OrderStore(db_path)
    store.record_fills([
        Fill(fill_id="f1", ticker="KXA-1", count=10, price_cents=40.0,
             side="yes", action="buy")
    ])
    client.add_position("KXA-1", "yes", quantity=4, avg_price_cents=40.0)
    CONFIG.risk.allow_position_drift = True

    snap = AccountState(client, store).reconcile()

    assert snap.inconsistencies == []
    assert snap.is_tradeable


# -- fail closed ------------------------------------------------------------


def test_reconciliation_failure_raises_rather_than_returning_stale_numbers(
    client, order_store
):
    def down(*a, **kw):
        raise TimeoutOnce()

    client.get_balance = down
    account = AccountState(client, order_store)

    with pytest.raises(ReconciliationError):
        account.reconcile()
    assert not account.snapshot.is_tradeable


def test_a_never_reconciled_account_is_not_tradeable(client, order_store):
    account = AccountState(client, order_store)
    assert not account.snapshot.is_tradeable
    assert "never been reconciled" in account.snapshot.blocking_reason()
    with pytest.raises(ReconciliationError):
        account.require_tradeable()


def test_a_stale_snapshot_is_not_tradeable(client, order_store):
    account = AccountState(client, order_store)
    snap = account.reconcile()
    assert snap.is_tradeable

    snap.reconciled_at = time.time() - (CONFIG.risk.max_reconciliation_age_seconds + 1)

    assert snap.is_stale()
    assert not snap.is_tradeable
    assert "stale" in snap.blocking_reason()


def test_missing_balance_field_is_a_reconciliation_failure(client, order_store):
    client.get_balance = lambda: {}
    with pytest.raises(ReconciliationError):
        AccountState(client, order_store).reconcile()


def test_account_limits_404_does_not_block_trading(client, order_store):
    """Demo accounts do not always expose /account/limits. Our own caps are
    the binding ones, so a 4xx there must not stop the bot."""
    def missing():
        raise KalshiAPIError(404, "not found")

    client.get_account_limits = missing
    snap = AccountState(client, order_store).reconcile()
    assert snap.is_tradeable
    assert snap.limits == {}


def test_server_error_on_limits_is_a_reconciliation_failure(client, order_store):
    def boom():
        raise KalshiAPIError(500, "internal")

    client.get_account_limits = boom
    with pytest.raises(ReconciliationError):
        AccountState(client, order_store).reconcile()


# -- unknown orders ---------------------------------------------------------


def test_an_unknown_order_blocks_trading_until_resolved(client, order_store):
    intent = OrderIntent(ticker="KXA-1", action="buy", side="yes", count=10,
                         limit_price_cents=50.0, time_in_force="IOC")
    record = order_store.record_intent(intent)
    record.state = OrderState.UNKNOWN
    order_store.update_order(record)

    def explode(**kw):
        raise KalshiAPIError(503, "down")

    client.get_orders = explode
    snap = AccountState(client, order_store).reconcile()

    assert snap.unknown_order_ids == [record.client_order_id]
    assert not snap.is_tradeable
    assert "unknown state" in snap.blocking_reason()


def test_reconciliation_resolves_an_unknown_order_the_exchange_never_got(
    client, order_store
):
    intent = OrderIntent(ticker="KXA-1", action="buy", side="yes", count=10,
                         limit_price_cents=50.0, time_in_force="IOC")
    record = order_store.record_intent(intent)
    record.state = OrderState.UNKNOWN
    order_store.update_order(record)

    snap = AccountState(client, order_store).reconcile()

    assert snap.unknown_order_ids == []
    assert snap.is_tradeable
    assert order_store.get_order(record.client_order_id).state is OrderState.REJECTED


def test_unknown_orders_reserve_their_full_requested_quantity(order_store):
    """Pending exposure for an unknown order assumes the worst: the exchange
    may hold all of it."""
    intent = OrderIntent(ticker="KXA-1", action="buy", side="yes", count=10,
                         limit_price_cents=50.0, time_in_force="IOC")
    record = order_store.record_intent(intent)
    record.state = OrderState.UNKNOWN
    record.filled_count = 3
    order_store.update_order(record)

    assert order_store.get_order(record.client_order_id).pending_cost_cents == pytest.approx(500.0)


# -- fill storage -----------------------------------------------------------


def test_rereading_fills_does_not_double_count(client, order_store):
    client.fills.append({
        "trade_id": "f1", "ticker": "KXA-1", "order_id": "ord-1",
        "client_order_id": "c1", "side": "yes", "action": "buy", "count": 5,
        "yes_price": 40.0, "fee_paid": 2.0, "created_time": 1_700_000_000,
    })
    account = AccountState(client, order_store)
    account.reconcile()
    account.reconcile()
    account.reconcile()

    local = order_store.position_from_fills()
    assert local[("KXA-1", "yes")]["quantity"] == 5
