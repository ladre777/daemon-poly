"""
Mechanisms ported from the open-source Kalshi bot survey.

Three things, each independently toggleable:

1. Fractional Kelly sizing (core/kelly.py) — every surveyed bot sizes by edge
   magnitude; this one sized purely by headroom.
2. Locked-arb detection (workers/arbitrage.py) — the only model-free edge in
   the survey, reimplemented with real fees because the source repo's flat
   "< 98c" test is a loss at the prices where these actually occur.
3. The crypto settlement blackout — a consequence of confirming that Kalshi's
   crypto contracts settle on a 60-second RTI average, not a spot print.
"""
from __future__ import annotations

import time

import pytest

from config import CONFIG
from core.account_state import AccountSnapshot
from core.kelly import kelly_cap_cents, kelly_fraction, win_probability_for
from core.pricing import fee_cents_per_contract
from workers.arbitrage import ArbitrageScanner, find_locked_arb

from tests.conftest import make_candidate, make_verdict


def snapshot(balance_cents=100_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        balance_cents=balance_cents,
        available_balance_cents=balance_cents,
        reconciled_at=time.time(),
    )


# --------------------------------------------------------------------------
# Kelly arithmetic
# --------------------------------------------------------------------------

def test_kelly_is_computed_on_fee_inclusive_odds():
    """The hard constraint: no edge is declared before fees.

    A contract at 50c with a 1.75c fee costs 51.75c, so the odds are worse
    than the sticker price implies. Sizing off the raw price overstates them
    and oversizes every position — the exact failure fractional Kelly exists
    to prevent.
    """
    raw = kelly_fraction(0.60, 50.0)
    with_fees = kelly_fraction(0.60, 50.0 + fee_cents_per_contract(50.0))
    assert with_fees < raw, "fees must make Kelly smaller, never larger"


def test_kelly_is_zero_for_a_bet_with_no_edge():
    """Kelly's answer to a coin flip priced at a coin flip is 'don't'."""
    assert kelly_fraction(0.50, 50.0) == 0.0
    assert kelly_fraction(0.40, 50.0) == 0.0, "a losing bet must never size up"


def test_kelly_grows_with_edge():
    small = kelly_fraction(0.55, 50.0)
    large = kelly_fraction(0.80, 50.0)
    assert 0 < small < large


def test_kelly_refuses_impossible_prices():
    assert kelly_fraction(0.9, 0.0) == 0.0
    assert kelly_fraction(0.9, 100.0) == 0.0, "paying the full payout cannot win"
    assert kelly_fraction(0.9, 120.0) == 0.0
    assert kelly_fraction(0.0, 50.0) == 0.0
    assert kelly_fraction(1.0, 50.0) == 0.0


def test_a_known_value():
    """p=0.6 at 50c: b = 50/50 = 1, so f* = 0.6 - 0.4/1 = 0.2."""
    assert kelly_fraction(0.60, 50.0) == pytest.approx(0.20)


def test_the_no_side_uses_the_opposite_probability():
    """Buying NO wins when YES does not.

    Reading this backwards would size NO trades by the probability of the
    thing that makes them worthless.
    """
    assert win_probability_for(0.30, "yes") == pytest.approx(0.30)
    assert win_probability_for(0.30, "no") == pytest.approx(0.70)


def test_the_fraction_scales_the_budget():
    full = kelly_cap_cents(0.60, 50.0, 100_000.0, 1.0)
    quarter = kelly_cap_cents(0.60, 50.0, 100_000.0, 0.25)
    assert quarter == pytest.approx(full * 0.25)


# --------------------------------------------------------------------------
# Kelly inside the guardrail — the part that must not weaken anything
# --------------------------------------------------------------------------

def test_kelly_can_only_shrink_a_position_never_grow_one(risk):
    """The safety property that makes this safe to default on.

    Kelly enters as one more gate among the concentration caps, so the
    smallest still wins. A wildly optimistic Kelly must not be able to lift
    the position past MAX_POSITION_PCT.
    """
    CONFIG.risk.kelly_enabled = False
    without = risk.evaluate(make_verdict(maker_probability=0.99), snapshot())

    risk.reset_kill_switch()
    CONFIG.risk.kelly_enabled = True
    CONFIG.risk.kelly_fraction = 1.0        # deliberately reckless
    with_kelly = risk.evaluate(make_verdict(maker_probability=0.99), snapshot())

    assert without.approved and with_kelly.approved
    assert with_kelly.size_contracts <= without.size_contracts, (
        "Kelly must never size above what the concentration caps allow"
    )


def test_a_thin_edge_is_sized_smaller_than_a_fat_one(risk):
    """The actual point of the change.

    Sizing by headroom alone spends the same budget on a 4pp edge as on a
    40pp one. These two proposals differ only in the model's probability.
    """
    CONFIG.risk.kelly_enabled = True
    CONFIG.risk.kelly_fraction = 0.25

    thin = risk.evaluate(make_verdict(maker_probability=0.60), snapshot())
    risk.reset_kill_switch()
    fat = risk.evaluate(make_verdict(maker_probability=0.95), snapshot())

    assert thin.approved and fat.approved
    assert thin.size_contracts < fat.size_contracts
    assert thin.detail["binding_constraint"] == "kelly cap"


def test_disabling_kelly_restores_the_previous_sizing(risk):
    """The toggle has to actually toggle."""
    CONFIG.risk.kelly_enabled = False
    decision = risk.evaluate(make_verdict(maker_probability=0.60), snapshot())
    assert decision.approved
    assert decision.detail["binding_constraint"] != "kelly cap"


# --------------------------------------------------------------------------
# locked arbitrage
# --------------------------------------------------------------------------

def test_the_source_repos_threshold_would_have_lost_money():
    """kalshibot buys when YES+NO < 98c, calling it '~2% after fees'.

    At 49c/49c the two legs carry ~1.75c of fee each. 98c + 3.5c = 101.5c for
    something that pays 100c: a 1.5c loss per pair, booked as a 2c profit.
    This is the whole reason the port recomputes fees rather than trusting
    the flat allowance.
    """
    yes = no = 48.9
    fees = fee_cents_per_contract(yes) + fee_cents_per_contract(no)
    assert yes + no < 98.0, "clears the source repo's 'combined < 98c' test"
    assert yes + no + fees > 100.0, "but loses money once fees are real"
    assert find_locked_arb("KXA-1", yes, no) is None, (
        "so this implementation must refuse it"
    )


def test_a_genuine_arb_is_found_and_priced_after_fees():
    """Wide enough to clear both fees: 45c + 45c + ~1.7c*2 = ~93.5c."""
    arb = find_locked_arb("KXA-1", 45.0, 45.0)

    assert arb is not None
    expected_fees = fee_cents_per_contract(45.0) * 2
    assert arb.total_cost_cents == pytest.approx(90.0 + expected_fees)
    assert arb.profit_cents == pytest.approx(100.0 - 90.0 - expected_fees)
    assert arb.profit_cents > 0


def test_profit_is_the_payout_minus_everything_paid():
    arb = find_locked_arb("KXA-1", 40.0, 40.0)
    assert arb is not None
    assert arb.profit_cents == pytest.approx(100.0 - arb.total_cost_cents)
    assert arb.profit_per_pair_pct == pytest.approx(
        arb.profit_cents / arb.total_cost_cents
    )


def test_no_arb_when_the_book_is_priced_normally():
    """The overwhelmingly common case: both asks sum above parity."""
    assert find_locked_arb("KXA-1", 52.0, 52.0) is None


def test_a_profit_below_the_floor_is_not_reported():
    CONFIG.arbitrage.min_profit_cents = 5.0
    assert find_locked_arb("KXA-1", 48.0, 48.0) is None, "~1c profit, 5c floor"
    CONFIG.arbitrage.min_profit_cents = 0.5
    assert find_locked_arb("KXA-1", 48.0, 48.0) is not None


def test_never_returns_a_zero_or_negative_opportunity():
    """None rather than an unprofitable LockedArb, so a caller cannot act on
    one by forgetting to check the sign."""
    for yes, no in ((50.0, 50.0), (60.0, 60.0), (99.0, 99.0)):
        result = find_locked_arb("KXA-1", yes, no)
        assert result is None or result.profit_cents > 0


def test_missing_or_nonsensical_quotes_are_refused():
    assert find_locked_arb("KXA-1", None, 45.0) is None
    assert find_locked_arb("KXA-1", 45.0, None) is None
    assert find_locked_arb("KXA-1", 0.0, 45.0) is None
    assert find_locked_arb("KXA-1", 45.0, 100.0) is None
    assert find_locked_arb("KXA-1", -5.0, 45.0) is None


def test_pairs_are_capped():
    CONFIG.arbitrage.max_pairs = 10
    arb = find_locked_arb("KXA-1", 45.0, 45.0, available_pairs=1000)
    assert arb.max_pairs == 10
    assert arb.total_profit_cents == pytest.approx(arb.profit_cents * 10)


# --------------------------------------------------------------------------
# the scanner
# --------------------------------------------------------------------------

class Notifier:
    def __init__(self):
        self.arbs = []

    def notify_locked_arb(self, arb):
        self.arbs.append(arb.ticker)

    def __getattr__(self, _name):
        return lambda *a, **kw: None


def test_the_scanner_is_off_unless_enabled():
    """New order-adjacent behaviour must be opt-in."""
    CONFIG.arbitrage.enabled = False
    scanner = ArbitrageScanner(notifier=Notifier())
    # yes_bid 55 -> derived NO ask 45; yes_ask 45. A real arb.
    candidates = [make_candidate(yes_bid=55.0, yes_ask=45.0)]
    assert scanner.scan(candidates) == []


def test_the_scanner_finds_and_alerts(caplog):
    CONFIG.arbitrage.enabled = True
    CONFIG.arbitrage.min_profit_cents = 0.5
    notifier = Notifier()
    scanner = ArbitrageScanner(notifier=notifier)

    candidates = [make_candidate(ticker="KXARB-1", yes_bid=55.0, yes_ask=45.0)]
    with caplog.at_level("WARNING"):
        found = scanner.scan(candidates)

    assert len(found) == 1
    assert found[0].ticker == "KXARB-1"
    assert notifier.arbs == ["KXARB-1"]
    assert "LOCKED ARB" in caplog.text


def test_a_standing_arb_alerts_once_per_pass():
    """Same lesson as the standing-signal alerts: one message, not one a scan."""
    CONFIG.arbitrage.enabled = True
    CONFIG.arbitrage.min_profit_cents = 0.5
    notifier = Notifier()
    scanner = ArbitrageScanner(notifier=notifier)
    candidates = [make_candidate(ticker="KXARB-1", yes_bid=55.0, yes_ask=45.0)]

    scanner.scan(candidates)
    scanner.scan(candidates)
    assert notifier.arbs == ["KXARB-1"], "repeat within a pass must be silent"

    scanner.begin_pass()
    scanner.scan(candidates)
    assert notifier.arbs == ["KXARB-1", "KXARB-1"], "a new pass may speak again"


def test_results_are_ranked_best_first():
    """brandononchain sorts signals by edge magnitude; the biggest locked
    profit should be the one an operator sees first."""
    CONFIG.arbitrage.enabled = True
    CONFIG.arbitrage.min_profit_cents = 0.5
    scanner = ArbitrageScanner()
    found = scanner.scan([
        make_candidate(ticker="KXSMALL", yes_bid=53.0, yes_ask=46.0),
        make_candidate(ticker="KXBIG", yes_bid=65.0, yes_ask=30.0),
    ])
    assert [a.ticker for a in found] == ["KXBIG", "KXSMALL"]


def test_an_alert_failure_does_not_kill_the_scan():
    class Broken:
        def notify_locked_arb(self, arb):
            raise RuntimeError("telegram down")

    CONFIG.arbitrage.enabled = True
    CONFIG.arbitrage.min_profit_cents = 0.5
    scanner = ArbitrageScanner(notifier=Broken())
    found = scanner.scan([make_candidate(ticker="KXARB-1", yes_bid=55.0, yes_ask=45.0)])
    assert len(found) == 1, "the opportunity survives a broken notifier"


def test_the_scanner_does_not_place_orders(client, order_store):
    """Detection only, deliberately — a half-filled arb is an unhedged bet."""
    CONFIG.arbitrage.enabled = True
    CONFIG.arbitrage.min_profit_cents = 0.5
    scanner = ArbitrageScanner(notifier=Notifier())
    scanner.scan([make_candidate(ticker="KXARB-1", yes_bid=55.0, yes_ask=45.0)])
    assert client.place_order_calls == []


def test_describe_states_the_real_cost_including_fees():
    arb = find_locked_arb("KXA-1", 45.0, 45.0)
    text = arb.describe()
    assert "fees" in text and "KXA-1" in text
    assert "per pair" in text


# --------------------------------------------------------------------------
# the two merged mechanisms together
# --------------------------------------------------------------------------
#
# Kelly (this branch) and unrealized-PnL loss control (PR #4) were developed
# independently and both touch the sizing gates. The merge was textually
# clean, which is not the same as correct, so their interaction is pinned
# here rather than assumed.


def test_kelly_and_the_unrealized_loss_budget_both_apply(risk):
    """Both gates live in one list, and the smallest still wins."""
    from core.account_state import Position

    CONFIG.risk.kelly_enabled = True
    CONFIG.risk.kelly_fraction = 0.25

    # $80 down on open positions against a $100 daily limit leaves $20 of new
    # risk — tighter than the Kelly cap on a strong edge, so the loss budget
    # should bind.
    losing = AccountSnapshot(
        balance_cents=100_000.0,
        available_balance_cents=100_000.0,
        positions=[Position(ticker="KXOTHER-1", side="yes", quantity=400,
                            avg_price_cents=60.0, mark_price_cents=40.0,
                            event_ticker="KXOTHER")],
        reconciled_at=time.time(),
    )
    decision = risk.evaluate(make_verdict(maker_probability=0.95), losing)
    assert decision.approved
    assert decision.detail["binding_constraint"] == "daily loss budget"
    assert decision.detail["unrealized_pnl_today"] == pytest.approx(-80.0)

    # With a flat book, a thin edge puts Kelly back in charge.
    risk.reset_kill_switch()
    thin = risk.evaluate(make_verdict(maker_probability=0.60), snapshot())
    assert thin.approved
    assert thin.detail["binding_constraint"] == "kelly cap"


def test_an_unrealized_loss_still_trips_the_switch_with_kelly_on(risk):
    """Kelly must not accidentally mask the drawdown halt."""
    from core.account_state import Position
    from workers.risk_guardrail import KillSwitchTripped

    CONFIG.risk.kelly_enabled = True
    deep = AccountSnapshot(
        balance_cents=100_000.0,
        available_balance_cents=100_000.0,
        positions=[Position(ticker="KXOTHER-1", side="yes", quantity=500,
                            avg_price_cents=60.0, mark_price_cents=35.0,
                            event_ticker="KXOTHER")],
        reconciled_at=time.time(),
    )
    with pytest.raises(KillSwitchTripped):
        risk.evaluate(make_verdict(), deep)
