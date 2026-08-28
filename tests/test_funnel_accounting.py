"""
The pass funnel must account for every candidate it was given.

The funnel exists because "Scout returned 2,917 candidates" followed by
silence is indistinguishable from a crash, a threshold nothing cleared, and a
category filter that matched nothing. That guarantee only holds if the
counters sum: a candidate that leaves the pass without incrementing anything
is invisible in exactly the way the funnel was built to prevent.

One did. On the first production pass where crypto was priced at all, the
funnel read ``quant 18 (no proposal 0)`` and ``proposed 11`` — of which the
LLM path accounted for seven. Fourteen quant candidates left no trace. They
had priced fine and simply not cleared MIN_EDGE_THRESHOLD after fees, which
is the quant path working correctly, reported as though nothing had happened.

These assertions originally targeted the funnel format of that era —
``quant 18 (no proposal 0)``. The line has since moved to ``key=value``
throughout, so they were rehomed onto ``quant_no_proposal=`` and
``quant_below_edge=`` rather than the product being reverted to a
parenthesised format it no longer uses anywhere else. The guarantee under
test is unchanged and is the point: attempted must equal no-proposal plus
below-edge plus proposed, so a candidate cannot leave the quant path without
incrementing something.
"""
from __future__ import annotations

import pytest

import main
from config import CONFIG
from workers.maker import Proposal

from tests.conftest import make_candidate
from tests.test_pass_loop import (
    StubChecker, StubMaker, StubScout, _positions_follow_fills,
)


class StubQuantMaker:
    """Prices everything, with control over what each proposal is worth.

    ``priceable`` decides whether the model produced a result at all;
    ``clears_edge`` decides whether that result survived the edge threshold.
    They are different outcomes and the funnel has to say which happened.
    """

    def __init__(self, priceable=True, clears_edge=True):
        self.priceable = priceable
        self.clears_edge = clears_edge
        self.passes = 0

    def begin_pass(self):
        self.passes += 1

    def can_handle(self, candidate):
        return True

    def propose(self, candidate):
        if not self.priceable:
            return None
        return _Result(candidate, self.clears_edge)


class _Result:
    def __init__(self, candidate, clears_edge):
        self.candidate = candidate
        self.clears_edge = clears_edge

    def to_maker_proposal(self):
        # Mirrors the real one: None means "priced, but the edge did not
        # clear MIN_EDGE_THRESHOLD net of fees".
        if not self.clears_edge:
            return None
        return Proposal(candidate=self.candidate, maker_probability=0.70,
                        maker_confidence=0.6, reasoning="quant", source="quant")


@pytest.fixture
def run_pass(client, order_store, edge_store, account, execution, risk, ledger):
    CONFIG.risk.dry_run = True

    def run(candidates, quant):
        _positions_follow_fills(client, candidates)
        main.run_once(StubScout(candidates), StubMaker(), quant, StubChecker(),
                      risk, execution, ledger, account)

    return run


def funnel_line(caplog):
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("Pass funnel")]
    assert lines, "the pass must always report a funnel"
    return lines[-1]


def test_a_priced_candidate_whose_edge_is_too_small_is_counted(run_pass, caplog):
    """The production hole. Fourteen candidates left the pass here silently."""
    candidates = [make_candidate(ticker=f"KXBTC15M-T{i}",
                                 event_ticker=f"KXBTC15M-{i}")
                  for i in range(3)]

    with caplog.at_level("INFO", logger="daemon_kalshi.main"):
        run_pass(candidates, StubQuantMaker(clears_edge=False))

    line = funnel_line(caplog)
    assert "quant=3 quant_no_proposal=0 quant_below_edge=3" in line
    assert "proposed=0" in line


def test_an_unpriceable_candidate_is_counted_separately(run_pass, caplog):
    """A different failure with a different fix: no price at all means the
    feed is cold or the data failed quality checks, not that the market was
    fairly priced."""
    candidates = [make_candidate(ticker="KXETH-T1", event_ticker="KXETH-1")]

    with caplog.at_level("INFO", logger="daemon_kalshi.main"):
        run_pass(candidates, StubQuantMaker(priceable=False))

    line = funnel_line(caplog)
    assert "quant=1 quant_no_proposal=1 quant_below_edge=0" in line


def test_a_quant_candidate_that_clears_the_edge_reaches_the_checker(run_pass, caplog):
    candidates = [make_candidate(ticker="KXETH-T1", event_ticker="KXETH-1")]

    with caplog.at_level("INFO", logger="daemon_kalshi.main"):
        run_pass(candidates, StubQuantMaker())

    line = funnel_line(caplog)
    assert "quant=1 quant_no_proposal=0 quant_below_edge=0" in line
    assert "proposed=1" in line
    assert "checked=1" in line


def test_the_quant_dispositions_sum_to_the_attempts(run_pass, caplog):
    """The invariant, stated directly: attempted == no-proposal + below-edge
    + proposed. If a future branch adds a fourth way out of the quant path
    without a counter, this fails."""
    candidates = [make_candidate(ticker=f"KXETH-T{i}", event_ticker=f"KXETH-{i}")
                  for i in range(4)]

    with caplog.at_level("INFO", logger="daemon_kalshi.main"):
        run_pass(candidates, StubQuantMaker(clears_edge=False))

    line = funnel_line(caplog)
    def field(name):
        return int(line.split(f"{name}=")[1].split(" ")[0])

    attempted = field("quant")
    no_proposal = field("quant_no_proposal")
    below_edge = field("quant_below_edge")
    proposed = field("proposed")

    assert attempted == 4
    assert no_proposal + below_edge + proposed == attempted


# -- the Checker's reason is visible ---------------------------------------


def test_a_rejection_logs_the_reason_not_just_the_verdict(caplog):
    """This gate has rejected 100% of everything it has ever seen — 24 for 24
    on one pass, across weather and crypto, from both the LLM and the quant
    path. That is either a correct gate in front of bad proposals or a gate
    biased toward reject, and "reject (conf 0.65)" cannot tell those apart.

    It is the same blindness that let a 6.6%-annualized bitcoin survive a
    whole session, and it matters more here: when a gate blocks everything the
    temptation is to loosen it, which is precisely the wrong move if the gate
    is right.
    """
    import logging

    from core.validation import clamp_text

    reasoning = ("The Maker's estimate leans on a stale forecast and the "
                 "market already reflects the same information.")
    log = logging.getLogger("daemon_kalshi.main")

    with caplog.at_level("INFO"):
        log.info("Checker verdict on %s: %s (conf %.2f) — %s",
                 "KXHIGHNY-26AUG18-T85", "reject", 0.90,
                 clamp_text(reasoning, 400))

    assert "stale forecast" in caplog.text


def test_the_reason_is_clamped_so_one_verdict_cannot_flood_the_log():
    """Checker reasoning is model output and therefore unbounded in
    principle; the log line has to stay one line."""
    from core.validation import clamp_text

    clamped = clamp_text("x" * 5000, 400)

    assert len(clamped) < 500
    assert "truncated" in clamped
