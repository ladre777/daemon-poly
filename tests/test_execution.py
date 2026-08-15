"""
P0-2: idempotent, fill-aware order submission.

Each test here maps to a failure the previous execution path could produce:
double submission on retry, a zero-fill IOC counted as a position, a timeout
losing track of a live order, and the 30-second scan loop stacking duplicate
orders on one signal.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from core.kalshi_client import KalshiAPIError, KalshiTimeoutError
from core.order_state import OrderIntent, OrderState
from workers.execution import DuplicateOrderBlocked, UnmanagedMakerMode
from workers.risk_guardrail import RiskDecision

from tests.conftest import make_verdict
from tests.fakes import TimeoutOnce


def approved(size=10, price=52.0, edge_id=None) -> RiskDecision:
    return RiskDecision(
        approved=True,
        reason="test",
        size_contracts=size,
        executable_price_cents=price,
        limit_price_cents=price,
        edge_id=edge_id,
    )


# -- deterministic identity ------------------------------------------------


def test_client_order_id_is_deterministic_for_the_same_intent():
    """The whole idempotency story rests on this: same intent, same ID."""
    kwargs = dict(
        ticker="KXA-1", action="buy", side="yes", count=10,
        limit_price_cents=52.0, time_in_force="IOC", dedupe_bucket=7,
    )
    assert OrderIntent(**kwargs).client_order_id() == OrderIntent(**kwargs).client_order_id()


def test_client_order_id_changes_when_the_order_changes():
    base = dict(
        ticker="KXA-1", action="buy", side="yes", count=10,
        limit_price_cents=52.0, time_in_force="IOC", dedupe_bucket=7,
    )
    first = OrderIntent(**base).client_order_id()
    for field, value in [
        ("ticker", "KXA-2"), ("side", "no"), ("count", 11),
        ("limit_price_cents", 53.0), ("dedupe_bucket", 8),
    ]:
        assert OrderIntent(**{**base, field: value}).client_order_id() != first, field


# -- fills, not acknowledgements -------------------------------------------


def test_ioc_full_fill_records_exposure(execution, client):
    record = execution.execute(make_verdict(), approved(size=10))

    assert record.state is OrderState.FILLED
    assert record.filled_count == 10
    assert record.avg_fill_price_cents == pytest.approx(52.0)
    assert record.is_terminal


def test_ioc_zero_fill_takes_no_exposure(execution, client):
    """A 200 from the exchange is not a fill. The old code incremented a
    position counter on any non-throwing response, making this case
    indistinguishable from a full fill."""
    client.fill_plan = [0]

    record = execution.execute(make_verdict(), approved(size=10))

    assert record.filled_count == 0
    assert record.state is OrderState.CANCELLED
    assert record.filled_cost_cents == 0
    assert record.pending_cost_cents == 0, "a dead IOC must not reserve exposure"


def test_ioc_partial_fill_counts_only_what_filled(execution, client):
    client.fill_plan = [3]

    record = execution.execute(make_verdict(), approved(size=10))

    assert record.filled_count == 3
    assert record.state is OrderState.PARTIALLY_FILLED
    assert record.is_terminal, "IOC remainder is cancelled, so this is terminal"
    assert record.pending_cost_cents == 0
    assert record.filled_cost_cents == pytest.approx(3 * 52.0)


def test_fees_are_carried_onto_the_record(execution, client):
    client.fee_per_contract_cents = 1.5

    record = execution.execute(make_verdict(), approved(size=4))

    assert record.fees_cents == pytest.approx(6.0)
    assert record.filled_cost_cents == pytest.approx(4 * 52.0 + 6.0)


# -- duplicate prevention ---------------------------------------------------


def test_second_scan_pass_does_not_stack_a_duplicate_order(execution, client):
    """The 30s loop re-derives the same candidate while a signal persists."""
    verdict = make_verdict()
    execution.execute(verdict, approved(size=10))

    with pytest.raises(DuplicateOrderBlocked):
        execution.execute(verdict, approved(size=10))

    assert len(client.place_order_calls) == 1


def test_duplicate_guard_holds_even_when_the_first_order_did_not_fill(execution, client):
    """A zero-fill order still consumed its intent for this window. Retrying
    it every 30 seconds would be an unbounded submission loop."""
    client.fill_plan = [0]
    verdict = make_verdict()
    execution.execute(verdict, approved(size=10))

    client.fill_plan = [10]
    with pytest.raises(DuplicateOrderBlocked):
        execution.execute(verdict, approved(size=10))
    assert len(client.place_order_calls) == 1


def test_a_different_price_is_a_different_intent(execution, client):
    verdict = make_verdict()
    execution.execute(verdict, approved(size=10, price=52.0))
    execution.execute(verdict, approved(size=10, price=55.0))
    assert len(client.place_order_calls) == 2


# -- timeout recovery -------------------------------------------------------


def test_timeout_after_exchange_accepted_recovers_the_real_order(execution, client, order_store):
    """The dangerous case: the exchange took the order, we never saw the
    response. Recovery must find it by client order ID and must not submit a
    second one."""
    client.raise_on_place = TimeoutOnce()
    client.accept_before_raising = True

    record = execution.execute(make_verdict(), approved(size=10))

    assert record.state is OrderState.FILLED
    assert record.filled_count == 10
    assert len(client.place_order_calls) == 1
    assert not order_store.unknown_orders(), "recovered order must not stay unknown"


def test_timeout_where_the_order_never_landed_is_marked_rejected(execution, client, order_store):
    client.raise_on_place = TimeoutOnce()
    client.accept_before_raising = False

    record = execution.execute(make_verdict(), approved(size=10))

    assert record.state is OrderState.REJECTED
    assert record.filled_count == 0
    assert record.pending_cost_cents == 0
    assert not order_store.unknown_orders()


def test_unresolvable_timeout_leaves_the_order_unknown_and_blocks(execution, client, order_store):
    """If the recovery lookup also fails, the order stays unknown — which is
    what makes the account untradeable until a human or a later reconciliation
    resolves it. Guessing 'it probably didn't land' is how an outage becomes an
    unbudgeted position."""
    client.raise_on_place = TimeoutOnce()
    client.accept_before_raising = True

    def explode(*a, **kw):
        raise KalshiAPIError(503, "gateway down")

    client.get_orders = explode

    # The submission timeout is what propagates: the recovery lookup failing
    # means we never learn the order's fate, so the timeout stands.
    with pytest.raises(KalshiTimeoutError):
        execution.execute(make_verdict(), approved(size=10))

    unknown = order_store.unknown_orders()
    assert len(unknown) == 1
    # Unknown orders reserve their FULL requested quantity, because the
    # exchange may hold all of it.
    assert unknown[0].pending_cost_cents == pytest.approx(10 * 52.0)


def test_resubmitting_the_same_intent_id_does_not_create_two_orders(execution, client):
    """Belt and braces: even if the duplicate guard were bypassed, the
    deterministic client order ID makes the exchange itself dedupe."""
    verdict = make_verdict()
    record = execution.execute(verdict, approved(size=10))

    intent = execution.build_intent(verdict, approved(size=10))
    assert intent.client_order_id() == record.client_order_id

    execution._submit(record)
    assert len(client.orders) == 1


# -- rejection --------------------------------------------------------------


def test_client_error_is_a_terminal_rejection(execution, client):
    def refuse(*a, **kw):
        raise KalshiAPIError(400, "price out of range")

    client.place_order = refuse
    record = execution.execute(make_verdict(), approved(size=10))

    assert record.state is OrderState.REJECTED
    assert record.filled_count == 0
    assert record.pending_cost_cents == 0


# -- dry run ----------------------------------------------------------------


def test_dry_run_is_the_default_and_sends_nothing(client, order_store, account):
    from workers.execution import Execution

    # Not touching CONFIG.risk.dry_run: the point is that the shipped default
    # is paper mode.
    assert CONFIG.risk.dry_run is True
    # Paper mode goes through the same account-state gate as live mode, so
    # dry runs exercise the real path rather than a shortcut around it.
    account.reconcile()

    record = Execution(client, order_store, account).execute(make_verdict(), approved(size=10))

    assert record.state is OrderState.DRY_RUN
    assert record.dry_run is True
    assert client.place_order_calls == []
    assert record.filled_count == 0
    assert record.pending_cost_cents == 0, "paper orders must never reserve exposure"


# -- maker mode -------------------------------------------------------------


def test_maker_mode_is_refused_at_the_execution_chokepoint(execution):
    CONFIG.risk.order_strategy = "maker"
    with pytest.raises(UnmanagedMakerMode, match="not safe to run"):
        execution.execute(make_verdict(), approved(size=10))


def test_unknown_strategy_is_refused(execution):
    CONFIG.risk.order_strategy = "wishful"
    with pytest.raises(UnmanagedMakerMode):
        execution.execute(make_verdict(), approved(size=10))


def test_taker_mode_uses_ioc(execution, client):
    execution.execute(make_verdict(), approved(size=10))
    assert client.place_order_calls[0]["time_in_force"] == "IOC"


def test_execute_refuses_an_unapproved_decision(execution):
    with pytest.raises(ValueError):
        execution.execute(make_verdict(), RiskDecision(False, "nope"))


def test_execution_refuses_when_account_state_is_unverified(client, order_store):
    """Defense in depth: risk checks this too, but an order must never leave
    this process on the strength of an upstream component having checked."""
    from core.account_state import AccountState, ReconciliationError
    from workers.execution import Execution

    CONFIG.risk.dry_run = False
    account = AccountState(client, order_store)  # never reconciled
    execution = Execution(client, order_store, account)

    with pytest.raises(ReconciliationError):
        execution.execute(make_verdict(), approved(size=10))
    assert client.place_order_calls == []


def test_buying_no_prices_the_no_side(execution, client):
    verdict = make_verdict(maker_probability=0.10)
    assert verdict.proposal.direction == "no"

    execution.execute(verdict, approved(size=5, price=48.0))

    call = client.place_order_calls[0]
    assert call["side"] == "no"
    assert call["no_price_dollars"] == "0.48"
    assert call["yes_price_dollars"] is None
