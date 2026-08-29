"""
P0-5: risk rebuilt around worst-case dollar exposure.

The invariant under test:

    never submit an order when locally reconstructed worst-case exposure plus
    the proposed order exceeds the configured limit

plus the supporting requirements — real exchange balance rather than the CLI
value, pending orders counted as risk, per-ticker/event/category
concentration, fees and slippage, and a kill switch that survives a restart.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from core.account_state import AccountSnapshot, Position
from core.order_state import OrderIntent, OrderState
from workers.risk_guardrail import (
    KillSwitchTripped,
    RiskGuardrail,
    executable_price_cents,
    fee_cents_per_contract,
)

from tests.conftest import make_candidate, make_verdict


def snapshot(balance_cents=100_000.0, positions=None, open_orders=None,
             available=None) -> AccountSnapshot:
    import time

    return AccountSnapshot(
        balance_cents=balance_cents,
        available_balance_cents=balance_cents if available is None else available,
        positions=positions or [],
        open_orders=open_orders or [],
        reconciled_at=time.time(),
    )


# -- prices, fees ----------------------------------------------------------


def test_executable_price_is_the_side_we_actually_buy():
    c = make_candidate(yes_bid=48.0, yes_ask=52.0)
    assert executable_price_cents(c, "yes") == 52.0
    # Buying NO lifts the NO ask, which is 100 - yes_bid.
    assert executable_price_cents(c, "no") == 52.0


def test_fee_peaks_near_fifty_cents():
    assert fee_cents_per_contract(50.0) > fee_cents_per_contract(10.0)
    assert fee_cents_per_contract(50.0) > fee_cents_per_contract(90.0)
    assert fee_cents_per_contract(0.0) == 0.0


def test_sizing_budgets_slippage_and_fees(risk):
    decision = risk.evaluate(make_verdict(), snapshot())
    assert decision.approved
    # Limit price carries the slippage allowance above the executable price.
    assert decision.limit_price_cents == pytest.approx(
        decision.executable_price_cents + CONFIG.risk.slippage_cents
    )
    assert decision.estimated_fees_cents > 0
    per_contract = decision.projected_cost_cents / decision.size_contracts
    assert per_contract > decision.limit_price_cents, "fees must be in the budget"


# -- the core invariant -----------------------------------------------------


def test_order_is_refused_when_it_would_breach_total_exposure(risk):
    """Existing positions consume the same budget a new order draws on.

    Spread across four unrelated categories so no per-category or per-event
    cap binds first — this isolates the account-wide limit.
    """
    bankroll_cents = 100_000.0
    at_cap = bankroll_cents * CONFIG.risk.max_total_exposure_pct
    per_category = at_cap / 4
    positions = [
        Position(ticker=f"KX{cat}-1", side="yes",
                 quantity=int(per_category // 50), avg_price_cents=50.0,
                 event_ticker=f"KX{cat}", category=cat)
        for cat in ("Politics", "Crypto", "Economics", "Climate")
    ]

    decision = risk.evaluate(make_verdict(), snapshot(positions=positions))

    assert not decision.approved
    assert "total exposure" in decision.reason


def test_pending_orders_count_as_exposure(risk, order_store):
    """A resting or in-flight order is money the exchange can still take."""
    intent = OrderIntent(ticker="KXPEND-1", action="buy", side="yes",
                         count=1000, limit_price_cents=50.0, time_in_force="GTC")
    record = order_store.record_intent(intent)
    record.state = OrderState.OPEN
    order_store.update_order(record)
    live = order_store.get_order(record.client_order_id)
    # $500 reserved by a single resting order — the whole total-exposure cap.
    assert live.pending_cost_cents == pytest.approx(50_000.0)

    decision = risk.evaluate(make_verdict(), snapshot(open_orders=[live]))

    assert not decision.approved
    assert "total exposure" in decision.reason


def test_approved_size_never_breaches_the_cap_it_was_sized_under(risk):
    """Property check across a range of prices: the projected exposure after
    the order must always sit inside the total-exposure cap."""
    for ask in (5.0, 20.0, 50.0, 75.0, 95.0):
        bid = max(ask - 4.0, 1.0)
        candidate = make_candidate(yes_bid=bid, yes_ask=ask)
        verdict = make_verdict(candidate=candidate, maker_probability=0.99)
        decision = risk.evaluate(verdict, snapshot())
        if not decision.approved:
            continue
        cap = 100_000.0 * CONFIG.risk.max_total_exposure_pct
        assert decision.projected_exposure_cents <= cap + 1e-6, ask


def test_per_ticker_cap_binds_before_the_total(risk):
    decision = risk.evaluate(make_verdict(), snapshot())
    assert decision.approved
    # 5% of a $1000 bankroll = $50 on one ticker, against 50% total.
    assert decision.detail["binding_constraint"] == "max position size"
    assert decision.projected_cost_cents <= 100_000.0 * CONFIG.risk.max_ticker_exposure_pct


def test_per_event_cap_limits_correlated_markets(risk):
    """Two markets in one event are one bet on the same question."""
    event_cap = 100_000.0 * CONFIG.risk.max_event_exposure_pct
    positions = [
        Position(ticker="KXTEST-25AUG14-B", side="yes",
                 quantity=int(event_cap // 50), avg_price_cents=50.0,
                 event_ticker="KXTEST-25AUG14")
    ]

    decision = risk.evaluate(make_verdict(), snapshot(positions=positions))

    assert not decision.approved
    assert "event KXTEST-25AUG14" in decision.reason


def test_per_category_cap_limits_correlated_exposure(risk):
    category_cap = 100_000.0 * CONFIG.risk.max_category_exposure_pct
    positions = [
        Position(ticker="KXUNRELATED-1", side="yes",
                 quantity=int(category_cap // 50), avg_price_cents=50.0,
                 event_ticker="KXUNRELATED", category="Sports")
    ]

    decision = risk.evaluate(make_verdict(), snapshot(positions=positions))

    assert not decision.approved
    assert "category Sports" in decision.reason


def test_cannot_commit_more_than_the_account_can_pay(risk):
    decision = risk.evaluate(make_verdict(), snapshot(balance_cents=100_000.0,
                                                      available=30.0))
    assert not decision.approved
    assert "available balance" in decision.reason or "rounds to zero" in decision.reason


# -- bankroll ---------------------------------------------------------------


def test_real_exchange_balance_caps_the_cli_bankroll(edge_store, order_store):
    """--bankroll is an operator ceiling, not a source of capital. A $1000
    declared bankroll against a $20 account must size off the $20."""
    risk = RiskGuardrail(bankroll_usd=1000.0, store=edge_store, order_store=order_store)
    assert risk.effective_bankroll_usd(snapshot(balance_cents=2_000.0)) == 20.0


def test_cli_bankroll_can_only_reduce_risk(edge_store, order_store):
    risk = RiskGuardrail(bankroll_usd=50.0, store=edge_store, order_store=order_store)
    assert risk.effective_bankroll_usd(snapshot(balance_cents=100_000.0)) == 50.0


def test_empty_account_cannot_trade(risk):
    decision = risk.evaluate(make_verdict(), snapshot(balance_cents=0.0))
    assert not decision.approved
    assert "bankroll" in decision.reason


# -- stale / unusable state -------------------------------------------------


def test_stale_account_state_refuses_every_order(risk):
    import time

    snap = snapshot()
    snap.reconciled_at = time.time() - 10_000
    decision = risk.evaluate(make_verdict(), snap)
    assert not decision.approved
    assert "stale" in decision.reason


def test_unknown_orders_refuse_every_order(risk):
    snap = snapshot()
    snap.unknown_order_ids = ["abc123"]
    decision = risk.evaluate(make_verdict(), snap)
    assert not decision.approved
    assert "unknown state" in decision.reason


# -- kill switch ------------------------------------------------------------


def test_kill_switch_trips_on_realized_daily_loss(risk, order_store):
    order_store.record_settlement(
        settlement_key="k1", ticker="KXA-1", realized_pnl=-500.0,
        settled_at=__import__("time").time(), fill_id=None,
    )
    with pytest.raises(KillSwitchTripped):
        risk.evaluate(make_verdict(), snapshot())


def test_kill_switch_survives_a_restart(edge_store, order_store):
    """An in-memory flag would un-trip itself on the next Railway redeploy."""
    risk = RiskGuardrail(1000.0, store=edge_store, order_store=order_store)
    order_store.record_settlement(
        settlement_key="k1", ticker="KXA-1", realized_pnl=-500.0,
        settled_at=__import__("time").time(), fill_id=None,
    )
    with pytest.raises(KillSwitchTripped):
        risk.evaluate(make_verdict(), snapshot())

    restarted = RiskGuardrail(1000.0, store=edge_store, order_store=order_store)
    with pytest.raises(KillSwitchTripped):
        restarted.evaluate(make_verdict(), snapshot())


def test_kill_switch_reset_is_manual(edge_store, order_store):
    risk = RiskGuardrail(1000.0, store=edge_store, order_store=order_store)
    risk.store.set_kill_switch(True, "test")
    risk._killed = True
    risk.reset_kill_switch()
    assert not edge_store.load_kill_switch()["tripped"]


def test_realized_losses_shrink_how_much_new_risk_is_allowed(risk, order_store):
    """A bad morning tightens the afternoon: $80 already lost against a $100
    daily limit leaves $20 of new risk, well under the $50 position cap."""
    import time

    order_store.record_settlement(
        settlement_key="k1", ticker="KXA-1", realized_pnl=-80.0,
        settled_at=time.time(), fill_id=None,
    )

    decision = risk.evaluate(make_verdict(), snapshot())

    assert decision.approved
    assert decision.detail["binding_constraint"] == "daily loss budget"
    assert decision.projected_cost_cents <= 2_000.0 + 1e-6


def test_new_risk_is_refused_once_the_daily_budget_is_gone(risk, order_store):
    """Just under the kill-switch threshold, so trading is not halted — but
    there is no budget left to open anything new."""
    import time

    order_store.record_settlement(
        settlement_key="k1", ticker="KXA-1", realized_pnl=-99.9,
        settled_at=time.time(), fill_id=None,
    )

    decision = risk.evaluate(make_verdict(), snapshot())

    assert not decision.approved
    assert "daily loss budget" in decision.reason


def test_total_exposure_cap_is_reachable_under_the_daily_limit(risk):
    """Regression: an earlier version counted all open exposure as a same-day
    loss, so a 10% daily limit made the 50% total-exposure cap unreachable and
    every order was refused once exposure passed 10%. The two limits govern
    different things and must not collapse into one."""
    positions = [Position(ticker="KXOTHER-1", side="yes", quantity=400,
                          avg_price_cents=50.0, event_ticker="KXOTHER",
                          category="Politics")]
    snap = snapshot(positions=positions)
    assert snap.worst_case_exposure_cents() == pytest.approx(20_000.0)

    decision = risk.evaluate(make_verdict(), snap)

    assert decision.approved, decision.reason


# -- checker and market-quality gates --------------------------------------


def test_unapproved_checker_verdict_is_refused(risk):
    decision = risk.evaluate(make_verdict(verdict="reject"), snapshot())
    assert not decision.approved
    assert "Checker did not approve" in decision.reason


def test_low_checker_confidence_is_refused(risk):
    decision = risk.evaluate(make_verdict(confidence=0.10), snapshot())
    assert not decision.approved


def test_wide_spread_is_refused(risk):
    candidate = make_candidate(yes_bid=30.0, yes_ask=60.0)
    decision = risk.evaluate(make_verdict(candidate=candidate), snapshot())
    assert not decision.approved
    assert "spread" in decision.reason


def test_thin_market_is_refused(risk):
    candidate = make_candidate(volume=10.0)
    decision = risk.evaluate(make_verdict(candidate=candidate), snapshot())
    assert not decision.approved
    assert "volume" in decision.reason


def test_longshot_guard_applies_to_the_no_side_too(risk):
    """A NO contract at 15c is exactly as much a longshot as a YES at 15c.
    The previous version only checked YES."""
    candidate = make_candidate(yes_bid=85.0, yes_ask=88.0)
    # Market implies 86.5%; Maker says 83%, so we would buy NO at 15c on a
    # 3.5pp edge — under the 6pp a sub-20c contract has to clear.
    verdict = make_verdict(candidate=candidate, maker_probability=0.83)
    assert verdict.proposal.direction == "no"
    assert executable_price_cents(candidate, "no") == 15.0

    decision = risk.evaluate(verdict, snapshot())

    assert not decision.approved
    assert "Longshot bias guard" in decision.reason


def test_longshot_with_a_big_enough_edge_still_passes(risk):
    candidate = make_candidate(yes_bid=10.0, yes_ask=14.0)
    verdict = make_verdict(candidate=candidate, maker_probability=0.60)
    decision = risk.evaluate(verdict, snapshot())
    assert decision.approved


def test_nonsensical_price_is_refused(risk):
    candidate = make_candidate(yes_bid=100.0, yes_ask=100.0)
    verdict = make_verdict(candidate=candidate, maker_probability=0.99)
    decision = risk.evaluate(verdict, snapshot())
    assert not decision.approved


def test_max_open_positions_still_caps_attention(risk):
    positions = [
        Position(ticker=f"KX{i}-1", side="yes", quantity=1, avg_price_cents=1.0)
        for i in range(CONFIG.risk.max_open_positions)
    ]
    decision = risk.evaluate(make_verdict(), snapshot(positions=positions))
    assert not decision.approved
    assert "max open positions" in decision.reason


# ---------------------------------------------------------------------------
# PF-09 reads one mode at a time
# ---------------------------------------------------------------------------
#
# calibration_by_category groups by (category, source, mode) and its own
# docstring says the three are not comparable. PF-09 keyed its lookup on
# (category, source) alone, so the dict comprehension kept whichever mode
# SQLite returned last — the refused bucket. A category losing money on the
# trades it TOOK was masked by the counterfactual results of the trades it
# DECLINED, and the gate did not fire.
#
# It was latent only because every row was refused while the balance was
# zero. The first funded pass creates paper rows and arms it.

from memory.edge_store import EdgeRecord  # noqa: E402


def _graded(store, action, pnl, n, category="Crypto", source="quant",
            outcome="yes", probability=0.70):
    for _ in range(n):
        edge_id = store.record_edge(EdgeRecord(
            ticker="KXBTCD-1", category=category, source=source,
            maker_probability=probability, market_implied_probability=0.40,
            edge_size=0.30, action_taken=action,
            counterfactual_price_cents=40.0, counterfactual_direction="yes",
        ))
        store.settle(edge_id, outcome, pnl)


def _pf09(edge_store, order_store, source="quant", category="Crypto"):
    risk = RiskGuardrail(1000.0, store=edge_store, order_store=order_store)
    from tests.conftest import make_candidate, make_verdict
    verdict = make_verdict(
        candidate=make_candidate(category=category), source=source
    )
    return risk._pf09_category_calibration(verdict)


def test_losing_paper_trades_are_not_masked_by_refused_winners(
        edge_store, order_store):
    """The regression. This is the state the first funded pass creates."""
    _graded(edge_store, "dry_run", -5.0, 12)        # what we took: losing
    _graded(edge_store, "skipped_risk", +1.0, 50)   # what we declined: winners

    decision = _pf09(edge_store, order_store)

    assert not decision.approved, (
        "PF-09 must judge the trades taken, not the ones refused"
    )
    assert "12" in decision.reason, "it must cite the paper bucket, n=12"


def test_real_money_outranks_paper(edge_store, order_store):
    _graded(edge_store, "executed", -3.0, 15)
    _graded(edge_store, "dry_run", +9.0, 40)

    decision = _pf09(edge_store, order_store)

    assert not decision.approved
    assert "15" in decision.reason, "live is the best evidence available"


def test_refused_is_still_used_when_it_is_the_only_evidence(
        edge_store, order_store):
    """Today's regime, and it must not change.

    With a zero balance every row is refused. That is the only signal there
    is, so the gate still reads it — the fix narrows which bucket is chosen,
    it does not stop the gate working before the account is funded.
    """
    _graded(edge_store, "skipped_risk", -2.0, 30)

    assert not _pf09(edge_store, order_store).approved


def test_a_thin_bucket_does_not_displace_a_populated_one(
        edge_store, order_store):
    """Preference is by mode, but only among buckets with enough rows.

    Three live trades must not silence a paper bucket of forty, or a single
    lucky fill would switch the gate off.
    """
    _graded(edge_store, "executed", +50.0, 3)       # tiny, flattering
    _graded(edge_store, "dry_run", -4.0, 40)        # substantial, losing

    decision = _pf09(edge_store, order_store)

    assert not decision.approved
    assert "40" in decision.reason


def test_a_profitable_category_still_passes(edge_store, order_store):
    """The gate must not just always refuse."""
    _graded(edge_store, "dry_run", +4.0, 40, probability=0.7, outcome="yes")

    assert _pf09(edge_store, order_store).approved
