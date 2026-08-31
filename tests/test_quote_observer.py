"""The quoting probe, wired into a pass.

Two properties matter more than the mechanics. The probe must never be able to
place an order, and it must never be able to take the trading loop down: it is
measurement bolted to a live path, and a measurement that can stop a pass from
reaching execution is worse than no measurement at all.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from memory.quote_store import QuoteStore
from workers.quote_observer import QuoteObserver
from workers.quoting import LONGSHOT, NEAR_CERTAIN
from workers.scout import Candidate

NOW = 1_000_000.0


@pytest.fixture
def observer(tmp_path):
    return QuoteObserver(store=QuoteStore(str(tmp_path / "q.db")))


def _c(ticker="KXT-1", yes_bid=45.0, yes_ask=55.0, hours=24.0):
    """A candidate with a close time far enough out to be quotable."""
    from datetime import datetime, timedelta, timezone

    close = datetime.fromtimestamp(NOW, timezone.utc) + timedelta(hours=hours)
    return Candidate(
        ticker=ticker, title=ticker, category="Financials",
        yes_bid=yes_bid, yes_ask=yes_ask, volume=100.0,
        close_time=close.isoformat().replace("+00:00", "Z"),
    )


def _rows(observer):
    from memory.db import connect

    with connect(observer.store.db_path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM quote_observations ORDER BY id")]


# -- what it records ------------------------------------------------------


def test_a_quotable_candidate_is_recorded_with_the_book_it_was_quoted_against(
        observer):
    out = observer.run([_c(yes_bid=45.0, yes_ask=55.0)], now=NOW)

    assert out["quoted"] == 1 and out["refused"] == 0
    row, = _rows(observer)
    assert (row["t0_yes_bid"], row["t0_yes_ask"]) == (45.0, 55.0)
    assert row["t0"] == NOW


def test_the_probe_is_centred_on_the_mid_not_on_a_model(observer):
    """Centring on our own fair value would confound adverse selection with
    model error, and the probe would answer neither question."""
    observer.run([_c(yes_bid=40.0, yes_ask=60.0)], now=NOW)

    row, = _rows(observer)
    assert row["fair_value_cents"] == 50.0
    assert row["bid_cents"] < 50.0 < row["ask_cents"]


def test_the_zone_comes_from_the_price_level(observer):
    observer.run([_c("A", 4.0, 6.0), _c("B", 94.0, 96.0)], now=NOW)

    zones = {r["ticker"]: r["zone"] for r in _rows(observer)}
    assert zones == {"A": LONGSHOT, "B": NEAR_CERTAIN}


def test_the_longshot_zone_rests_only_the_ask(observer):
    """The whole optimism-tax claim: at longshot prices we want to be the
    seller of YES to optimistic takers, never the buyer."""
    observer.run([_c("A", 4.0, 6.0)], now=NOW)

    row, = _rows(observer)
    assert row["bid_size"] == 0 and row["ask_size"] > 0


# -- what it refuses to record -------------------------------------------


@pytest.mark.parametrize("yes_bid,yes_ask,why", [
    (0.0, 55.0, "a zero bid is not a price"),
    (55.0, 45.0, "crossed book"),
    (50.0, 50.0, "one-sided book"),
    (45.0, 100.0, "an ask at payout is not a price"),
])
def test_a_book_that_is_not_two_sided_is_refused_not_repaired(
        observer, yes_bid, yes_ask, why):
    """Inventing a missing side would put a fabricated number at the base of
    the only measurement that decides whether quoting is real."""
    out = observer.run([_c(yes_bid=yes_bid, yes_ask=yes_ask)], now=NOW)

    assert out["quoted"] == 0, why
    assert out["refused"] == 1
    assert _rows(observer) == []


def test_a_candidate_with_an_unparseable_close_time_is_refused(observer):
    c = _c()
    c.close_time = "whenever"
    out = observer.run([c], now=NOW)

    assert out["quoted"] == 0 and out["refused"] == 1


def test_a_market_inside_the_stop_quote_window_is_refused(observer):
    """Near expiry a resting quote cannot be repriced fast enough to stay
    ahead of the settlement it is about to be measured by."""
    seconds = CONFIG.quoting.stop_quote_seconds
    out = observer.run([_c(hours=(seconds / 3600.0) / 2)], now=NOW)

    assert out["quoted"] == 0 and out["refused"] == 1


def test_a_wide_book_is_refused(observer):
    """A book is wide because nobody knows the price. Resting inside it is not
    liquidity provision, it is volunteering to be the one who finds out."""
    wide = CONFIG.quoting.max_market_spread_cents + 10
    out = observer.run([_c(yes_bid=50.0 - wide / 2, yes_ask=50.0 + wide / 2)],
                       now=NOW)

    assert out["quoted"] == 0 and out["refused"] == 1


# -- resolution across passes --------------------------------------------


def test_an_observation_is_resolved_by_a_later_pass_over_the_same_market(
        observer):
    q = CONFIG.quoting
    observer.run([_c("A", 45.0, 55.0)], now=NOW)
    # A pass late enough for the fill check, with the ask through our bid.
    out = observer.run([_c("A", 40.0, 44.0)],
                       now=NOW + q.observation_min_age_seconds + 1)
    assert out["filled_checked"] == 1 and out["marked"] == 0

    # And later still, for the mark, from a different reading.
    out = observer.run(
        [_c("A", 50.0, 54.0)],
        now=NOW + q.observation_min_age_seconds + q.mark_horizon_seconds + 2)
    assert out["marked"] == 1

    summary = observer.store.summary()
    assert summary["bid_fills"] == 1
    assert summary["bid_mark"] > 0


def test_a_market_missing_from_a_later_pass_is_left_open(observer):
    """The scout not returning a ticker today says nothing about where its
    price went, so the observation must stay resolvable."""
    observer.run([_c("A", 45.0, 55.0)], now=NOW)
    later = NOW + CONFIG.quoting.observation_min_age_seconds + 1
    out = observer.run([_c("B", 45.0, 55.0)], now=later)

    assert out["unreadable"] == 1 and out["filled_checked"] == 0
    pending = {r["ticker"] for r
               in observer.store.awaiting_fill_check(older_than=later)}
    assert "A" in pending, "an unreadable market must stay checkable"


# -- it must not be able to hurt the loop --------------------------------


def test_a_failing_store_does_not_take_the_pass_down(observer):
    """Measurement wired into a live path must degrade to "no data", never to
    a pass that does not reach execution."""
    class _Broken:
        def record_many(self, *a, **k):
            raise RuntimeError("disk on fire")

        def awaiting_fill_check(self, *a, **k):
            raise RuntimeError("disk on fire")

    observer.store = _Broken()
    out = observer.run([_c()], now=NOW)

    assert out == {"quoted": 0, "refused": 0, "filled_checked": 0,
                   "marked": 0, "unreadable": 0}


def test_the_kill_switch_records_nothing(observer, monkeypatch):
    monkeypatch.setattr(CONFIG.quoting, "observation_enabled", False)
    out = observer.run([_c()], now=NOW)

    assert out["quoted"] == 0
    assert _rows(observer) == []


def test_the_observer_cannot_trade(observer):
    for forbidden in ("client", "execution", "place_order", "submit", "cancel"):
        assert not hasattr(observer, forbidden)
    for forbidden in ("client", "execution", "place_order", "submit"):
        assert not hasattr(observer.generator, forbidden)


# -- the wiring into a pass ----------------------------------------------


def test_run_once_hands_the_pass_candidates_to_the_probe(
        tmp_path, client, order_store, edge_store, account, execution, risk,
        ledger):
    """Constructing an observer is not the same as calling one. Without this,
    the probe could sit in main() for weeks recording nothing at all."""
    import main
    from tests.conftest import make_candidate
    from tests.test_pass_loop import (
        StubChecker, StubMaker, StubQuantMaker, StubScout,
        _positions_follow_fills,
    )

    CONFIG.risk.dry_run = True
    candidates = [make_candidate(ticker="KXQ-1", event_ticker="KXQ-E1")]
    _positions_follow_fills(client, candidates)
    observer = QuoteObserver(store=QuoteStore(str(tmp_path / "q.db")))

    main.run_once(StubScout(candidates), StubMaker(), StubQuantMaker(),
                  StubChecker(), risk, execution, ledger, account,
                  quote_observer=observer)

    assert [r["ticker"] for r in _rows(observer)] == ["KXQ-1"]


def test_a_pass_without_a_probe_still_runs(
        client, order_store, edge_store, account, execution, risk, ledger):
    """The probe is optional by construction: main() sets it to None when its
    table cannot be opened, and that path must not break a pass."""
    import main
    from tests.conftest import make_candidate
    from tests.test_pass_loop import (
        StubChecker, StubMaker, StubQuantMaker, StubScout,
        _positions_follow_fills,
    )

    CONFIG.risk.dry_run = True
    candidates = [make_candidate(ticker="KXQ-1", event_ticker="KXQ-E1")]
    _positions_follow_fills(client, candidates)

    main.run_once(StubScout(candidates), StubMaker(), StubQuantMaker(),
                  StubChecker(), risk, execution, ledger, account,
                  quote_observer=None)
