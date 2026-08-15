"""
P1-8: replay/backtest evidence.

The brief says: report calibration, Brier score, net PnL after fees,
drawdown, fill assumptions and sensitivity to stale or delayed quotes — and
"do not claim profitability without this evidence".

These tests do NOT claim profitability. The world here is synthetic, so a
profit in it is a statement about the arithmetic, not about Kalshi. What they
establish is the negative: if the pricing and accounting cannot pass these,
the quant path should not trade. The most important one is the null
hypothesis — against a fairly priced book, a correct model must NOT show a
profit. A backtest that prints gains on a fair market has broken accounting,
and that is the failure mode this catches.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from backtest.replay import run_replay, true_probability_above


# -- the null hypothesis ----------------------------------------------------


def test_a_correct_model_has_no_edge_in_a_fairly_priced_market():
    """The single most important check in this file.

    The book is centred on the true probability. A model that knows the true
    probability therefore has no informational advantage, and after crossing
    the spread and paying fees it must not find trades. If this ever starts
    printing profits, the edge or cost accounting is wrong somewhere.
    """
    report = run_replay(n_markets=400)

    assert report.n == 0, (
        f"a correct model found {report.n} 'edges' in a fair market — "
        f"accounting bug"
    )
    assert report.skipped_no_edge == report.considered


def test_a_fair_market_stays_edgeless_across_spreads():
    for spread in (1.0, 2.0, 4.0, 8.0):
        report = run_replay(n_markets=200, spread_cents=spread)
        assert report.n == 0, f"spread {spread}c produced {report.n} trades"


# -- a genuinely mispriced market ------------------------------------------


def test_a_mispriced_market_is_traded_and_profitable():
    """Sanity in the other direction: given a book systematically 8c away
    from fair, a correct model should find and profit from it. If this shows
    a loss, the direction or sizing logic is inverted somewhere."""
    report = run_replay(n_markets=400, mispricing_cents=8.0)

    assert report.n > 100
    assert report.net_pnl > 0
    assert report.gross_pnl > report.net_pnl, "fees must reduce PnL"


def test_fees_are_actually_charged():
    report = run_replay(n_markets=400, mispricing_cents=8.0)
    assert report.total_fees > 0
    assert report.net_pnl == pytest.approx(report.gross_pnl - report.total_fees)


def test_drawdown_is_reported_and_negative_or_zero():
    report = run_replay(n_markets=400, mispricing_cents=8.0)
    assert report.max_drawdown <= 0
    # A real strategy has some losing runs; a drawdown of exactly zero over
    # 300+ trades would mean the equity curve never dips, which is not a
    # plausible outcome and would suggest outcomes are not being sampled.
    assert report.max_drawdown < 0


# -- calibration ------------------------------------------------------------


def test_the_model_is_calibrated_against_its_own_process():
    """The model is handed the true parameters of the process that generates
    the outcomes, so it should be well calibrated. A Brier score at or above
    0.25 — what a model that always says 50% scores — would mean the pricing
    code is wrong, since the model cannot be wrong about a process it is
    exactly right about."""
    report = run_replay(n_markets=600, mispricing_cents=8.0)

    assert report.brier_score is not None
    assert report.brier_score < 0.25, report.summary()


def test_calibration_buckets_track_reality():
    report = run_replay(n_markets=800, mispricing_cents=8.0)
    buckets = [b for b in report.calibration_buckets() if b["n"] >= 20]

    assert buckets, "not enough trades to bucket"
    for row in buckets:
        # Wide tolerance: these are finite samples, not a convergence proof.
        assert abs(row["error"]) < 0.20, f"{row} in\n{report.summary()}"


def test_mis_estimating_volatility_degrades_calibration():
    """Sensitivity check. Volatility is the input most likely to be wrong in
    production — it is estimated from a short rolling buffer — so the harness
    should show that getting it wrong hurts, and roughly how much."""
    good = run_replay(n_markets=600, mispricing_cents=8.0, vol_error=1.0)
    bad = run_replay(n_markets=600, mispricing_cents=8.0, vol_error=0.4)

    good_tail_error = _tail_error(good)
    bad_tail_error = _tail_error(bad)

    assert bad_tail_error > good_tail_error, (
        f"underestimating vol should hurt tail calibration\n"
        f"good={good_tail_error:.3f} bad={bad_tail_error:.3f}"
    )


def _tail_error(report) -> float:
    """Mean absolute calibration error in the confident buckets.

    Vol errors show up at the extremes first: understating volatility makes
    the model too sure that a far strike will not be crossed.
    """
    rows = [
        b for b in report.calibration_buckets()
        if b["n"] >= 10 and (b["predicted"] > 0.8 or b["predicted"] < 0.2)
    ]
    if not rows:
        return 0.0
    return sum(abs(b["error"]) for b in rows) / len(rows)


# -- stale-quote sensitivity ------------------------------------------------


def test_stale_quotes_are_skipped_entirely():
    report = run_replay(
        n_markets=200, mispricing_cents=8.0,
        quote_delay_seconds=CONFIG.risk.max_quote_age_seconds + 60,
    )
    assert report.n == 0
    assert report.skipped_stale == report.considered


def test_quotes_inside_the_freshness_window_still_trade():
    report = run_replay(
        n_markets=200, mispricing_cents=8.0,
        quote_delay_seconds=max(CONFIG.risk.max_quote_age_seconds - 5, 0),
    )
    assert report.skipped_stale == 0
    assert report.n > 0


def test_the_staleness_threshold_is_what_decides():
    """Guards against the freshness check being accidentally disconnected."""
    CONFIG.risk.max_quote_age_seconds = 30
    fresh = run_replay(n_markets=100, mispricing_cents=8.0, quote_delay_seconds=10)
    stale = run_replay(n_markets=100, mispricing_cents=8.0, quote_delay_seconds=45)

    assert fresh.skipped_stale == 0
    assert stale.skipped_stale == 100


# -- cost sensitivity -------------------------------------------------------


def test_a_wider_spread_leaves_less_edge():
    tight = run_replay(n_markets=400, mispricing_cents=8.0, spread_cents=1.0)
    wide = run_replay(n_markets=400, mispricing_cents=8.0, spread_cents=6.0)

    assert wide.n <= tight.n
    assert wide.net_pnl < tight.net_pnl


def test_raising_the_fee_rate_reduces_profit():
    baseline = run_replay(n_markets=400, mispricing_cents=8.0)
    CONFIG.risk.fee_rate = 0.30
    expensive = run_replay(n_markets=400, mispricing_cents=8.0)

    assert expensive.net_pnl < baseline.net_pnl
    assert expensive.n < baseline.n, "fewer trades should clear a higher fee bar"


def test_the_reported_summary_covers_every_required_metric():
    """The brief lists exactly what a backtest must report."""
    summary = run_replay(n_markets=200, mispricing_cents=8.0).summary()

    for required in ("net PnL after fees", "fees", "Brier score",
                     "max drawdown", "predicted", "actual"):
        assert required in summary, required


# -- the model itself -------------------------------------------------------


def test_closed_form_probability_is_sane():
    # Deep in the money.
    assert true_probability_above(100.0, 50.0, 1e-4, 3600) > 0.99
    # Far out of the money.
    assert true_probability_above(100.0, 200.0, 1e-4, 3600) < 0.01
    # At the money is a coin flip.
    assert true_probability_above(100.0, 100.0, 1e-4, 3600) == pytest.approx(0.5, abs=0.01)


def test_more_time_moves_probability_toward_a_coin_flip():
    near = true_probability_above(100.0, 110.0, 1e-4, 600)
    far = true_probability_above(100.0, 110.0, 1e-4, 86_400)
    assert far > near
