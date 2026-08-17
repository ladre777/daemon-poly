"""
CF Benchmarks RTI feed, and the pricing changes that depend on it.

The quant path priced index-settled contracts off CoinGecko spot. Kalshi's
own `rules_secondary` says that is the wrong instrument, in as many words:

    "While checking a source like Google or Coinbase may help guide your
     decision, the price used to determine this market is based on CF
     Benchmarks' corresponding Real Time Index (RTI)."

Everything here is fixture-driven. No socket is opened and no authenticated
call is made; the frames are constructed to the documented shape of the
`cfbenchmarks_value` channel.
"""
from __future__ import annotations

import time

import pytest

from config import CONFIG
from core.contract_specs import spec_for
from core.rti_client import (
    CHANNEL,
    INDEX_FOR_SYMBOL,
    RTIFeed,
    index_for_symbol,
    parse_frame,
    subscribe_command,
)
from core.spot_price_client import SpotPriceClient
from workers.quant_maker import (
    SETTLEMENT_AVERAGING_SECONDS,
    _diffusion_horizon_seconds,
)


def frame(index_id="BRTI", value=63_500.25, windowed=None, ts=None):
    f = {"index_id": index_id, "avg_60s_data": {"value": value}}
    if windowed is not None:
        f["last_60s_windowed_average_15min"] = windowed
    if ts is not None:
        f["timestamp"] = ts
    return f


# --------------------------------------------------------------------------
# frame parsing
# --------------------------------------------------------------------------

def test_a_normal_frame_yields_the_trailing_average():
    q = parse_frame(frame(value=63_441.03))
    assert q.index_id == "BRTI"
    assert q.value == pytest.approx(63_441.03)
    assert q.windowed_15min is None, "absent outside the final minute"


def test_the_windowed_average_is_carried_when_present():
    """Only published in the final minute before :00/:15/:30/:45 — and for
    KXBTC15M that value IS the settlement figure."""
    q = parse_frame(frame(value=63_500.0, windowed=63_498.77))
    assert q.windowed_15min == pytest.approx(63_498.77)


def test_a_bare_numeric_avg_is_tolerated():
    """Documented as an object with `value`; a bare number is still usable
    and dropping it would lose a good quote on shape alone."""
    assert parse_frame({"index_id": "BRTI", "avg_60s_data": 63_000.0}).value == 63_000.0


@pytest.mark.parametrize("bad", [
    {},
    {"avg_60s_data": {"value": 1.0}},                       # no index_id
    {"index_id": "", "avg_60s_data": {"value": 1.0}},
    {"index_id": "BRTI"},                                   # no value
    {"index_id": "BRTI", "avg_60s_data": {"value": None}},
    {"index_id": "BRTI", "avg_60s_data": {"value": "abc"}},
    {"index_id": "BRTI", "avg_60s_data": {"value": 0}},
    {"index_id": "BRTI", "avg_60s_data": {"value": -5}},
    {"index_id": "BRTI", "avg_60s_data": {"value": float("nan")}},
    {"index_id": "BRTI", "avg_60s_data": {"value": float("inf")}},
    "not a dict",
])
def test_unusable_frames_are_refused_not_coerced(bad):
    """A NaN index value would make every edge NaN and fail every threshold
    comparison silently — the failure mode core/validation exists to stop."""
    assert parse_frame(bad) is None


def test_a_nonsense_windowed_value_does_not_poison_a_good_frame():
    q = parse_frame(frame(value=63_000.0, windowed=-1.0))
    assert q.value == pytest.approx(63_000.0)
    assert q.windowed_15min is None


# --------------------------------------------------------------------------
# index mapping — the per-asset trap
# --------------------------------------------------------------------------

def test_each_asset_maps_to_its_own_index():
    assert index_for_symbol("btc") == "BRTI"
    assert index_for_symbol("eth") == "ETHUSD_RTI"
    assert index_for_symbol("BTC") == "BRTI", "case-insensitive"


def test_an_unmapped_asset_gets_nothing_rather_than_bitcoins_index():
    """SOL has no confirmed index. Defaulting it onto BRTI would price
    solana contracts off the bitcoin index and look entirely normal."""
    assert index_for_symbol("sol") is None
    assert index_for_symbol("") is None
    assert "sol" not in INDEX_FOR_SYMBOL


def test_the_subscribe_frame_uses_index_ids_not_market_tickers():
    """core/kalshi_ws.subscribe() sends market_tickers, which this channel
    rejects — which is why it could not be reused unchanged."""
    cmd = subscribe_command(["BRTI", "ETHUSD_RTI"])
    assert cmd["cmd"] == "subscribe"
    assert cmd["params"]["channels"] == [CHANNEL]
    assert cmd["params"]["index_ids"] == ["BRTI", "ETHUSD_RTI"]
    assert "market_tickers" not in cmd["params"]


# --------------------------------------------------------------------------
# the feed cache, and its refusal to guess
# --------------------------------------------------------------------------

def test_the_feed_serves_the_latest_value_per_index():
    feed = RTIFeed()
    feed.apply_frame(frame("BRTI", 63_000.0))
    feed.apply_frame(frame("ETHUSD_RTI", 2_600.0))
    feed.apply_frame(frame("BRTI", 63_100.0))

    assert feed.value_for_symbol("btc") == pytest.approx(63_100.0)
    assert feed.value_for_symbol("eth") == pytest.approx(2_600.0)
    assert feed.frames_applied == 3


def test_a_cold_feed_returns_nothing():
    assert RTIFeed().value_for_symbol("btc") is None


def test_an_unmapped_symbol_returns_nothing_even_with_data():
    feed = RTIFeed()
    feed.apply_frame(frame("BRTI", 63_000.0))
    assert feed.value_for_symbol("sol") is None, "must not fall through to BRTI"


def test_a_stale_value_is_refused():
    feed = RTIFeed()
    feed.apply_frame(frame("BRTI", 63_000.0,
                           ts=time.time() - CONFIG.risk.max_spot_age_seconds - 60))
    assert feed.value_for_symbol("btc") is None
    assert feed.is_healthy is False


def test_rejected_frames_are_counted_not_silently_dropped():
    feed = RTIFeed()
    feed.apply_frame({"garbage": True})
    assert feed.frames_rejected == 1
    assert feed.frames_applied == 0


def test_health_reflects_whether_anything_fresh_exists():
    feed = RTIFeed()
    assert feed.is_healthy is False
    feed.apply_frame(frame("BRTI", 63_000.0))
    assert feed.is_healthy is True


# --------------------------------------------------------------------------
# the wiring: never fall back to spot
# --------------------------------------------------------------------------

def test_the_client_returns_no_quote_without_an_rti_feed():
    """The load-bearing refusal. Falling back to CoinGecko here would
    silently restore the exact bug this change removes."""
    client = SpotPriceClient(rti_feed=None)
    client.begin_pass()
    assert client.get_quote("btc", "kalshi_rti") is None


def test_the_client_prices_off_the_index_when_the_feed_is_live():
    feed = RTIFeed()
    feed.apply_frame(frame("BRTI", 63_441.03))
    client = SpotPriceClient(rti_feed=feed)
    client.begin_pass()

    quote = client.get_quote("btc", "kalshi_rti")
    assert quote is not None
    assert quote.price == pytest.approx(63_441.03)
    assert quote.source == "kalshi_rti"


def test_a_cold_feed_makes_the_quant_path_decline_rather_than_guess():
    feed = RTIFeed()          # nothing received
    client = SpotPriceClient(rti_feed=feed)
    client.begin_pass()
    assert client.get_quote("btc", "kalshi_rti") is None


def test_no_http_call_is_made_for_an_rti_symbol():
    """The index is pushed, not polled. A stray HTTP call here would mean
    the dispatch fell through to the spot path."""
    class ExplodingHTTP:
        def get(self, *a, **kw):
            raise AssertionError("kalshi_rti must not make an HTTP request")

        def close(self):
            pass

    feed = RTIFeed()
    feed.apply_frame(frame("BRTI", 63_000.0))
    client = SpotPriceClient(http=ExplodingHTTP(), rti_feed=feed)
    client.begin_pass()
    assert client.get_quote("btc", "kalshi_rti").price == pytest.approx(63_000.0)


# --------------------------------------------------------------------------
# the time-average correction
# --------------------------------------------------------------------------

def test_an_index_settled_contract_gets_a_shorter_diffusion_horizon():
    """Settlement is the MEAN of the final 60s, not the endpoint.

    A time-average is less variable than the point it straddles, so treating
    it as a point sample overstates the spread of outcomes, pushes every
    probability toward 0.5, and manufactures edge against a correctly priced
    market.
    """
    spec = spec_for("KXBTC15M-26AUG170100-00")
    horizon = _diffusion_horizon_seconds(spec, 900.0)
    assert horizon == pytest.approx(900.0 - 2 * SETTLEMENT_AVERAGING_SECONDS / 3)
    assert horizon < 900.0


def test_a_point_in_time_contract_is_unchanged():
    spec = spec_for("KXGOLD-25AUG")
    assert spec.observation != "rti_60s_average"
    assert _diffusion_horizon_seconds(spec, 900.0) == 900.0


def test_the_horizon_never_goes_non_positive():
    """Deep inside the averaging window the correction would go negative,
    which would collapse the probability into a step function."""
    spec = spec_for("KXBTC15M-26AUG170100-00")
    assert _diffusion_horizon_seconds(spec, 10.0) >= 1.0
    assert _diffusion_horizon_seconds(spec, 0.5) >= 1.0


def test_a_missing_spec_is_handled():
    assert _diffusion_horizon_seconds(None, 900.0) == 900.0


def test_the_correction_lowers_variance_by_the_documented_amount():
    """var(mean over trailing w) = sigma^2 (T - 2w/3), so the ratio of
    standard deviations is sqrt((T - 2w/3)/T)."""
    import math

    spec = spec_for("KXBTC15M-26AUG170100-00")
    T = 900.0
    ratio = math.sqrt(_diffusion_horizon_seconds(spec, T) / T)
    assert ratio == pytest.approx(math.sqrt((900 - 40) / 900), rel=1e-9)
    assert 0.97 < ratio < 0.98, "a real but modest ~2% narrowing at 15 minutes"
