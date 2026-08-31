"""Freezing a category that is measured to lose money.

Why this exists
---------------
The 2026-08-30 ledger report collapsed 19,988 settled rows to the 975 events
that actually resolve, and recomputed every category's PnL cluster-robustly.
Almost every apparent signal dissolved. Exactly one result cleared two sigma
in either direction:

    Weather/llm   4,663 rows   18 events   mean -$0.0368   cluster t = -2.11

That is the only statistically demonstrated fact in the ledger, and it is a
loss. Crypto/quant came out at -0.95 and Finance/llm at -0.05 — neither
demonstrates anything. So Weather is not frozen because it looks bad; it is
frozen because it is the one path we can actually prove loses money.

What "frozen" has to mean
-------------------------
Two properties, and the tests below are split along exactly that line:

1. **It can never trade.** No frozen proposal may reach the risk layer or
   execution, on any pass, under any sampling. This is the safety property.
2. **It must keep producing evidence.** A frozen path is still priced
   occasionally and its proposals are still written, so a future weather
   model can be seen flipping the sign. Freezing that also blinded us would
   make the decision irreversible in practice.
"""
from __future__ import annotations

import pytest

import main
from config import CONFIG
from workers.coherence import CoherenceGate
from workers.maker import Proposal

from tests.conftest import make_candidate
from tests.test_pass_loop import StubChecker, StubQuantMaker, StubScout


class CountingMaker:
    """Records every call, so 'was not priced' is testable rather than assumed."""

    def __init__(self, probability=0.70):
        self.probability = probability
        self.priced: list[str] = []

    def propose(self, candidate):
        self.priced.append(candidate.ticker)
        proposal = Proposal(
            candidate=candidate,
            maker_probability=self.probability,
            maker_confidence=0.8,
            reasoning="stub",
        )
        if proposal.edge_size < CONFIG.risk.min_edge_threshold:
            return None
        return proposal


@pytest.fixture
def run_pass(client, order_store, edge_store, account, execution, risk, ledger,
             monkeypatch):
    CONFIG.risk.dry_run = False
    monkeypatch.setattr(CONFIG, "frozen_categories", {"weather"})
    monkeypatch.setattr(CONFIG, "llm_reasoning_categories",
                        {"weather", "finance", "sports"})

    def run(candidates, pass_index=0, maker=None, gate=None):
        maker = maker or CountingMaker()
        filled = main.run_once(
            StubScout(candidates), maker, StubQuantMaker(), StubChecker(),
            risk, execution, ledger, account,
            coherence_gate=gate, pass_index=pass_index,
        )
        return filled, maker

    return run


def _weather():
    return make_candidate(ticker="KXHIGHNY-26AUG30-T80", category="Weather",
                          event_ticker="KXHIGHNY-26AUG30")


def _sports():
    return make_candidate(ticker="KXPGATOUR-TOC26-A", category="Sports",
                          event_ticker="KXPGATOUR-TOC26")


# -- property 1: it can never trade ----------------------------------------


def test_a_frozen_category_never_places_an_order(run_pass, client):
    """The safety property. Probe pass, so it IS priced — and still no order."""
    filled, maker = run_pass([_weather()], pass_index=0)

    assert maker.priced == ["KXHIGHNY-26AUG30-T80"], "probe pass should price it"
    assert filled == 0
    assert client.place_order_calls == [], "a frozen category reached execution"


def test_freezing_is_per_category_not_global(run_pass, client):
    """An unfrozen category in the same pass must be completely unaffected."""
    filled, _ = run_pass([_weather(), _sports()], pass_index=0)

    assert filled == 1
    assert len(client.place_order_calls) == 1
    assert "KXPGATOUR" in client.place_order_calls[0]["ticker"]


def test_a_frozen_proposal_never_reaches_the_coherence_gate(run_pass):
    """Recorded, not judged.

    Letting a path we have stopped trading mutate CoherenceGate's per-family
    state would let it change the sampling of a path we have not.
    """
    gate = CoherenceGate()
    run_pass([_weather()], pass_index=0, gate=gate)

    assert gate._events == {}, "frozen proposal was fed to the coherence gate"
    assert gate.sampled_families() == {}


# -- property 2: it must keep producing evidence ---------------------------


def test_a_frozen_proposal_is_still_written_and_gradeable(run_pass, edge_store):
    run_pass([_weather()], pass_index=0)

    rows = [r for r in edge_store.recent_edges(limit=50)
            if r["ticker"] == "KXHIGHNY-26AUG30-T80"]
    assert len(rows) == 1
    row = rows[0]
    assert row["action_taken"] == "skipped_frozen"
    assert row["checker_verdict"] is None, "the Checker was never asked"
    # Everything grading needs, and nothing that implies a position was held.
    # A row missing any of these is written but unscoreable, which would make
    # the freeze irreversible in practice.
    assert row["maker_probability"] is not None
    assert row["market_implied_probability"] is not None
    assert row["counterfactual_price_cents"] is not None
    assert row["counterfactual_direction"] in ("yes", "no")
    assert row["close_time"] is not None, "reconcile_forecasts skips rows without one"
    assert row["category"] == "Weather" and row["source"] == "llm", (
        "must land in the Weather/llm bucket refusal_breakdown groups by"
    )
    assert row["settled"] == 0 and row["size_contracts"] == 0


def test_the_frozen_label_is_distinct_from_a_risk_refusal(run_pass, edge_store):
    """It must be separable in refusal_breakdown, or freezing is invisible."""
    run_pass([_weather()], pass_index=0)

    actions = {r["action_taken"] for r in edge_store.recent_edges(limit=50)}
    assert "skipped_frozen" in actions
    assert "skipped_risk" not in actions, "a frozen row must not read as a risk refusal"


# -- sampling: cheaper, never less safe ------------------------------------


def test_a_non_probe_pass_does_not_pay_for_the_model_call(run_pass, monkeypatch):
    monkeypatch.setattr(CONFIG, "frozen_category_sample_rate", 20)
    filled, maker = run_pass([_weather()], pass_index=7)

    assert maker.priced == [], "frozen category was priced on a non-probe pass"
    assert filled == 0


def test_sampling_cannot_make_a_frozen_category_tradeable(run_pass, client,
                                                          monkeypatch):
    """Every pass a probe. Still never an order — sampling is a cost control,
    not the thing that keeps it out of the book."""
    monkeypatch.setattr(CONFIG, "frozen_category_sample_rate", 1)

    for i in range(5):
        filled, maker = run_pass([_weather()], pass_index=i)
        assert maker.priced == ["KXHIGHNY-26AUG30-T80"]
        assert filled == 0

    assert client.place_order_calls == []


@pytest.mark.parametrize("rate,index,probes", [
    (20, 0, True), (20, 1, False), (20, 19, False), (20, 20, True),
    (1, 3, True), (0, 3, True),
])
def test_probe_schedule(monkeypatch, rate, index, probes):
    monkeypatch.setattr(CONFIG, "frozen_category_sample_rate", rate)
    assert main._frozen_probe_pass(index) is probes


def test_nothing_is_frozen_when_the_set_is_empty(run_pass, client, monkeypatch):
    """The freeze must be removable without a code change."""
    monkeypatch.setattr(CONFIG, "frozen_categories", set())
    filled, maker = run_pass([_weather()], pass_index=0)

    assert maker.priced == ["KXHIGHNY-26AUG30-T80"]
    assert filled == 1
    assert len(client.place_order_calls) == 1
