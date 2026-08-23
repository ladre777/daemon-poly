"""
Making the paper-trading period measurable.

The schema always held fill prices, fees and realized PnL. But three queries
filtered on ``action_taken = 'executed'``, and ``record_execution`` writes
``'dry_run'`` whenever the run is paper — so a dry-run row was never settled
and never scored. The entire paper period produced no calibration data at all,
by construction, however long the bot ran.

Worse on the live configuration: nothing is approved either. Every row is
``skipped_risk``, so grading only paper fills would have produced an empty
table for the same reason, one level down.

So forecasts are graded whatever happened to them, kept in three modes that
are never averaged together:

    live     really traded; PnL is money
    paper    approved and dry-run, or filled nothing; PnL is counterfactual
    refused  the gate declined it; PnL is what it WOULD have made

The third is the one that answers "is the Checker refusing winners".
"""
from __future__ import annotations

import pytest

from memory.edge_store import EdgeRecord, EdgeStore
from workers.ledger import Ledger, _counterfactual_pnl


@pytest.fixture
def store(db_path):
    return EdgeStore(db_path)


def record(store, action="skipped_risk", probability=0.70, price=40.0,
           side="yes", ticker="KXTEST-1", category="Crypto"):
    return store.record_edge(EdgeRecord(
        ticker=ticker, category=category, source="llm",
        maker_probability=probability, market_implied_probability=0.40,
        edge_size=0.30, checker_verdict="reject", checker_confidence=0.6,
        action_taken=action, counterfactual_price_cents=price,
        counterfactual_direction=side,
    ))


class ResolvedClient:
    """Every market resolved YES unless told otherwise."""

    def __init__(self, results=None):
        self.results = results or {}
        self.default = "yes"
        self.lookups: list[str] = []

    def get_market(self, ticker):
        self.lookups.append(ticker)
        return {"market": {"ticker": ticker,
                           "result": self.results.get(ticker, self.default),
                           "close_time": "2026-08-17T00:00:00Z"}}

    def get_settlements(self, **kw):
        return {"settlements": [], "cursor": None}


# -- the rows that were previously invisible -------------------------------


def test_a_dry_run_row_is_gradeable(store, order_store):
    """The case that made the whole paper period unmeasurable."""
    edge_id = record(store, action="dry_run")
    ledger = Ledger(ResolvedClient(), store, order_store)

    assert ledger.reconcile_forecasts() == 1

    row = store.recent_edges()[0]
    assert row["id"] == edge_id
    assert row["settled"] == 1
    assert row["outcome"] == "yes"


def test_a_refused_row_is_gradeable(store, order_store):
    """On the live configuration this is every row there is."""
    record(store, action="skipped_risk")
    ledger = Ledger(ResolvedClient(), store, order_store)

    assert ledger.reconcile_forecasts() == 1


def test_an_executed_row_is_left_to_the_fill_based_path(store, order_store):
    """Real trades settle from real fills, with real fees. This must not
    overwrite them with a counterfactual."""
    record(store, action="executed")
    ledger = Ledger(ResolvedClient(), store, order_store)

    assert ledger.reconcile_forecasts() == 0


def test_a_row_without_a_probability_is_not_graded(store, order_store):
    store.record_edge(EdgeRecord(ticker="KXTEST-1", action_taken="skipped_risk"))
    ledger = Ledger(ResolvedClient(), store, order_store)

    assert ledger.reconcile_forecasts() == 0


def test_an_unresolved_market_is_left_alone(store, order_store):
    record(store, action="dry_run")

    class Open(ResolvedClient):
        def get_market(self, ticker):
            return {"market": {"ticker": ticker, "result": ""}}

    assert Ledger(Open(), store, order_store).reconcile_forecasts() == 0
    assert store.recent_edges()[0]["settled"] == 0


def test_grading_is_idempotent(store, order_store):
    record(store, action="dry_run")
    ledger = Ledger(ResolvedClient(), store, order_store)

    assert ledger.reconcile_forecasts() == 1
    assert ledger.reconcile_forecasts() == 0, "a settled row is not re-graded"


# -- counterfactual PnL ----------------------------------------------------


def test_a_winning_forecast_pays_the_rest_of_the_dollar():
    pnl = _counterfactual_pnl(
        {"counterfactual_price_cents": 40.0, "counterfactual_direction": "yes"}, "yes"
    )

    # 100c payout - 40c paid - fee, in dollars.
    assert 0.55 < pnl < 0.60


def test_a_losing_forecast_loses_the_stake_and_the_fee():
    pnl = _counterfactual_pnl(
        {"counterfactual_price_cents": 40.0, "counterfactual_direction": "yes"}, "no"
    )

    assert -0.43 < pnl < -0.40


def test_the_no_side_is_scored_against_the_no_outcome():
    win = _counterfactual_pnl(
        {"counterfactual_price_cents": 60.0, "counterfactual_direction": "no"}, "no"
    )
    lose = _counterfactual_pnl(
        {"counterfactual_price_cents": 60.0, "counterfactual_direction": "no"}, "yes"
    )

    assert win > 0 and lose < 0


def test_fees_are_subtracted_using_the_live_formula():
    """Paper results must not be flattered by pretending trading is free."""
    from core.pricing import fee_cents_per_contract

    gross = (100.0 - 50.0) / 100.0
    net = _counterfactual_pnl(
        {"counterfactual_price_cents": 50.0, "counterfactual_direction": "yes"}, "yes"
    )

    assert net == pytest.approx(gross - fee_cents_per_contract(50.0) / 100.0)


def test_a_row_with_no_captured_price_yields_no_pnl():
    """Calibration on the probability alone is still possible; inventing a
    price to fill the column would be worse than leaving it empty."""
    assert _counterfactual_pnl(
        {"counterfactual_price_cents": None, "counterfactual_direction": "yes"}, "yes"
    ) is None
    assert _counterfactual_pnl(
        {"counterfactual_price_cents": 40.0, "counterfactual_direction": ""}, "yes"
    ) is None


# -- the modes stay apart --------------------------------------------------


def test_paper_and_refused_and_live_are_reported_separately(store, order_store):
    for action in ("dry_run", "skipped_risk", "executed"):
        record(store, action=action, ticker=f"KX{action}-1")
    # Settle the executed one the way the fill path would.
    live_id = [e["id"] for e in store.recent_edges()
               if e["action_taken"] == "executed"][0]
    store.settle(live_id, "yes", 0.55)
    Ledger(ResolvedClient(), store, order_store).reconcile_forecasts()

    modes = {r["mode"] for r in store.calibration_by_category()}

    assert modes == {"live", "paper", "refused"}


def test_a_paper_result_cannot_be_summed_into_a_live_one(store, order_store):
    record(store, action="dry_run", ticker="KXA-1")
    record(store, action="executed", ticker="KXB-1")
    live_id = [e["id"] for e in store.recent_edges()
               if e["action_taken"] == "executed"][0]
    store.settle(live_id, "yes", 999.0)
    Ledger(ResolvedClient(), store, order_store).reconcile_forecasts()

    rows = {r["mode"]: r for r in store.calibration_by_category()}

    assert rows["live"]["total_pnl"] == pytest.approx(999.0)
    assert rows["paper"]["total_pnl"] != pytest.approx(999.0)


def test_brier_is_computed_over_graded_forecasts(store, order_store):
    """The number the whole change exists to make available."""
    record(store, action="dry_run", probability=1.0, ticker="KXA-1")
    Ledger(ResolvedClient(), store, order_store).reconcile_forecasts()

    paper = [r for r in store.calibration_by_category() if r["mode"] == "paper"][0]

    assert paper["n"] == 1
    assert paper["brier_score"] == pytest.approx(0.0), "a confident correct call"


def test_a_confidently_wrong_call_scores_badly(store, order_store):
    record(store, action="dry_run", probability=1.0, ticker="KXA-1")
    Ledger(ResolvedClient(results={"KXA-1": "no"}), store, order_store).reconcile_forecasts()

    paper = [r for r in store.calibration_by_category() if r["mode"] == "paper"][0]

    assert paper["brier_score"] == pytest.approx(1.0)


# -- the work stays bounded ------------------------------------------------


def test_lookups_are_capped_per_pass(store, order_store):
    for i in range(40):
        record(store, action="dry_run", ticker=f"KXT{i}-1")
    client = ResolvedClient()

    Ledger(client, store, order_store).reconcile_forecasts(max_tickers=5)

    assert len(client.lookups) == 5


def test_rows_sharing_a_ticker_cost_one_lookup(store, order_store):
    for _ in range(6):
        record(store, action="dry_run", ticker="KXSAME-1")
    client = ResolvedClient()

    settled = Ledger(client, store, order_store).reconcile_forecasts(max_tickers=5)

    assert client.lookups == ["KXSAME-1"]
    assert settled == 6, "one lookup grades every row on that market"


def test_a_zero_cap_disables_grading(store, order_store):
    record(store, action="dry_run")
    client = ResolvedClient()

    assert Ledger(client, store, order_store).reconcile_forecasts(max_tickers=0) == 0
    assert client.lookups == []


def test_an_unreachable_market_does_not_stop_the_rest(store, order_store):
    from core.kalshi_client import KalshiAPIError

    record(store, action="dry_run", ticker="KXBAD-1")
    record(store, action="dry_run", ticker="KXGOOD-1")

    class Flaky(ResolvedClient):
        def get_market(self, ticker):
            if ticker == "KXBAD-1":
                raise KalshiAPIError(503, "down")
            return super().get_market(ticker)

    assert Ledger(Flaky(), store, order_store).reconcile_forecasts() == 1


# -- the reconciler must reach markets that actually resolved ---------------
#
# #24 built forecast grading and it produced ZERO rows in the entire history
# of the bot, including a 4.4-hour uninterrupted run during which crypto
# hourly markets resolved every hour.
#
# The cause was head-of-line blocking. unsettled_forecast_edges selected
# oldest-first with no close-time filter, and reconcile_forecasts takes only
# the first FORECAST_RECONCILE_MAX_TICKERS (25) distinct tickers from it. A
# market that is still open returns no result and nothing records that it was
# asked, so the same 25 oldest tickers were re-queried every pass forever. In
# production the oldest rows were multi-day contracts and cross-category
# parlay shards — markets resolving months out, or never — so the window sat
# on them permanently while resolvable markets waited at the tail.


def _edge(store, ticker, close_time, action="skipped_risk"):
    from memory.edge_store import EdgeRecord

    return store.record_edge(EdgeRecord(
        ticker=ticker,
        category="Crypto",
        maker_probability=0.6,
        market_implied_probability=0.5,
        action_taken=action,
        counterfactual_price_cents=50.0,
        counterfactual_direction="yes",
        close_time=close_time,
    ))


def test_markets_that_cannot_have_resolved_are_not_queued(edge_store):
    """A contract four days out cannot have resolved, so asking about it is
    a wasted call that also costs a slot in the reconcile window."""
    import time

    now = time.time()
    _edge(edge_store, "KXBTCD-FUTURE", now + 4 * 86400)

    assert edge_store.unsettled_forecast_edges() == []


def test_a_long_dated_row_cannot_block_a_resolved_one(edge_store):
    """The exact production failure, in miniature: an old row for a market
    that resolves months out, and a newer row for one that closed an hour
    ago. Oldest-first ordering put the first one permanently in front."""
    import time

    now = time.time()
    _edge(edge_store, "KXMVECROSSCATEGORY-SHARD", now + 90 * 86400)
    _edge(edge_store, "KXBTC-RESOLVED", now - 3600)

    queued = [r["ticker"] for r in edge_store.unsettled_forecast_edges()]

    assert queued == ["KXBTC-RESOLVED"]


def test_recently_closed_markets_come_first(edge_store):
    """Calibration wants recent outcomes, and crypto hourlies close
    constantly. Most-recently-closed first keeps the window on them."""
    import time

    now = time.time()
    _edge(edge_store, "OLD-CLOSE", now - 10 * 86400)
    _edge(edge_store, "JUST-CLOSED", now - 60)

    queued = [r["ticker"] for r in edge_store.unsettled_forecast_edges()]

    assert queued[0] == "JUST-CLOSED"


def test_legacy_rows_fall_back_to_creation_time_rather_than_sorting_last(
    edge_store
):
    """Rows written before close_time existed carry NULL.

    Sorting them behind every non-NULL row made "last" permanent in practice:
    crypto hourlies close continuously and kept arriving ahead of them, so a
    legacy row was never reached. A row that can never be reached is not
    deprioritised, it is dropped — silently, which is the property this whole
    mechanism exists to avoid.

    They now order by created_at, which is the closest stand-in the row
    carries for when its market resolved.
    """
    import time

    now = time.time()
    _edge(edge_store, "LEGACY-RECENT", None)
    _edge(edge_store, "CLOSED-OLD", now - 10 * 86400)

    queued = [r["ticker"] for r in edge_store.unsettled_forecast_edges()]

    # The legacy row was created just now, so it outranks a market that
    # closed ten days ago rather than queueing behind it.
    assert queued == ["LEGACY-RECENT", "CLOSED-OLD"]


def test_a_recently_closed_market_still_outranks_an_older_legacy_row(
    edge_store
):
    """The fallback must not invert the main ordering: calibration still wants
    the most recently resolved markets first."""
    import sqlite3
    import time

    now = time.time()
    legacy = _edge(edge_store, "LEGACY-OLD", None)
    with sqlite3.connect(edge_store.db_path) as c:
        c.execute("UPDATE edges SET created_at=? WHERE id=?",
                  (now - 10 * 86400, legacy))
    _edge(edge_store, "JUST-CLOSED", now - 60)

    queued = [r["ticker"] for r in edge_store.unsettled_forecast_edges()]

    assert queued == ["JUST-CLOSED", "LEGACY-OLD"]


def test_executed_rows_are_still_excluded(edge_store):
    """Unchanged: real fills settle through the fill path, not this one."""
    import time

    _edge(edge_store, "TRADED", time.time() - 60, action="executed")

    assert edge_store.unsettled_forecast_edges() == []


def test_close_time_survives_a_round_trip(edge_store):
    """The column has to actually persist, or the filter silently treats
    every row as legacy and the blocking comes back."""
    import time

    close = time.time() - 120
    _edge(edge_store, "KXBTC-X", close)

    row = edge_store.unsettled_forecast_edges()[0]

    assert row["close_time"] == pytest.approx(close)


# -- the calibration table has to be readable ------------------------------


def test_settling_a_forecast_logs_the_row_and_the_table(ledger, edge_store,
                                                        client, caplog):
    """Grading that nobody can read answers nothing.

    The whole point of #24 is to say whether the gates are refusing correctly,
    and until now that measurement could only be read by opening the database
    on the production volume. Meanwhile the aggregate "Graded 2 forecast
    row(s)" cannot answer the question the rows exist for: the Checker
    rejected a 45-point edge on KXHIGHCHI-T78 — was it right?
    """
    import time

    from memory.edge_store import EdgeRecord

    edge_store.record_edge(EdgeRecord(
        ticker="KXHIGHCHI-26AUG17-T78",
        category="Weather",
        source="llm",
        maker_probability=0.88,
        market_implied_probability=0.425,
        action_taken="skipped_risk",
        counterfactual_price_cents=43.0,
        counterfactual_direction="yes",
        close_time=time.time() - 3600,
    ))
    client.markets["KXHIGHCHI-26AUG17-T78"] = {
        "ticker": "KXHIGHCHI-26AUG17-T78", "result": "no",
    }

    with caplog.at_level("INFO"):
        assert ledger.reconcile_forecasts() == 1

    # The individual outcome, with everything needed to judge the refusal.
    assert "KXHIGHCHI-26AUG17-T78" in caplog.text
    assert "model said 88%" in caplog.text
    assert "outcome NO" in caplog.text
    # And the table it rolls up into.
    assert "[refused]" in caplog.text and "Calibration" in caplog.text
    assert "Weather" in caplog.text


def test_a_broken_calibration_read_cannot_stop_grading(ledger, edge_store,
                                                       client, caplog,
                                                       monkeypatch):
    """Reporting is observability, not control flow. A failure printing the
    summary must not roll back settlements that already committed."""
    import time

    from memory.edge_store import EdgeRecord

    edge_store.record_edge(EdgeRecord(
        ticker="KXBTC-X", category="Crypto", maker_probability=0.6,
        market_implied_probability=0.5, action_taken="skipped_risk",
        counterfactual_price_cents=50.0, counterfactual_direction="yes",
        close_time=time.time() - 60,
    ))
    client.markets["KXBTC-X"] = {"ticker": "KXBTC-X", "result": "yes"}
    monkeypatch.setattr(
        edge_store, "calibration_by_category",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with caplog.at_level("INFO"):
        assert ledger.reconcile_forecasts() == 1


# -- regime split ----------------------------------------------------------
#
# The first refused-mode row ever produced read
#
#     n=64  brier=0.175  said 46% actual 45%  pnl $12.75
#
# which reads as the gates refusing winners. It spans 2026-08-17T17:00Z,
# before which sigma was 6.6% annualized and every crypto probability was
# pinned at 0% or 100%. One number across two models describes neither, so
# the table is logged as two and never merged.


def _edge_at(store, ticker, created_at, prob, close_time):
    """Insert a settled-able row with an explicit created_at."""
    import sqlite3

    from memory.edge_store import EdgeRecord

    edge_id = store.record_edge(EdgeRecord(
        ticker=ticker, category="Crypto", source="quant",
        maker_probability=prob, market_implied_probability=0.5,
        action_taken="skipped_risk", counterfactual_price_cents=50.0,
        counterfactual_direction="yes", close_time=close_time,
    ))
    with sqlite3.connect(store.db_path) as c:
        c.execute("UPDATE edges SET created_at=? WHERE id=?",
                  (created_at, edge_id))
    return edge_id


def test_the_window_selects_by_when_the_forecast_was_made(edge_store):
    """Not by when it settled — what decides the regime is which model
    produced the probability."""
    boundary = 1_000_000.0
    old = _edge_at(edge_store, "OLD", boundary - 100, 0.9, boundary)
    new = _edge_at(edge_store, "NEW", boundary + 100, 0.6, boundary)
    edge_store.settle(old, "no", -0.5)
    edge_store.settle(new, "yes", 0.5)

    before = edge_store.calibration_by_category(until=boundary)
    after = edge_store.calibration_by_category(since=boundary)

    assert [r["n"] for r in before] == [1]
    assert [r["n"] for r in after] == [1]
    assert before[0]["total_pnl"] == pytest.approx(-0.5)
    assert after[0]["total_pnl"] == pytest.approx(0.5)


def test_the_boundary_is_exclusive_on_one_side_only(edge_store):
    """A row exactly on the boundary belongs to the post-fix regime, and to
    exactly one of the two tables — no row may be counted twice."""
    boundary = 1_000_000.0
    exact = _edge_at(edge_store, "EXACT", boundary, 0.6, boundary)
    edge_store.settle(exact, "yes", 0.5)

    before = edge_store.calibration_by_category(until=boundary)
    after = edge_store.calibration_by_category(since=boundary)

    assert before == []
    assert [r["n"] for r in after] == [1]


def test_an_unbounded_call_still_returns_everything(edge_store):
    """Unset boundary collapses to the previous behaviour."""
    boundary = 1_000_000.0
    a = _edge_at(edge_store, "A", boundary - 100, 0.9, boundary)
    b = _edge_at(edge_store, "B", boundary + 100, 0.6, boundary)
    edge_store.settle(a, "no", -0.5)
    edge_store.settle(b, "yes", 0.5)

    assert [r["n"] for r in edge_store.calibration_by_category()] == [2]


def test_both_tables_are_logged_and_labelled(ledger, edge_store, client,
                                             caplog, monkeypatch):
    """The property the split exists for: two labelled tables, never one
    merged number."""
    import time

    from config import CONFIG

    now = time.time()
    monkeypatch.setattr(CONFIG.risk, "calibration_regime_split_at",
                        "2026-08-17T17:00:00Z")
    _edge_at(edge_store, "KXBTC-OLD", 1_000_000.0, 0.99, now - 60)
    client.markets["KXBTC-OLD"] = {"ticker": "KXBTC-OLD", "result": "no"}

    with caplog.at_level("INFO"):
        ledger.reconcile_forecasts()

    assert "Calibration (pre-fix)" in caplog.text
    assert "Calibration (post-fix)" in caplog.text


def test_an_unparseable_boundary_falls_back_to_one_table(ledger, edge_store,
                                                         client, caplog,
                                                         monkeypatch):
    """Two tables labelled by a boundary nobody set would be worse than one
    honest table."""
    import time

    from config import CONFIG

    now = time.time()
    monkeypatch.setattr(CONFIG.risk, "calibration_regime_split_at", "not-a-date")
    _edge_at(edge_store, "KXBTC-Y", now - 3600, 0.6, now - 60)
    client.markets["KXBTC-Y"] = {"ticker": "KXBTC-Y", "result": "yes"}

    with caplog.at_level("INFO"):
        ledger.reconcile_forecasts()

    assert "Calibration (all)" in caplog.text
    assert "pre-fix" not in caplog.text


# -- the boundary the config actually ships ---------------------------------


def test_the_shipped_boundary_is_the_weather_prompt_rewrite_not_the_sigma_fix():
    """CALIBRATION_REGIME_SPLIT_AT defaulted to 2026-08-17T17:00:00Z (the
    sigma fix, #41) from the day it was introduced until this test existed.
    In between, e4cade6 (2026-08-21T19:01 UTC) rewrote the weather prompt's
    error-band guidance from an invented 3-4F to a measured 1-2F — a second
    change to what the Maker's own numbers mean — and the boundary never
    moved to follow it. Every calibration table logged since 2026-08-21 has
    been silently pooling rows written under both prompts into one "post-fix"
    number.

    This pins the boundary at the later date so a future edit that reverts it
    (or moves it to some other commit) fails loudly here instead of silently
    contaminating the calibration table again.
    """
    import dataclasses

    from config import RiskConfig

    field = next(f for f in dataclasses.fields(RiskConfig)
                 if f.name == "calibration_regime_split_at")
    default = field.default

    assert default == "2026-08-21T19:01:00Z"


def test_the_shipped_boundary_parses_and_sits_after_the_prompt_rewrite():
    """Pins the boundary as a working timestamp rather than a string that
    happens to match the test above — a typo that still equality-matched
    would pass the previous test and silently collapse to one table."""
    from datetime import datetime, timezone

    from config import RiskConfig
    from core.validation import parse_timestamp

    field_default = next(
        f.default for f in __import__("dataclasses").fields(RiskConfig)
        if f.name == "calibration_regime_split_at"
    )
    parsed = parse_timestamp(field_default)
    e4cade6_deployed_at = datetime(
        2026, 8, 21, 19, 0, 48, tzinfo=timezone.utc
    ).timestamp()

    assert parsed is not None
    assert parsed > e4cade6_deployed_at
