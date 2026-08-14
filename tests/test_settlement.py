"""
P0-4: settlement from exchange-confirmed results, matched to fills.

The bugs these lock out:
  - inferring the outcome from PnL sign (a NO position resolving NO also
    shows positive PnL, so the old logic mislabelled it as a YES outcome and
    poisoned every Brier score);
  - applying one aggregate ticker PnL to every recent row;
  - giving up entirely when more than one row matched a ticker;
  - double-counting PnL when reconciliation runs twice.
"""
from __future__ import annotations

import pytest

from core.order_state import Fill

from tests.conftest import make_verdict
from tests.test_execution import approved


def store_fill(order_store, fill_id, ticker, side, count, price, fees=0.0,
               client_order_id="c1"):
    order_store.record_fills([
        Fill(fill_id=fill_id, ticker=ticker, count=count, price_cents=price,
             side=side, action="buy", fees_cents=fees,
             client_order_id=client_order_id, exchange_order_id="ord-1")
    ])


# -- outcome comes from the exchange, not from PnL sign --------------------


def test_yes_position_resolving_yes_pays_out(ledger, order_store, client):
    store_fill(order_store, "f1", "KXA-1", "yes", count=10, price=40.0)
    client.add_settlement("KXA-1", "yes")

    assert ledger.reconcile_settlements() == 1

    row = order_store.settlements_for_ticker("KXA-1")[0]
    assert row["settlement_result"] == "yes"
    # 10 contracts bought at 40c, settling at 100c: +$6.00
    assert row["realized_pnl"] == pytest.approx(6.0)


def test_no_position_resolving_no_is_a_win_not_a_yes_outcome(ledger, order_store, client):
    """The exact case the old heuristic got wrong: this position makes money,
    but the market resolved NO."""
    store_fill(order_store, "f1", "KXA-1", "no", count=10, price=30.0)
    client.add_settlement("KXA-1", "no")

    ledger.reconcile_settlements()

    row = order_store.settlements_for_ticker("KXA-1")[0]
    assert row["settlement_result"] == "no", "outcome must be the market's, not ours"
    assert row["side"] == "no"
    assert row["realized_pnl"] == pytest.approx(7.0)


def test_losing_position_loses_exactly_what_it_cost(ledger, order_store, client):
    store_fill(order_store, "f1", "KXA-1", "yes", count=10, price=40.0)
    client.add_settlement("KXA-1", "no")

    ledger.reconcile_settlements()

    row = order_store.settlements_for_ticker("KXA-1")[0]
    assert row["realized_pnl"] == pytest.approx(-4.0)


def test_fees_are_subtracted_from_realized_pnl(ledger, order_store, client):
    store_fill(order_store, "f1", "KXA-1", "yes", count=10, price=40.0, fees=50.0)
    client.add_settlement("KXA-1", "yes")

    ledger.reconcile_settlements()

    row = order_store.settlements_for_ticker("KXA-1")[0]
    # $6.00 gross less $0.50 of fees
    assert row["realized_pnl"] == pytest.approx(5.5)


# -- per-fill attribution ---------------------------------------------------


def test_two_fills_at_different_prices_settle_to_different_pnl(
    ledger, order_store, client
):
    """The old code applied one aggregate ticker PnL to whichever row it
    matched. Each fill has its own price and must settle on its own terms."""
    store_fill(order_store, "f1", "KXA-1", "yes", count=10, price=40.0,
               client_order_id="c1")
    store_fill(order_store, "f2", "KXA-1", "yes", count=10, price=90.0,
               client_order_id="c2")
    client.add_settlement("KXA-1", "yes")

    assert ledger.reconcile_settlements() == 2

    rows = {r["fill_id"]: r for r in order_store.settlements_for_ticker("KXA-1")}
    assert rows["f1"]["realized_pnl"] == pytest.approx(6.0)
    assert rows["f2"]["realized_pnl"] == pytest.approx(1.0)


def test_multiple_unsettled_fills_no_longer_stall_reconciliation(
    ledger, order_store, client
):
    """Previously >1 match on a ticker was left unsettled forever because it
    could not be attributed. Fill-level IDs remove the ambiguity."""
    for i in range(5):
        store_fill(order_store, f"f{i}", "KXA-1", "yes", count=1, price=50.0,
                   client_order_id=f"c{i}")
    client.add_settlement("KXA-1", "yes")

    assert ledger.reconcile_settlements() == 5
    assert order_store.unsettled_fills("KXA-1") == []


# -- idempotency ------------------------------------------------------------


def test_running_reconciliation_twice_does_not_double_count(
    ledger, order_store, client
):
    store_fill(order_store, "f1", "KXA-1", "yes", count=10, price=40.0)
    client.add_settlement("KXA-1", "yes")

    first = ledger.reconcile_settlements()
    second = ledger.reconcile_settlements()
    third = ledger.reconcile_settlements()

    assert (first, second, third) == (1, 0, 0)
    assert len(order_store.settlements_for_ticker("KXA-1")) == 1


def test_repeated_reconciliation_keeps_realized_pnl_stable(
    ledger, order_store, client
):
    """Daily PnL drives the kill switch, so a number that grows on every pass
    would trip it spuriously — or, with a winning day, never trip it at all."""
    store_fill(order_store, "f1", "KXA-1", "yes", count=10, price=40.0)
    client.add_settlement("KXA-1", "yes")

    for _ in range(4):
        ledger.reconcile_settlements()

    assert order_store.realized_pnl_since(0) == pytest.approx(6.0)


def test_unresolved_market_leaves_the_fill_open(ledger, order_store, client):
    store_fill(order_store, "f1", "KXA-1", "yes", count=10, price=40.0)
    # No settlement and no market result published yet.

    assert ledger.reconcile_settlements() == 0
    assert len(order_store.unsettled_fills("KXA-1")) == 1


def test_non_definitive_result_is_not_treated_as_an_outcome(
    ledger, order_store, client
):
    store_fill(order_store, "f1", "KXA-1", "yes", count=10, price=40.0)
    client.add_settlement("KXA-1", "")

    assert ledger.reconcile_settlements() == 0
    assert len(order_store.unsettled_fills("KXA-1")) == 1


# -- fallback path ----------------------------------------------------------


def test_falls_back_to_the_market_result_when_settlements_are_unavailable(
    ledger, order_store, client
):
    from core.kalshi_client import KalshiAPIError

    store_fill(order_store, "f1", "KXA-1", "yes", count=10, price=40.0)
    client.markets["KXA-1"] = {"ticker": "KXA-1", "result": "yes"}

    def unavailable(**kw):
        raise KalshiAPIError(404, "not available on this account")

    client.get_settlements = unavailable

    assert ledger.reconcile_settlements() == 1
    assert order_store.settlements_for_ticker("KXA-1")[0]["settlement_result"] == "yes"


# -- edge writeback ---------------------------------------------------------


def test_settlement_writes_back_to_the_edge_that_produced_it(
    execution, ledger, edge_store, order_store, client, account
):
    """End to end: decision -> order -> fill -> settlement -> calibration."""
    account.reconcile()
    verdict = make_verdict()
    decision = approved(size=10, price=40.0)
    edge_id = ledger.log_decision(verdict, decision)
    record = execution.execute(verdict, decision)
    ledger.record_execution(edge_id, record)

    client.add_settlement("KXTEST-25AUG14-A", "yes")
    ledger.reconcile_settlements()

    edge = [e for e in edge_store.recent_edges() if e["id"] == edge_id][0]
    assert edge["settled"] == 1
    assert edge["outcome"] == "yes"
    assert edge["pnl"] == pytest.approx(6.0)
    assert edge["action_taken"] == "executed"
    assert edge["entry_price"] == pytest.approx(0.40)
    assert edge["size_contracts"] == 10


def test_a_zero_fill_order_is_not_recorded_as_an_executed_trade(
    execution, ledger, edge_store, client, account
):
    """Calibration counts executed rows. An unfilled order that counted as a
    trade would pollute both the Brier score and the PnL history."""
    client.fill_plan = [0]
    account.reconcile()
    verdict = make_verdict()
    decision = approved(size=10)
    edge_id = ledger.log_decision(verdict, decision)
    record = execution.execute(verdict, decision)
    ledger.record_execution(edge_id, record)

    edge = [e for e in edge_store.recent_edges() if e["id"] == edge_id][0]
    assert edge["action_taken"] == "no_fill"
    assert edge["size_contracts"] == 0
    assert edge["entry_price"] is None
    assert edge_store.unsettled_executed_edges() == []


def test_partial_fill_records_only_the_filled_quantity(
    execution, ledger, edge_store, client, account
):
    client.fill_plan = [3]
    account.reconcile()
    verdict = make_verdict()
    decision = approved(size=10, price=40.0)
    edge_id = ledger.log_decision(verdict, decision)
    record = execution.execute(verdict, decision)
    ledger.record_execution(edge_id, record)

    edge = [e for e in edge_store.recent_edges() if e["id"] == edge_id][0]
    assert edge["action_taken"] == "executed"
    assert edge["size_contracts"] == 3


def test_a_refused_decision_is_logged_but_not_executed(ledger, edge_store):
    from workers.risk_guardrail import RiskDecision

    edge_id = ledger.log_decision(make_verdict(), RiskDecision(False, "too wide"))
    edge = [e for e in edge_store.recent_edges() if e["id"] == edge_id][0]
    assert edge["action_taken"] == "skipped_risk"
    assert edge["size_contracts"] == 0


def test_edge_settlement_is_idempotent(ledger, edge_store, order_store, client,
                                       execution, account):
    account.reconcile()
    verdict = make_verdict()
    decision = approved(size=10, price=40.0)
    edge_id = ledger.log_decision(verdict, decision)
    ledger.record_execution(edge_id, execution.execute(verdict, decision))

    client.add_settlement("KXTEST-25AUG14-A", "yes")
    for _ in range(3):
        ledger.reconcile_settlements()

    edge = [e for e in edge_store.recent_edges() if e["id"] == edge_id][0]
    assert edge["pnl"] == pytest.approx(6.0)
