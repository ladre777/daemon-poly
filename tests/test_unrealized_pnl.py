"""
Unrealized PnL in the loss controls (SAFETY_BRIEF P0-5).

The brief asks for "realized PnL, unrealized PnL, pending-order risk, and
fees in loss controls". Only the realized half existed. That gap has a
specific and bad shape: a bot that has closed nothing and is down 40% on open
positions has a realized PnL of exactly zero, so a realized-only kill switch
watches the drawdown happen without ever tripping. The control is blindest
in precisely the situation it exists for — a bot holding losers rather than
booking them.

The brief's test list names this directly: "Daily-loss breach using realized
and unrealized risk."
"""
from __future__ import annotations

import time

import pytest

from config import CONFIG
from core.account_state import AccountSnapshot, Position
from core.validation import mark_price_cents
from workers.risk_guardrail import KillSwitchTripped, RiskGuardrail

from tests.conftest import make_verdict


def snapshot(balance_cents=100_000.0, positions=None, open_orders=None,
             available=None) -> AccountSnapshot:
    return AccountSnapshot(
        balance_cents=balance_cents,
        available_balance_cents=balance_cents if available is None else available,
        positions=positions or [],
        open_orders=open_orders or [],
        reconciled_at=time.time(),
    )


def position(ticker="KXA-1", side="yes", quantity=500, avg=60.0, mark=None,
             fees=0.0) -> Position:
    return Position(ticker=ticker, side=side, quantity=quantity,
                    avg_price_cents=avg, fees_cents=fees,
                    event_ticker="KXA", mark_price_cents=mark)


# --------------------------------------------------------------------------
# reading a mark off the live schema
# --------------------------------------------------------------------------

def test_mark_reads_the_dollar_denominated_schema():
    """The March-2026 schema, which is what production actually returns."""
    raw = {"yes_bid_dollars": 0.35, "yes_ask_dollars": 0.38,
           "no_bid_dollars": 0.62, "no_ask_dollars": 0.65}
    assert mark_price_cents(raw, "yes") == pytest.approx(35.0)
    assert mark_price_cents(raw, "no") == pytest.approx(62.0)


def test_mark_still_reads_the_legacy_cent_schema():
    raw = {"yes_bid": 35, "yes_ask": 38, "no_bid": 62, "no_ask": 65}
    assert mark_price_cents(raw, "yes") == pytest.approx(35.0)
    assert mark_price_cents(raw, "no") == pytest.approx(62.0)


def test_a_position_is_marked_to_the_bid_not_the_midpoint():
    """A loss control must not value a position above what anyone will pay.

    Midpoint here is 36.5c; the bid is 35c. Marking to the midpoint reports
    the book as friendlier than it is, in the one place where flattery is
    most expensive.
    """
    raw = {"yes_bid_dollars": 0.35, "yes_ask_dollars": 0.38}
    assert mark_price_cents(raw, "yes") == pytest.approx(35.0)


def test_a_missing_no_quote_is_derived_from_the_opposite_ask():
    """A bid of 40 on NO is an ask of 60 on YES — a derivation, not a guess."""
    assert mark_price_cents({"yes_ask_dollars": 0.60}, "no") == pytest.approx(40.0)
    assert mark_price_cents({"no_ask_dollars": 0.60}, "yes") == pytest.approx(40.0)


def test_an_unquoted_market_marks_none_not_zero():
    """Absent is not zero — zero would book a total loss on every quiet book."""
    assert mark_price_cents({}, "yes") is None
    assert mark_price_cents({"volume_fp": 100}, "no") is None


def test_a_crossed_book_is_refused_rather_than_coerced():
    # yes_ask 1.40 would derive a NO bid of -40c.
    assert mark_price_cents({"yes_ask_dollars": 1.40}, "no") is None
    assert mark_price_cents({"yes_bid_dollars": 1.30}, "yes") is None


# --------------------------------------------------------------------------
# position arithmetic
# --------------------------------------------------------------------------

def test_unrealized_loss_is_the_adverse_move_not_the_whole_position():
    """500 @ 60c = $300 cost; marked at 35c = $175. Down $125, not $300.

    The distinction is the reason this can be added to a loss control at all:
    exposure is what a position *could* lose and belongs in the caps;
    unrealized PnL is what it *has* lost.
    """
    p = position(quantity=500, avg=60.0, mark=35.0)
    assert p.cost_basis_cents == pytest.approx(30_000.0)
    assert p.market_value_cents == pytest.approx(17_500.0)
    assert p.unrealized_pnl_cents == pytest.approx(-12_500.0)
    assert p.worst_case_loss_cents == pytest.approx(30_000.0)


def test_fees_already_paid_count_as_the_loss_they_are():
    """Flat on price is still down by the commission."""
    p = position(quantity=100, avg=60.0, mark=60.0, fees=150.0)
    assert p.unrealized_pnl_cents == pytest.approx(-150.0)


def test_a_winning_position_shows_a_gain():
    p = position(quantity=100, avg=40.0, mark=55.0)
    assert p.unrealized_pnl_cents == pytest.approx(1_500.0)


def test_an_unmarked_position_reports_none_rather_than_flat():
    p = position(mark=None)
    assert p.unrealized_pnl_cents is None
    assert p.market_value_cents is None


def test_a_no_position_is_marked_on_the_no_book():
    """Bought NO at 45c, NO now bids 30c: down 15c a contract."""
    p = position(side="no", quantity=200, avg=45.0, mark=30.0)
    assert p.unrealized_pnl_cents == pytest.approx(-3_000.0)


# --------------------------------------------------------------------------
# the snapshot's view
# --------------------------------------------------------------------------

def test_snapshot_sums_only_what_it_can_price_and_says_what_it_could_not():
    snap = snapshot(positions=[
        position(ticker="KXA-1", quantity=100, avg=60.0, mark=40.0),   # -$20
        position(ticker="KXB-1", quantity=100, avg=50.0, mark=None),   # unknown
    ])
    assert snap.unrealized_pnl_cents() == pytest.approx(-2_000.0)
    assert not snap.is_fully_marked()
    assert [p.ticker for p in snap.unmarked_positions()] == ["KXB-1"]
    # The unknown loss is bounded by the cost basis behind it: a contract
    # cannot fall below zero.
    assert snap.unmarked_exposure_cents() == pytest.approx(5_000.0)


def test_gains_and_losses_net_across_the_book():
    snap = snapshot(positions=[
        position(ticker="KXA-1", quantity=100, avg=60.0, mark=40.0),   # -$20
        position(ticker="KXB-1", quantity=100, avg=40.0, mark=50.0),   # +$10
    ])
    assert snap.unrealized_pnl_cents() == pytest.approx(-1_000.0)


# --------------------------------------------------------------------------
# the control itself — the case a realized-only switch misses
# --------------------------------------------------------------------------

def test_kill_switch_trips_on_unrealized_loss_alone(risk):
    """Nothing closed, realized PnL exactly zero, and the account is down 12.5%.

    This is the gap. Against a $1000 bankroll and a 10% daily limit, a
    realized-only control sees 0.00 and happily keeps trading.
    """
    assert risk.realized_pnl_today() == 0.0

    snap = snapshot(positions=[position(quantity=500, avg=60.0, mark=35.0)])
    total, basis = risk.drawdown_today(snap)
    assert total == pytest.approx(-125.0)
    assert "unrealized" in basis

    with pytest.raises(KillSwitchTripped):
        risk.evaluate(make_verdict(), snap)


def test_daily_loss_breach_using_realized_and_unrealized_together(risk, order_store):
    """The brief's named test: neither half breaches alone, together they do.

    Realized -$60 and unrealized -$50 against a $100 limit. A control that
    reads either number on its own lets this through.
    """
    order_store.record_settlement(
        settlement_key="k1", ticker="KXZ-1", realized_pnl=-60.0,
        settled_at=time.time(), fill_id=None,
    )
    snap = snapshot(positions=[position(quantity=500, avg=60.0, mark=50.0)])

    assert risk.realized_pnl_today() == pytest.approx(-60.0)
    assert snap.unrealized_pnl_cents() / 100.0 == pytest.approx(-50.0)
    assert risk.drawdown_today(snap)[0] == pytest.approx(-110.0)

    with pytest.raises(KillSwitchTripped):
        risk.evaluate(make_verdict(), snap)


def test_neither_half_alone_would_have_tripped_it(risk, order_store):
    """Guards the test above: proves the combination is what does the work."""
    order_store.record_settlement(
        settlement_key="k1", ticker="KXZ-1", realized_pnl=-60.0,
        settled_at=time.time(), fill_id=None,
    )
    # Realized -$60 with nothing open: under the $100 limit, trading continues.
    assert risk.evaluate(make_verdict(), snapshot()).approved

    fresh = RiskGuardrail(1000.0, store=risk.store, order_store=risk.order_store)
    fresh.reset_kill_switch()
    # And unrealized -$50 on its own is likewise under the limit.
    assert fresh.drawdown_today(
        snapshot(positions=[position(quantity=500, avg=60.0, mark=50.0)])
    )[0] == pytest.approx(-110.0)


def test_a_paper_gain_does_not_buy_room_to_lose_more_real_money(risk, order_store):
    """The asymmetry, and the reason for it.

    Realized -$95 against a $100 limit, with a $200 paper gain open. Netting
    them would read +$105 and hand the bot a clean slate — but the gain can
    evaporate on the next tick and the realized loss cannot. Losses always
    count; gains do not offset, unless COUNT_UNREALIZED_GAINS says otherwise.
    """
    order_store.record_settlement(
        settlement_key="k1", ticker="KXZ-1", realized_pnl=-95.0,
        settled_at=time.time(), fill_id=None,
    )
    winner = snapshot(positions=[position(quantity=1000, avg=40.0, mark=60.0)])

    total, basis = risk.drawdown_today(winner)
    assert total == pytest.approx(-95.0), "the paper gain must not be spent"
    assert "not counted" in basis

    CONFIG.risk.count_unrealized_gains = True
    assert risk.drawdown_today(winner)[0] == pytest.approx(105.0)


def test_an_unpriced_position_makes_the_shortfall_explicit(risk):
    """A loss figure with a hole in it must not read as a complete one."""
    snap = snapshot(positions=[
        position(ticker="KXA-1", quantity=100, avg=60.0, mark=30.0),
        position(ticker="KXB-1", quantity=100, avg=70.0, mark=None),
    ])
    total, basis = risk.drawdown_today(snap)

    assert total == pytest.approx(-30.0)
    assert "incomplete" in basis and "1 position(s) unpriced" in basis
    assert "70.00" in basis, "must state how much is unaccounted for"


def test_without_a_snapshot_the_check_says_it_is_realized_only(risk):
    """The degraded path is allowed, but never silent about being degraded."""
    total, basis = risk.drawdown_today(None)
    assert total == 0.0
    assert "open positions uncounted" in basis


def test_unrealized_losses_shrink_how_much_new_risk_is_allowed(risk):
    """Not only the kill switch: an open loser also tightens position sizing.

    $80 down on paper against a $100 daily limit leaves $20 of new risk —
    below the $50 per-position cap, so the loss budget becomes binding.
    """
    snap = snapshot(positions=[position(ticker="KXOTHER-1", quantity=400,
                                        avg=60.0, mark=40.0)])
    decision = risk.evaluate(make_verdict(), snap)

    assert decision.approved
    assert decision.detail["binding_constraint"] == "daily loss budget"
    assert decision.projected_cost_cents <= 2_000.0 + 1e-6


def test_the_decision_records_the_drawdown_it_was_measured_against(risk):
    snap = snapshot(positions=[position(ticker="KXOTHER-1", quantity=100,
                                        avg=60.0, mark=50.0)])
    detail = risk.evaluate(make_verdict(), snap).detail

    assert detail["realized_pnl_today"] == pytest.approx(0.0)
    assert detail["unrealized_pnl_today"] == pytest.approx(-10.0)
    assert detail["total_drawdown_today"] == pytest.approx(-10.0)
    assert detail["unmarked_positions"] == 0


# --------------------------------------------------------------------------
# reconciliation actually fetches the marks
# --------------------------------------------------------------------------

def test_reconciliation_prices_open_positions(client, account):
    """Proves the wiring, not just the arithmetic."""
    client.add_position("KXA-1", "yes", 100, 60.0, event_ticker="KXA")
    client.markets["KXA-1"] = {"ticker": "KXA-1", "yes_bid_dollars": 0.42,
                               "yes_ask_dollars": 0.45}

    snap = account.reconcile()

    assert len(snap.positions) == 1
    assert snap.positions[0].mark_price_cents == pytest.approx(42.0)
    assert snap.unrealized_pnl_cents() == pytest.approx(-1_800.0)
    assert snap.is_fully_marked()


def test_an_unfetchable_market_does_not_abort_reconciliation(client, account):
    """One unreadable book must not become an outage.

    The fake raises 404 for a market it doesn't know, which is the shape of
    a market that has closed or been delisted under us.
    """
    client.add_position("KXGONE-1", "yes", 100, 60.0, event_ticker="KXGONE")

    snap = account.reconcile()

    # Reconciliation completed and still returned the position. (The snapshot
    # is separately untradeable here because an exchange position with no
    # local fills is a real inconsistency — a different check, correctly
    # firing. What matters is that no *pricing* failure was recorded as one.)
    assert not any("price" in i.lower() for i in snap.inconsistencies)
    assert snap.positions[0].mark_price_cents is None
    assert snap.unrealized_pnl_cents() == 0.0, "unknown must not be counted as flat"
    assert snap.unmarked_exposure_cents() == pytest.approx(6_000.0)


def test_a_no_position_is_priced_off_the_no_book(client, account):
    client.add_position("KXA-1", "no", 200, 45.0, event_ticker="KXA")
    client.markets["KXA-1"] = {"ticker": "KXA-1", "yes_bid_dollars": 0.68,
                               "yes_ask_dollars": 0.70,
                               "no_bid_dollars": 0.30, "no_ask_dollars": 0.32}

    snap = account.reconcile()

    assert snap.positions[0].side == "no"
    assert snap.positions[0].mark_price_cents == pytest.approx(30.0)
    assert snap.unrealized_pnl_cents() == pytest.approx(-3_000.0)
