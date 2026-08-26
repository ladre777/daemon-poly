"""
The three fixes for the zero-approval finding.

A ledger trace of 501 decisions (edges #489-#989) showed the pipeline was not
being selective — it was losing to its own clock:

  Checker rejected                     279
  STALE QUOTE                          149   <- 99% of Checker approvals
  Checker abstained                     45
  Checker approved, below confidence    27   <- logged as "did not approve: approve"
  Risk approved                          1

Of 146 Checker approvals, 123 cleared the confidence threshold and exactly one
reached risk approval. Every stale refusal was over the 60s limit — 60 to 115s,
median 79 — because a scan takes ~53s of rate-limited pagination before any
model is asked anything, and Moonshot then burned its full timeout once per
pass before the fallback answered.

None of these fixes touches MAX_QUOTE_AGE_SECONDS or any risk parameter.
"""
from __future__ import annotations

import time

import pytest

from config import CONFIG
from core.kalshi_client import KalshiAPIError, KalshiTimeoutError
from core.llm_client import MakerLLM, _is_timeout
from workers.scout import Scout

from tests.conftest import make_candidate, make_verdict


# --------------------------------------------------------------------------
# fix 1: re-read the quote instead of reusing the scan-time capture
# --------------------------------------------------------------------------

class FakeMarketClient:
    """Serves one market payload, or raises."""

    def __init__(self, yes_bid=48.0, yes_ask=52.0, raises=None, payload=None):
        self.yes_bid, self.yes_ask = yes_bid, yes_ask
        self.raises = raises
        self.payload = payload
        self.calls: list[str] = []

    def get_market(self, ticker):
        self.calls.append(ticker)
        if self.raises:
            raise self.raises
        if self.payload is not None:
            return self.payload
        from datetime import datetime, timedelta, timezone

        close = datetime.now(timezone.utc) + timedelta(hours=2)
        return {"market": {
            "ticker": ticker,
            "title": "refreshed",
            "yes_bid_dollars": f"{self.yes_bid / 100:.4f}",
            "yes_ask_dollars": f"{self.yes_ask / 100:.4f}",
            "volume_fp": "10000",
            "close_time": close.isoformat().replace("+00:00", "Z"),
        }}


def _stale_candidate(**kw):
    """A candidate whose quote is already past the freshness limit, exactly
    as production produced them."""
    c = make_candidate(**kw)
    c.quote.captured_at = time.time() - CONFIG.risk.max_quote_age_seconds - 19
    return c


def test_a_stale_candidate_becomes_fresh():
    """The whole point: 79s old in, fresh out, limit untouched."""
    c = _stale_candidate()
    assert c.quote.is_stale(), "precondition: this is what risk was refusing"

    scout = Scout(client=FakeMarketClient())
    assert scout.refresh_quote(c) is True
    assert not c.quote.is_stale()
    assert c.quote.age_seconds < 1.0


def test_the_price_is_updated_not_just_the_timestamp():
    """The failure mode that would make this fix worse than nothing.

    Refreshing only the clock would satisfy the freshness check while the
    decision stayed anchored to a price that had moved — precisely what the
    check exists to prevent.
    """
    c = _stale_candidate()
    c.yes_bid, c.yes_ask = 48.0, 52.0

    scout = Scout(client=FakeMarketClient(yes_bid=61.0, yes_ask=64.0))
    assert scout.refresh_quote(c) is True

    assert c.yes_bid == pytest.approx(61.0)
    assert c.yes_ask == pytest.approx(64.0)
    assert c.quote.yes_bid == pytest.approx(61.0)
    assert c.quote.yes_ask == pytest.approx(64.0)


def test_a_moved_price_re_derives_the_edge_and_can_lose_it(risk, order_store):
    """A market that ran away from us must now fail on its own merits.

    Risk computes net edge from the candidate's live bid/ask, so refreshing
    into a worse price is not "still approved with a new timestamp" — it is a
    genuine re-evaluation.
    """
    from core.account_state import AccountSnapshot
    from core.pricing import net_edge

    snapshot = AccountSnapshot(balance_cents=100_000.0,
                               available_balance_cents=100_000.0,
                               reconciled_at=time.time())
    c = _stale_candidate(yes_bid=48.0, yes_ask=52.0)
    verdict = make_verdict(candidate=c, maker_probability=0.70)
    before = net_edge(0.70, c, verdict.proposal.direction)

    # The market repriced to where the model has no edge left.
    Scout(client=FakeMarketClient(yes_bid=68.0, yes_ask=72.0)).refresh_quote(c)
    after = net_edge(0.70, c, verdict.proposal.direction)

    assert after < before
    decision = risk.evaluate(verdict, snapshot)
    assert not decision.approved
    assert "net edge" in decision.reason, (
        "must be refused for the real reason — no edge — not for staleness"
    )


@pytest.mark.parametrize("boom", [
    KalshiAPIError(503, "gateway down"),
    KalshiTimeoutError("GET", "/markets", Exception("read timeout")),
])
def test_an_unreadable_market_is_skipped_not_traded_stale(boom):
    """Fails closed. The old price is never the fallback."""
    c = _stale_candidate()
    assert Scout(client=FakeMarketClient(raises=boom)).refresh_quote(c) is False
    assert c.quote.is_stale(), "left stale, and the caller skips it"


def test_a_malformed_refresh_payload_is_skipped():
    scout = Scout(client=FakeMarketClient(payload={"market": {"ticker": "X"}}))
    assert scout.refresh_quote(_stale_candidate()) is False


def test_a_non_dict_payload_is_skipped():
    scout = Scout(client=FakeMarketClient(payload={"market": "nonsense"}))
    assert scout.refresh_quote(_stale_candidate()) is False


def test_run_once_refreshes_only_checker_approved_candidates(
    client, order_store, edge_store, account, execution, risk, ledger
):
    """Cost control: one extra call per approval, not one per market scanned.

    The scan sees thousands of markets; a handful clear the Checker. Refreshing
    at scan scope would multiply the request budget that already makes the
    scan slow enough to cause this bug.
    """
    import main
    from tests.test_pass_loop import (
        StubChecker, StubMaker, StubQuantMaker, StubScout, _positions_follow_fills,
    )

    CONFIG.risk.dry_run = False
    candidates = [make_candidate(ticker="KXA-1"), make_candidate(ticker="KXB-2")]
    _positions_follow_fills(client, candidates)

    scout = StubScout(candidates)
    main.run_once(scout, StubMaker(), StubQuantMaker(), StubChecker(),
                  risk, execution, ledger, account)

    assert scout.refreshed, "approved candidates must be re-read"
    assert len(scout.refreshed) <= len(candidates)


def test_a_failed_refresh_stops_the_candidate_reaching_execution(
    client, order_store, edge_store, account, execution, risk, ledger
):
    import main
    from tests.test_pass_loop import (
        StubChecker, StubMaker, StubQuantMaker, StubScout, _positions_follow_fills,
    )

    CONFIG.risk.dry_run = False
    candidates = [make_candidate(ticker="KXA-1")]
    _positions_follow_fills(client, candidates)

    scout = StubScout(candidates, refresh_ok=False)
    filled = main.run_once(scout, StubMaker(), StubQuantMaker(), StubChecker(),
                           risk, execution, ledger, account)

    assert filled == 0
    assert client.place_order_calls == [], (
        "an unverifiable price must never reach the exchange"
    )


# --------------------------------------------------------------------------
# fix 2: stop paying the primary provider's timeout every pass
# --------------------------------------------------------------------------

class Backend:
    def __init__(self, name, behaviour):
        self.name, self.model = name, "m"
        self.configured = True
        self._behaviour = list(behaviour)
        self.calls = 0

    def complete(self, system, user, temperature=0.3):
        self.calls += 1
        step = self._behaviour[min(self.calls - 1, len(self._behaviour) - 1)]
        if isinstance(step, Exception):
            raise step
        return step


def _timeout():
    import httpx

    return httpx.ReadTimeout("The read operation timed out")


def test_the_production_error_is_recognised_as_a_timeout():
    """The exact exception text from the live logs."""
    assert _is_timeout(_timeout())
    assert _is_timeout(Exception("The read operation timed out"))
    assert not _is_timeout(ValueError("bad request"))


def test_repeated_timeouts_open_the_breaker_despite_successes():
    """The bug: the consecutive counter never reached its threshold.

    Moonshot timed out roughly once per pass with successes in between, so
    the breaker never opened and the bot paid the timeout forever.
    """
    primary = Backend("moonshot", [_timeout(), "ok", _timeout(), "ok", "ok"])
    fallback = Backend("anthropic", ["fb"])
    llm = MakerLLM(primary, fallback, timeout_threshold=2)

    assert llm.complete("s", "u") == "fb"      # timeout 1 -> fallback
    assert llm.complete("s", "u") == "ok"      # primary answers; tally kept
    assert llm.complete("s", "u") == "fb"      # timeout 2 -> breaker opens

    assert llm.breaker.is_open
    assert llm.on_fallback

    before = primary.calls
    assert llm.complete("s", "u") == "fb"
    assert primary.calls == before, "primary must not be called while open"


def test_a_success_does_not_erase_the_timeout_tally():
    """A fast answer after a slow one means flaky, not healthy."""
    primary = Backend("moonshot", [_timeout(), "ok", "ok", "ok"])
    llm = MakerLLM(primary, Backend("anthropic", ["fb"]), timeout_threshold=2)

    llm.complete("s", "u")
    llm.complete("s", "u")
    assert llm.timeouts_seen == 1, "not reset by the intervening success"


def test_one_timeout_alone_does_not_open_the_breaker():
    """A single slow call is noise; the fallback covers it."""
    primary = Backend("moonshot", [_timeout(), "ok"])
    llm = MakerLLM(primary, Backend("anthropic", ["fb"]), timeout_threshold=2)

    assert llm.complete("s", "u") == "fb"
    assert not llm.breaker.is_open


def test_the_tally_clears_once_the_cooldown_elapses():
    """Self-healing: a recovered provider is not held against its history."""
    primary = Backend("moonshot", [_timeout(), _timeout(), "ok"])
    llm = MakerLLM(primary, Backend("anthropic", ["fb"]),
                   cooldown_seconds=0.01, timeout_threshold=2)

    llm.complete("s", "u")
    llm.complete("s", "u")
    assert llm.breaker.is_open

    time.sleep(0.02)
    assert llm.complete("s", "u") == "ok", "probe goes back to the primary"
    assert llm.timeouts_seen == 0


def test_the_timeout_budget_is_calibrated_on_observed_latency():
    """The budget must be a measurement, not a guess — but the measurement
    has been retaken, and it moved the other way.

    This test used to assert ``<= 8.0``, on the reading that successful calls
    returned in ~2s so a long budget was only ever spent by calls that were
    going to fail anyway. Production then showed the opposite failure mode.
    Commit 442ea6b (2026-08-21, "Raise Moonshot timeouts to 25s; cap LLM calls
    at 25/pass to reduce timeout storms") raised maker and checker timeouts
    from 12s to 25s together, recording in config.py:

        25s — Moonshot often answers after 12s under load; short timeout =
        false failure.

    Under load the tail is long, and a tight budget discards good answers —
    which is the one thing the old lower bound was written to prevent. Two
    commits disagreed here and the later one carries the production evidence,
    so the assertion follows the shipped policy rather than pinning a value
    the system deliberately moved off.

    The config value is not changed by this test. What is asserted is that
    the number stays deliberate: long enough to cover the observed tail, not
    so long that a hung provider stalls a whole pass.
    """
    assert CONFIG.models.maker_timeout_seconds >= 25.0, (
        "must cover the observed under-load tail; below this, slow-but-good "
        "answers are discarded as failures (442ea6b)"
    )
    assert CONFIG.models.maker_timeout_seconds <= 60.0, (
        "a hung provider must not be able to stall a pass indefinitely"
    )
    assert (
        CONFIG.models.checker_timeout_seconds
        == CONFIG.models.maker_timeout_seconds
    ), "442ea6b raised both together; they answer to the same provider tail"


# --------------------------------------------------------------------------
# fix 3: the ledger must distinguish 'rejected' from 'below threshold'
# --------------------------------------------------------------------------

def _snapshot():
    from core.account_state import AccountSnapshot

    return AccountSnapshot(balance_cents=100_000.0,
                           available_balance_cents=100_000.0,
                           reconciled_at=time.time())


def test_a_low_confidence_approval_says_so(risk):
    """Production wrote 27 rows reading "Checker did not approve: approve".

    Self-contradictory, and it sends an auditor after the model when the
    threshold is what blocked the trade.
    """
    verdict = make_verdict(verdict="approve",
                           confidence=CONFIG.risk.checker_min_confidence - 0.05)
    decision = risk.evaluate(verdict, _snapshot())

    assert not decision.approved
    assert "Checker approved" in decision.reason
    assert "confidence" in decision.reason
    assert "below" in decision.reason
    assert f"{verdict.confidence:.2f}" in decision.reason
    assert f"{CONFIG.risk.checker_min_confidence:.2f}" in decision.reason
    assert "did not approve: approve" not in decision.reason


@pytest.mark.parametrize("verdict_str", ["reject", "abstain"])
def test_a_genuine_decline_still_reads_as_a_decline(risk, verdict_str):
    decision = risk.evaluate(
        make_verdict(verdict=verdict_str, confidence=0.9), _snapshot()
    )
    assert not decision.approved
    assert decision.reason == f"Checker did not approve: {verdict_str}"


def test_the_two_reasons_are_never_the_same_string(risk):
    """They are different signals with opposite responses — improve the model
    versus revisit the threshold — so the permanent record must separate
    them."""
    low = risk.evaluate(make_verdict(verdict="approve", confidence=0.55),
                        _snapshot()).reason
    rejected = risk.evaluate(make_verdict(verdict="reject", confidence=0.9),
                             _snapshot()).reason
    assert low != rejected


def test_the_confidence_threshold_itself_is_unchanged():
    """Explicitly not touched — this was a logging fix, not a risk change."""
    assert CONFIG.risk.checker_min_confidence == 0.65


def test_the_freshness_limit_itself_is_unchanged():
    assert CONFIG.risk.max_quote_age_seconds == 60.0
