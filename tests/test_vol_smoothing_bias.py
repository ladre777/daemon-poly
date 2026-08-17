"""
The volatility estimate was three times too small, and the feed is why.

What production did on 2026-08-17, the first pass ever to run with a warm
volatility clock (`quant 99 (no proposal 0)` — every crypto candidate priced,
for the first time in the bot's history):

    Refusing KXBTCD-26AUG2117-T65499.99: model says 0% against a market at
      19.0% — 5.46 in log-odds, over the 3.00 limit
    Refusing KXBTCD-26AUG2117-T62499.99: model says 100% against a market at
      84.5% — 5.21 in log-odds, over the 3.00 limit

Every crypto proposal, both assets, every horizon, always the same direction:
the model collapsing to 0% or 100% where the market priced 7-94%.

Back-solving sigma from two *adjacent* strikes on `KXBTCD-26AUG1713` — ten
minutes to expiry, $100 apart, a derivation that cancels spot and so assumes
nothing:

    market  3.27e-5 per second   (18.4% annualized)
    model   1.18e-5 per second   ( 6.6% annualized)

and the model's sigma was the same 1.18e-5 at four days as at ten minutes, so
the sqrt(t) scaling was fine. The level was wrong, by a roughly constant ~3x.

The cause is that BRTI is not a raw print. It is a deliberately smoothed
cross-venue aggregate, and we sampled it every 5 seconds — well inside its
smoothing window, where a trailing average barely moves between reads.

These tests construct that exact pathology from a known-vol path and show
three things: the old estimator understates it by ~3x, the estimator itself
was never wrong (an unsmoothed path measures correctly at every interval),
and measuring across a ladder of sampling intervals recovers the truth.
"""
from __future__ import annotations

import math
import random

import pytest

from config import CONFIG
from core.spot_price_client import PriceHistory

SECONDS_PER_YEAR = 365 * 24 * 3600
TRUE_ANNUAL_VOL = 0.20


def annualize(per_second: float) -> float:
    return per_second * math.sqrt(SECONDS_PER_YEAR)


def diffusion(n=3600, dt=1.0, annual_vol=TRUE_ANNUAL_VOL, seed=7, start=0.0):
    """A price path with a known volatility, one point per second."""
    rng = random.Random(seed)
    sigma = annual_vol / math.sqrt(SECONDS_PER_YEAR)
    price, out = 64000.0, []
    for i in range(n):
        price *= math.exp(sigma * math.sqrt(dt) * rng.gauss(0, 1))
        out.append((start + i * dt, price))
    return out


def smoothed(points, window):
    """What a CF Benchmarks-style index publishes: each print is an average
    over the trailing `window` seconds, not the instantaneous price."""
    out, buf = [], []
    for at, price in points:
        buf.append((at, price))
        buf = [(t, p) for t, p in buf if t > at - window]
        out.append((at, sum(p for _, p in buf) / len(buf)))
    return out


def history_from(points, sample_seconds=5.0, now=None):
    """Load a path into a PriceHistory, downsampled the way `record_tick`
    downsamples the live feed."""
    import time

    now = now if now is not None else time.time()
    end = points[-1][0]
    h = PriceHistory(maxlen=100_000)
    last = None
    for at, price in points:
        if last is not None and at - last < sample_seconds:
            continue
        # Rebase onto wall clock: realized_vol filters on a lookback from now.
        h.add(price, now - (end - at))
        last = at
    return h


@pytest.fixture(autouse=True)
def wide_outlier_band():
    """The outlier filter is not under test here and a synthetic path can
    drift past it over an hour."""
    CONFIG.risk.spot_outlier_ratio = 100.0


# -- the estimator was never the problem -----------------------------------


def test_an_unsmoothed_feed_measures_correctly_at_every_interval():
    """The control. On a raw price series the sampling interval does not
    matter — which is what says the ~3x error came from the data, not from
    the arithmetic in realized_vol."""
    h = history_from(diffusion())

    for interval in (0.0, 30.0, 60.0, 120.0):
        vol = h.realized_vol(3600, sample_seconds=interval)
        assert vol is not None
        assert 0.13 < annualize(vol) < 0.30, f"{interval}s -> {annualize(vol):.1%}"


def test_an_unsmoothed_feed_is_unchanged_by_the_robust_estimate():
    """No behaviour change where there was no problem: on a clean feed the
    robust estimate is the same number the old one produced."""
    h = history_from(diffusion())

    plain = h.realized_vol(3600)
    robust = h.realized_vol_robust(3600)

    assert robust == pytest.approx(plain, rel=0.35)


# -- the pathology, reproduced ---------------------------------------------


def test_smoothing_understates_volatility_at_the_tick_interval():
    """The bug. A 60-second trailing average sampled every 5 seconds reads
    about a third of the truth — which is the production number."""
    h = history_from(smoothed(diffusion(), window=60))

    measured = annualize(h.realized_vol(3600))

    assert measured < 0.5 * TRUE_ANNUAL_VOL
    assert 2.0 < TRUE_ANNUAL_VOL / measured < 5.0


def test_the_understatement_matches_what_production_showed():
    """Pinning the diagnosis, not just the direction. Production measured
    6.6% annualized against a market-implied 18.4%; a 60s smoothing window
    sampled at 5s reproduces that ratio."""
    h = history_from(smoothed(diffusion(), window=60))

    ratio = TRUE_ANNUAL_VOL / annualize(h.realized_vol(3600))

    assert 2.5 < ratio < 4.0


def test_worse_smoothing_understates_further():
    """Monotone in the smoothing window, as an averaging explanation
    requires. If this ever fails, the mechanism is not what we think."""
    def measured(window):
        return annualize(history_from(smoothed(diffusion(), window)).realized_vol(3600))

    assert measured(120) < measured(60) < measured(30) < measured(1)


# -- the fix ---------------------------------------------------------------


def test_the_signature_climbs_with_the_sampling_interval():
    """The diagnostic. On a damped feed the estimate rises as the sampling
    interval leaves the smoothing window behind, then flattens."""
    h = history_from(smoothed(diffusion(), window=60))

    signature = [v for _, v in h.vol_signature(3600) if v]

    assert len(signature) >= 4
    assert signature[-1] > 2 * signature[0]


def test_the_robust_estimate_recovers_the_true_volatility():
    """The property the change exists for."""
    h = history_from(smoothed(diffusion(), window=60))

    recovered = annualize(h.realized_vol_robust(3600))

    assert 0.7 * TRUE_ANNUAL_VOL < recovered < 1.3 * TRUE_ANNUAL_VOL


def test_the_robust_estimate_never_reads_lower_than_the_raw_one():
    """Averaging destroys variance and cannot create it, so the maximum
    across the ladder is the least-damped estimate. It must never come back
    below where it started."""
    for window in (1, 30, 60, 120):
        h = history_from(smoothed(diffusion(), window))
        assert h.realized_vol_robust(3600) >= h.realized_vol(3600)


def test_the_recovered_sigma_would_no_longer_trip_the_coherence_gate():
    """End of the causal chain. With the true sigma restored, the model's
    probability on the ten-minute strike that produced 'model says 100%
    against a market at 84.5%' lands near the market instead of pinned at
    the clip."""
    h = history_from(smoothed(diffusion(), window=60))
    sigma = h.realized_vol_robust(3600)

    # KXBTCD-26AUG1713-T63999.99: ~600s to expiry, market 84.5%.
    spot, strike, horizon = 64050.0, 63999.99, 600.0
    z = math.log(strike / spot) / (sigma * math.sqrt(horizon))
    prob_above = 1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))

    assert 0.02 < prob_above < 0.98, f"still pinned at {prob_above:.1%}"


# -- fail-closed sanity band -----------------------------------------------


def quant_with_vol(annual_vol, seed=3):
    """A QuantMaker whose btc history has a known annualized volatility, and
    a fresh spot quote to price against."""
    import time

    from core.spot_price_client import SpotPriceClient, SpotQuote
    from workers.quant_maker import QuantMaker

    from tests.test_quant_path import FakeHTTP

    CONFIG.risk.quant_allow_unverified = True
    CONFIG.risk.spot_outlier_ratio = 100.0

    client = SpotPriceClient(http=FakeHTTP())
    history = PriceHistory(maxlen=100_000)
    now = time.time()
    points = diffusion(n=3600, annual_vol=annual_vol, seed=seed)
    end = points[-1][0]
    for at, price in points[::30]:
        history.add(price, at=now - (end - at))
    client.history["btc"] = history
    last = points[-1][1]
    client._quotes["btc"] = SpotQuote(symbol="btc", price=last,
                                      observed_at=now, source="crypto")
    return QuantMaker(client), last


def test_an_implausible_estimate_is_refused_rather_than_used(caplog):
    """A 6.6%-annualized bitcoin is not a market disagreement, it is a broken
    estimate, and the honest response is to decline. This is the guard that
    would have caught the whole thing on the first pass."""
    from tests.test_quant_path import candidate

    quant, spot = quant_with_vol(0.02)

    with caplog.at_level("WARNING"):
        result = quant.propose(candidate(strike=spot * 1.001))

    assert result is None
    assert "outside the plausible band" in caplog.text


def test_a_plausible_estimate_still_prices(caplog):
    """The band must not have turned into a blanket refusal — a normal
    bitcoin regime prices exactly as before."""
    from tests.test_quant_path import candidate

    quant, spot = quant_with_vol(0.40)

    result = quant.propose(candidate(strike=spot * 1.001))

    assert result is not None
    assert 0.0 < result.probability_yes < 1.0


def test_the_band_would_have_caught_the_production_reading():
    """6.6% annualized was what production actually measured."""
    assert CONFIG.risk.min_plausible_annual_vol > 0.066


def test_the_band_is_wide_enough_not_to_bind_normally():
    """Bitcoin runs 20-100% annualized. The band must not refuse real
    regimes — it is a backstop, not a filter."""
    for annual in (0.15, 0.20, 0.50, 1.0, 2.0):
        assert (CONFIG.risk.min_plausible_annual_vol
                <= annual
                <= CONFIG.risk.max_plausible_annual_vol)


def test_the_band_refuses_rather_than_clamping():
    """Stated behaviourally so nobody later 'fixes' this by clamping into
    range: clamping would invent a sigma nobody measured and hide the fault.
    An out-of-band estimate must yield no proposal at all — not a proposal
    computed at the boundary."""
    from tests.test_quant_path import candidate

    quant, spot = quant_with_vol(0.02)

    assert quant.propose(candidate(strike=spot * 1.001)) is None


# -- the buffer has to be able to hold the longer intervals -----------------


def test_every_rung_can_clear_the_observation_bar_within_retention():
    """A maximum across rungs is only as trustworthy as its noisiest rung: a
    low reading is discarded, a spuriously high one is taken. So every
    interval on the ladder must be able to muster comfortably more than
    MIN_VOL_OBSERVATIONS inside the retention window.

    This is why the ladder stops at 120s. On the live feed the 300s rung read
    12% against 120s's 16% — noise, from the 9-12 observations an hour of
    retention leaves it.
    """
    retention = CONFIG.risk.vol_history_retention_seconds

    for interval in CONFIG.risk.vol_sample_intervals:
        if not interval:
            continue
        observations = retention / interval
        assert observations >= 2 * CONFIG.risk.min_vol_observations, (
            f"{interval}s rung gets only {observations:.0f} observations "
            f"in a {retention:.0f}s window"
        )


def test_the_ladder_reaches_past_the_feeds_smoothing():
    """It must still climb far enough out to leave the smoothing window, or
    the whole exercise measures damped vol at every rung."""
    assert max(CONFIG.risk.vol_sample_intervals) >= 120.0


def test_the_buffer_spans_the_whole_retention_window():
    """A 300-second sampling rung needs ~3000s of span to clear
    MIN_VOL_OBSERVATIONS. At the 5s tick rate the old 500-point buffer capped
    the span at 2500s and put the top of the ladder permanently out of
    reach."""
    needed = (CONFIG.risk.vol_history_retention_seconds
              / CONFIG.risk.rti_tick_sample_seconds)

    assert PriceHistory.DEFAULT_MAXLEN >= needed


def test_the_lookback_changes_the_answer_so_it_must_be_reported():
    """Why the pricing path logs its own sigma.

    The quant path picks its lookback from each contract's horizon
    (`max(3600, min(seconds_to_expiry * 20, 86400))`), while the startup
    signature is measured over one window. On a buffer longer than the short
    window those disagree — which is not a bug in itself, but a diagnostic
    that reports a number other than the one setting prices is exactly how a
    6.6%-annualized bitcoin survived a whole session.

    So this pins the thing that made it invisible rather than the difference
    itself: the two are allowed to differ, and both have to be logged.
    """
    import inspect

    import main

    h = history_from(smoothed(diffusion(n=7200), window=60))

    short = h.realized_vol_robust(1800)
    long = h.realized_vol_robust(86400)

    assert short is not None and long is not None
    # Both are logged by _log_vol_signature, so whichever the quant path
    # picks, the operator can see it.
    source = inspect.getsource(main._log_vol_signature)
    assert "realized_vol_robust" in source
    assert "86400" in source


def test_the_quant_path_states_the_sigma_it_priced_with(caplog):
    """Per family per pass, not per market, and in annualized terms — the
    only units in which a wrong number is obvious at a glance."""
    from tests.test_quant_path import candidate

    import time

    from core.spot_price_client import SpotQuote

    quant, spot = quant_with_vol(0.40)
    # begin_pass clears the per-pass quote cache, so reseed after it.
    quant.begin_pass()
    quant.spot._quotes["btc"] = SpotQuote(symbol="btc", price=spot,
                                          observed_at=time.time(),
                                          source="crypto")

    with caplog.at_level("INFO"):
        quant.propose(candidate(strike=spot * 1.001))
        quant.propose(candidate(strike=spot * 1.002))

    assert "annualized" in caplog.text
    assert caplog.text.count("Pricing btc with sigma") == 1


def test_thinning_respects_irregular_spacing():
    """The live feed does not arrive on a clean grid, so thinning walks
    forward from the last kept point rather than slicing every Nth."""
    from core.spot_price_client import _thin

    points = [(0.0, 1.0), (1.0, 1.0), (9.0, 1.0), (10.0, 1.0), (25.0, 1.0)]

    kept = [t for t, _ in _thin(points, 10.0)]

    assert kept == [0.0, 10.0, 25.0]


def test_thinning_is_a_no_op_without_a_spacing():
    from core.spot_price_client import _thin

    points = [(0.0, 1.0), (1.0, 2.0)]

    assert _thin(points, 0.0) == points
    assert _thin([], 30.0) == []


# -- horizon guard ---------------------------------------------------------
#
# The last piece of the 2026-08-17 story. After the smoothing fix the estimate
# read 52% annualized while the market implied 28% on a four-day contract, and
# the reflex was to call the estimate wrong again. It was not. 52% realized
# over the trailing hour and 28% implied over the coming four days are
# different quantities and both were true — volatility mean-reverts, so an
# hour-long spike does not last four days.
#
# What was wrong was carrying a one-hour measurement 96x out to a four-day
# contract, which manufactured 15 points of phantom edge on out-of-the-money
# tails. The priority families need no extrapolation at all.


def quant_with_span(span_seconds, annual_vol=0.40):
    """A QuantMaker holding exactly `span_seconds` of history."""
    import time

    from core.spot_price_client import SpotPriceClient, SpotQuote
    from workers.quant_maker import QuantMaker

    from tests.test_quant_path import FakeHTTP

    CONFIG.risk.quant_allow_unverified = True
    CONFIG.risk.spot_outlier_ratio = 100.0

    client = SpotPriceClient(http=FakeHTTP())
    history = PriceHistory(maxlen=100_000)
    now = time.time()
    points = diffusion(n=int(span_seconds), annual_vol=annual_vol, seed=4)
    end = points[-1][0]
    for at, price in points[::30]:
        history.add(price, at=now - (end - at))
    client.history["btc"] = history
    last = points[-1][1]
    client._quotes["btc"] = SpotQuote(symbol="btc", price=last,
                                      observed_at=now, source="crypto")
    quant = QuantMaker(client)
    return quant, last


def candidate_expiring_in(seconds, strike):
    """A candidate whose close_time is `seconds` from now."""
    from datetime import datetime, timedelta, timezone

    from tests.test_quant_path import candidate

    close = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    c = candidate(strike=strike)
    c.close_time = close.isoformat().replace("+00:00", "Z")
    return c


def test_a_four_day_contract_is_refused_on_an_hour_of_history(caplog):
    """96x extrapolation. This is the case that produced phantom tail edge."""
    quant, spot = quant_with_span(3600)

    with caplog.at_level("INFO"):
        result = quant.propose(candidate_expiring_in(4 * 86400, spot * 1.001))

    assert result is None
    assert "over the" in caplog.text and "limit" in caplog.text


def test_the_fifteen_minute_family_is_untouched():
    """0.2x the observation span — the horizon the estimate is actually good
    for, and the family this bot is pointed at."""
    quant, spot = quant_with_span(3600)

    result = quant.propose(candidate_expiring_in(900, spot * 1.001))

    assert result is not None
    assert 0.0 < result.probability_yes < 1.0


def test_the_hourly_family_is_untouched():
    """1.0x. Also a priority family, also no extrapolation."""
    quant, spot = quant_with_span(3600)

    result = quant.propose(candidate_expiring_in(3600, spot * 1.001))

    assert result is not None


def test_the_guard_scales_with_the_history_actually_held():
    """It is a ratio, not a fixed horizon: a barely-warm bot prices only very
    short contracts, and reaches further as it accumulates history."""
    cold, spot = quant_with_span(700)

    assert cold.propose(candidate_expiring_in(3600, spot * 1.001)) is None

    warm, spot = quant_with_span(3600)
    assert warm.propose(candidate_expiring_in(3600, spot * 1.001)) is not None


def test_the_limit_admits_the_priority_families_and_excludes_multi_day():
    """The boundary, stated in the units that decide it."""
    span = CONFIG.risk.vol_history_retention_seconds
    ratio = CONFIG.risk.max_horizon_vol_span_ratio

    assert 900 / span <= ratio, "15-minute contracts must be priceable"
    assert 3600 / span <= ratio, "hourly contracts must be priceable"
    assert 86400 / span > ratio, "daily contracts must not be"


def test_the_refusal_is_logged_once_per_family_not_per_strike(caplog):
    """A daily ladder is dozens of strikes; it must not be dozens of lines."""
    quant, spot = quant_with_span(3600)

    with caplog.at_level("INFO"):
        for i in range(20):
            quant.propose(candidate_expiring_in(4 * 86400, spot * (1 + i / 1000)))

    assert caplog.text.count("is the wrong quantity") <= 1
    assert caplog.text.count("over the") <= 1
