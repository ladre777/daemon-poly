"""
P1-8: contract-specific, data-quality-aware quant path.

Two groups here. The first is about *semantics*: does the external spot
instrument actually settle the Kalshi contract, in the same units? The second
is about *data quality*: one quote per symbol per pass, timestamps, staleness,
backoff, outlier filtering, and a volatility estimate that knows its own
sampling interval.
"""
from __future__ import annotations

import time

import pytest

from config import CONFIG
from core.contract_specs import CONTRACT_SPECS, spec_for, usable
from core.spot_price_client import PriceHistory, SpotPriceClient, SpotQuote
from workers.quant_maker import QuantMaker
from workers.scout import Candidate


# -- contract specs ---------------------------------------------------------


def test_every_shipped_spec_is_marked_unverified():
    """Nothing in this repo has been checked against Kalshi's settlement
    rules, and the specs must say so rather than implying otherwise."""
    assert all(not s.verified for s in CONTRACT_SPECS)
    assert all(s.caveat for s in CONTRACT_SPECS)


def test_specs_are_ordered_most_specific_first():
    """KXBTCD must match before KXBTC, or the daily contract picks up the
    generic spec."""
    assert spec_for("KXBTCD-25AUG14-B").prefix == "KXBTCD"
    assert spec_for("KXBTC-25DEC31").prefix == "KXBTC"


def test_unmapped_family_has_no_spec():
    assert spec_for("KXNFLGAME-25") is None
    ok, why = usable(None)
    assert not ok and "no contract spec" in why


def test_unverified_specs_are_refused_by_default():
    ok, why = usable(spec_for("KXBTCD-25AUG14-B"))
    assert not ok
    assert "unverified" in why


def test_unverified_specs_can_be_allowed_explicitly_for_demo():
    CONFIG.risk.quant_allow_unverified = True
    ok, _ = usable(spec_for("KXBTCD-25AUG14-B"))
    assert ok


def test_a_unit_mismatch_is_refused_even_with_the_override():
    """The gold contract's strike may be per troy ounce (~$2,600) while the
    feed returns GLD share price (~$250). That is a ~10x error that prices
    every contract at ~0 or ~1 without raising anything, so no override
    should let it through."""
    CONFIG.risk.quant_allow_unverified = True
    spec = spec_for("KXGOLD-25AUG")

    assert not spec.units_match
    ok, why = usable(spec)
    assert not ok
    assert "units" in why


def test_crypto_specs_have_matching_units():
    for prefix in ("KXBTCD", "KXBTC", "KXETH", "KXSOL"):
        assert spec_for(f"{prefix}-25").units_match


# -- price history quality --------------------------------------------------


def test_duplicate_observations_in_one_pass_are_rejected():
    """Ten BTC markets in one scan must not append ten identical points.
    Duplicates drive realized vol toward zero, and vol is in the denominator
    of the probability, so understated vol means overconfident pricing."""
    history = PriceHistory()
    now = time.time()

    assert history.add(100.0, at=now) is True
    assert history.add(100.0, at=now) is False
    assert history.add(100.0, at=now - 1) is False, "out of order too"
    assert len(history) == 1


def test_non_positive_and_non_finite_prices_are_rejected():
    history = PriceHistory()
    for bad in (0.0, -1.0, float("nan"), float("inf"), None):
        assert history.add(bad) is False
    assert len(history) == 0


def test_outliers_are_filtered_once_there_is_a_baseline():
    history = PriceHistory()
    base = time.time()
    for i in range(10):
        history.add(100.0 + i * 0.01, at=base + i)

    assert history.add(1000.0, at=base + 20) is False, "10x spike"
    assert history.add(1.0, at=base + 21) is False, "100x crash"
    assert history.add(100.5, at=base + 22) is True, "plausible move accepted"


def test_an_empty_buffer_accepts_anything():
    """Nothing to compare against yet — rejecting here would mean never
    bootstrapping."""
    history = PriceHistory()
    assert history.add(50_000.0) is True


def test_realized_vol_is_per_second_and_spacing_aware():
    """The old estimate assumed observations were evenly spaced at exactly
    SCOUT_POLL_SECONDS. Two buffers covering the same price path over the
    same wall-clock span should give similar per-second vol even when the
    sampling interval differs."""
    import random

    rng = random.Random(11)
    span, sigma = 3600.0, 1e-4

    def build(step):
        history = PriceHistory(maxlen=10_000)
        price, t0 = 100.0, time.time() - span
        n = int(span / step)
        for i in range(n):
            price *= 1 + rng.gauss(0, sigma * (step ** 0.5))
            history.add(price, at=t0 + i * step)
        return history.realized_vol(span * 2)

    dense = build(10.0)
    sparse = build(60.0)

    assert dense is not None and sparse is not None
    # Same order of magnitude — the point is that spacing no longer changes
    # the answer by the ratio of the intervals (6x here).
    assert 0.3 < (dense / sparse) < 3.0, (dense, sparse)


def test_vol_needs_a_minimum_number_of_observations():
    history = PriceHistory()
    base = time.time()
    for i in range(3):
        history.add(100.0 + i, at=base + i)
    assert history.realized_vol(3600) is None


def test_span_seconds_reports_the_window_covered():
    history = PriceHistory()
    base = time.time()
    history.add(100.0, at=base)
    history.add(101.0, at=base + 600)
    assert history.span_seconds() == pytest.approx(600)


# -- client caching and backoff ---------------------------------------------


class FakeHTTP:
    def __init__(self, price=50_000.0, status=200):
        self.price = price
        self.status = status
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        return self

    @property
    def status_code(self):
        return self.status

    def json(self):
        return {"bitcoin": {"usd": self.price}}

    def close(self):
        pass


def test_one_fetch_per_symbol_per_pass():
    http = FakeHTTP()
    client = SpotPriceClient(http=http)
    client.begin_pass()

    for _ in range(10):
        client.get_quote("btc", "crypto")

    assert http.calls == 1, "ten candidates must not make ten API calls"
    assert len(client.get_history("btc")) == 1, "nor ten history points"


def test_a_new_pass_fetches_again():
    http = FakeHTTP()
    client = SpotPriceClient(http=http)

    client.begin_pass()
    client.get_quote("btc", "crypto")
    client.begin_pass()
    client.get_quote("btc", "crypto")

    assert http.calls == 2


def test_rate_limiting_triggers_bounded_backoff():
    http = FakeHTTP(status=429)
    client = SpotPriceClient(http=http)
    client.begin_pass()

    assert client.get_quote("btc", "crypto") is None
    calls_after_first = http.calls

    # Subsequent passes are blocked until the backoff expires, rather than
    # hammering a source that just told us to slow down.
    for _ in range(5):
        client.begin_pass()
        assert client.get_quote("btc", "crypto") is None
    assert http.calls == calls_after_first


def test_backoff_clears_after_a_success():
    http = FakeHTTP()
    client = SpotPriceClient(http=http)
    client._backoff["crypto"] = (0.0, 3)  # expired backoff, 3 past failures

    client.begin_pass()
    assert client.get_quote("btc", "crypto") is not None
    assert "crypto" not in client._backoff


def test_a_stale_cached_quote_is_not_returned():
    client = SpotPriceClient(http=FakeHTTP())
    client._quotes["btc"] = SpotQuote(
        symbol="btc", price=1.0, source="crypto",
        observed_at=time.time() - CONFIG.risk.max_spot_age_seconds - 10,
    )
    assert client.get_quote("btc", "crypto") is None


def test_implausible_prices_never_enter_state():
    client = SpotPriceClient(http=FakeHTTP(price=0))
    client.begin_pass()
    assert client.get_quote("btc", "crypto") is None
    assert client.get_history("btc") is None


# -- QuantMaker routing -----------------------------------------------------


def candidate(ticker="KXBTCD-25AUG14-B", strike=50_000.0, strike_type="greater"):
    from datetime import datetime, timedelta, timezone

    close = datetime.now(timezone.utc) + timedelta(hours=1)
    return Candidate(
        ticker=ticker, title=ticker, category="Crypto",
        yes_bid=48, yes_ask=52, volume=10_000,
        close_time=close.isoformat().replace("+00:00", "Z"),
        series_ticker=ticker.split("-")[0],
        strike_type=strike_type, floor_strike=strike,
    )


def test_quant_declines_unverified_families_by_default():
    quant = QuantMaker(SpotPriceClient(http=FakeHTTP()))
    quant.begin_pass()
    assert quant.can_handle(candidate()) is False


def test_quant_declines_a_family_with_no_spec():
    CONFIG.risk.quant_allow_unverified = True
    quant = QuantMaker(SpotPriceClient(http=FakeHTTP()))
    quant.begin_pass()
    assert quant.can_handle(candidate(ticker="KXNFLGAME-25")) is False


def test_quant_declines_gold_even_when_unverified_is_allowed():
    CONFIG.risk.quant_allow_unverified = True
    quant = QuantMaker(SpotPriceClient(http=FakeHTTP()))
    quant.begin_pass()
    assert quant.can_handle(candidate(ticker="KXGOLD-25AUG")) is False


def test_quant_handles_a_permitted_family():
    CONFIG.risk.quant_allow_unverified = True
    quant = QuantMaker(SpotPriceClient(http=FakeHTTP()))
    quant.begin_pass()
    assert quant.can_handle(candidate()) is True


def test_quant_declines_without_enough_price_history():
    """One observation is not a volatility estimate. Declining is the correct
    answer; inventing a number would produce false confidence."""
    CONFIG.risk.quant_allow_unverified = True
    quant = QuantMaker(SpotPriceClient(http=FakeHTTP()))
    quant.begin_pass()
    assert quant.propose(candidate()) is None


def test_quant_prices_once_history_is_deep_enough():
    import random

    CONFIG.risk.quant_allow_unverified = True
    client = SpotPriceClient(http=FakeHTTP())
    rng = random.Random(3)
    history = PriceHistory(maxlen=10_000)
    base = time.time() - 7200
    price = 50_000.0
    for i in range(200):
        price *= 1 + rng.gauss(0, 3e-4)
        history.add(price, at=base + i * 30)
    client.history["btc"] = history
    client._quotes["btc"] = SpotQuote(symbol="btc", price=price,
                                      observed_at=time.time(), source="crypto")

    quant = QuantMaker(client)
    result = quant.propose(candidate(strike=price * 1.001))

    assert result is not None
    assert 0.0 < result.probability_yes < 1.0
    assert result.observations_used >= CONFIG.risk.min_vol_observations
    assert result.volatility_per_second > 0


def test_quant_declines_a_stale_spot_quote():
    CONFIG.risk.quant_allow_unverified = True
    client = SpotPriceClient(http=FakeHTTP())
    client._quotes["btc"] = SpotQuote(
        symbol="btc", price=50_000.0, source="crypto",
        observed_at=time.time() - CONFIG.risk.max_spot_age_seconds - 60,
    )
    quant = QuantMaker(client)
    assert quant.propose(candidate()) is None


def test_declines_are_logged_once_per_family_not_once_per_market(caplog):
    quant = QuantMaker(SpotPriceClient(http=FakeHTTP()))
    quant.begin_pass()
    with caplog.at_level("INFO"):
        for i in range(50):
            quant.can_handle(candidate(ticker=f"KXBTCD-25AUG14-{i}"))

    assert caplog.text.count("Quant path declining") == 1
