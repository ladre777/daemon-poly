"""
Integration cover for one scan pass: the wiring in main.run_once that decides
whether any order is placed at all.

The safety properties here are about ordering, not arithmetic — reconcile
before trading, re-reconcile after each fill, and place nothing when the
account picture cannot be verified.
"""
from __future__ import annotations

import pytest

import main
from config import CONFIG
from core.account_state import ReconciliationError
from workers.execution import UnmanagedMakerMode, assert_order_strategy_supported
from workers.maker import Proposal

from tests.conftest import make_candidate


class StubScout:
    def __init__(self, candidates):
        self.candidates = candidates
        self.scans = 0

    def scan(self):
        self.scans += 1
        return list(self.candidates)


class StubQuantMaker:
    def can_handle(self, candidate):
        return False


class StubMaker:
    def __init__(self, probability=0.70):
        self.probability = probability

    def propose(self, candidate):
        proposal = Proposal(
            candidate=candidate,
            maker_probability=self.probability,
            maker_confidence=0.8,
            reasoning="stub",
        )
        # Mirrors the real Maker, which drops sub-threshold edges rather than
        # handing them downstream.
        if proposal.edge_size < CONFIG.risk.min_edge_threshold:
            return None
        return proposal


class StubChecker:
    def __init__(self, verdict="approve", confidence=0.9):
        self.verdict = verdict
        self.confidence = confidence

    def check(self, proposal):
        from workers.checker import Verdict

        return Verdict(proposal=proposal, verdict=self.verdict,
                       confidence=self.confidence, reasoning="stub")


@pytest.fixture
def pass_parts(client, order_store, edge_store, account, execution, risk, ledger):
    CONFIG.risk.dry_run = False

    def run(candidates, checker=None, maker=None):
        scout = StubScout(candidates)
        return main.run_once(
            scout, maker or StubMaker(), StubQuantMaker(),
            checker or StubChecker(), risk, execution, ledger, account,
        ), scout

    return run


# -- reconcile before trading ----------------------------------------------


def test_pass_places_no_orders_when_reconciliation_fails(pass_parts, client):
    def down(*a, **kw):
        raise ReconciliationError("exchange unreachable")

    client.get_balance = down

    filled, scout = pass_parts([make_candidate()])

    assert filled == 0
    assert scout.scans == 0, "must not even scan without verified account state"
    assert client.place_order_calls == []


def test_pass_places_no_orders_when_an_order_is_in_unknown_state(
    pass_parts, client, order_store
):
    from core.kalshi_client import KalshiAPIError
    from core.order_state import OrderIntent, OrderState

    intent = OrderIntent(ticker="KXOLD-1", action="buy", side="yes", count=5,
                         limit_price_cents=50.0, time_in_force="IOC")
    record = order_store.record_intent(intent)
    record.state = OrderState.UNKNOWN
    order_store.update_order(record)

    def explode(**kw):
        raise KalshiAPIError(503, "down")

    client.get_orders = explode

    filled, _ = pass_parts([make_candidate()])

    assert filled == 0
    assert client.place_order_calls == []


def test_happy_path_places_one_order(pass_parts, client):
    filled, _ = pass_parts([make_candidate()])

    assert filled == 1
    assert len(client.place_order_calls) == 1


def test_zero_fill_is_not_counted_as_a_filled_trade(pass_parts, client):
    client.fill_plan = [0]
    filled, _ = pass_parts([make_candidate()])

    assert len(client.place_order_calls) == 1
    assert filled == 0, "an order that filled nothing is not a trade"


# -- exposure accumulates within a pass ------------------------------------


def test_exposure_is_re_reconciled_after_each_fill(pass_parts, client):
    """Without this, N candidates in one pass are each approved against the
    same pre-trade exposure and can collectively blow through the cap."""
    before = client.call_counts.get("get_balance", 0)

    pass_parts([make_candidate()])

    # One reconcile before the scan, one after the fill.
    assert client.call_counts["get_balance"] >= before + 2


def test_second_candidate_sees_the_first_ones_exposure(pass_parts, client):
    """Two markets in the same event: after the first fills, the per-event cap
    must bind on the second.

    The second order is not necessarily refused — it gets resized down to the
    headroom that remains, which is the correct behaviour. What must hold is
    the invariant: combined exposure never exceeds the event cap.
    """
    CONFIG.risk.max_event_exposure_pct = 0.05  # $50 on a $1000 bankroll

    candidates = [
        make_candidate(ticker="KXTEST-25AUG14-A", event_ticker="KXTEST-25AUG14"),
        make_candidate(ticker="KXTEST-25AUG14-B", event_ticker="KXTEST-25AUG14"),
    ]

    def positions_follow_fills(settlement_status="unsettled"):
        # Model the exchange reporting positions for whatever has filled.
        out = []
        for fill in client.fills:
            out.append({
                "ticker": fill["ticker"],
                "position": fill["count"],
                "market_exposure": fill["count"] * (fill["yes_price"] or 0),
                "fees_paid": 0,
                "event_ticker": "KXTEST-25AUG14",
            })
        return {"market_positions": out}

    client.get_positions = positions_follow_fills
    CONFIG.risk.allow_position_drift = True

    filled, _ = pass_parts(candidates)

    counts = [c["count"] for c in client.place_order_calls]
    assert len(counts) == 2
    assert counts[1] < counts[0] / 10, (
        "the second order must be sized against the headroom the first left, "
        "not against a pre-trade snapshot"
    )

    event_cap_cents = 100_000.0 * CONFIG.risk.max_event_exposure_pct
    committed = sum(
        c["count"] * float(c["yes_price_dollars"]) * 100
        for c in client.place_order_calls
    )
    assert committed <= event_cap_cents, (
        f"combined event exposure {committed:.0f}c breached the "
        f"{event_cap_cents:.0f}c cap"
    )


def test_duplicate_candidate_in_one_pass_produces_one_order(pass_parts, client):
    candidate = make_candidate()
    pass_parts([candidate, candidate])
    assert len(client.place_order_calls) == 1


# -- checker gate -----------------------------------------------------------


def test_rejected_by_checker_places_nothing(pass_parts, client):
    filled, _ = pass_parts([make_candidate()], checker=StubChecker(verdict="reject"))
    assert filled == 0
    assert client.place_order_calls == []


def test_no_edge_places_nothing(pass_parts, client):
    """Maker agreeing with the market is not a trade."""
    filled, _ = pass_parts([make_candidate()], maker=StubMaker(probability=0.50))
    assert client.place_order_calls == []


# -- startup gates ----------------------------------------------------------


def test_maker_mode_is_refused_at_startup():
    CONFIG.risk.order_strategy = "maker"
    with pytest.raises(UnmanagedMakerMode) as e:
        assert_order_strategy_supported()
    # The error has to say what is missing, not just "no".
    for expected in ("TTL", "cancellation", "duplicate", "taker"):
        assert expected.lower() in str(e.value).lower()


def test_taker_mode_is_accepted():
    CONFIG.risk.order_strategy = "taker"
    assert assert_order_strategy_supported() == "taker"


def test_dry_run_is_the_shipped_default():
    """A fresh checkout must never fire real orders."""
    import importlib

    import config

    importlib.reload(config)
    assert config.CONFIG.risk.dry_run is True


def test_dry_run_pass_sends_nothing(client, order_store, edge_store, account,
                                    risk, ledger):
    from workers.execution import Execution

    CONFIG.risk.dry_run = True
    execution = Execution(client, order_store, account)

    filled = main.run_once(
        StubScout([make_candidate()]), StubMaker(), StubQuantMaker(),
        StubChecker(), risk, execution, ledger, account,
    )

    assert filled == 0
    assert client.place_order_calls == []
