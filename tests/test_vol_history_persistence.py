"""
The volatility clock must survive a restart.

The quant path refuses to price until it has MIN_VOL_SPAN_SECONDS (600) of
observations. That is right — volatility is the denominator of every
probability it produces, and an estimate off a handful of ticks is noise.

But the buffer lived only in memory, and production redeploys often. Across a
whole session of live logs:

    Only 0s of price history for eth (need 600s)     — fresh container
    Only 112s of price history for eth (need 600s)
    Only 424s of price history for eth (need 600s)
    Pass funnel: ... quant 68 (no proposal 68, below edge 0) ...

The 15-minute and hourly crypto families never priced once, in any run,
because no container survived long enough. The gate was never wrong — the
data was being thrown away.

So the observations are kept. The same 600 seconds of real ticks are still
required; nothing here lowers the bar.
"""
from __future__ import annotations

import time

import pytest

from config import CONFIG
from core.spot_price_client import PriceHistory, SpotPriceClient
from memory.price_store import PriceStore


@pytest.fixture
def store(db_path):
    return PriceStore(db_path)


def ticks(n=130, spacing=5.0, start=None, base=64000.0):
    """A realistic sampled stream: n points, `spacing` apart."""
    start = start if start is not None else time.time() - n * spacing
    return [(start + i * spacing, base * (1 + 0.0004 * ((i * 7) % 11 - 5)))
            for i in range(n)]


# -- the store itself ------------------------------------------------------


def test_points_survive_a_round_trip(store):
    points = ticks(10)
    store.save("btc", points)

    loaded = store.load("btc")

    assert len(loaded) == 10
    assert loaded[0][0] == pytest.approx(points[0][0])


def test_saving_is_idempotent(store):
    """Called once a pass with the WHOLE rolling buffer, not a delta — so
    overlapping saves must not duplicate."""
    points = ticks(10)
    store.save("btc", points)
    store.save("btc", points)

    assert len(store.load("btc")) == 10


def test_symbols_do_not_mix(store):
    store.save("btc", ticks(5))
    store.save("eth", ticks(3, base=3100.0))

    assert len(store.load("btc")) == 5
    assert len(store.load("eth")) == 3


def test_points_are_returned_oldest_first(store):
    store.save("btc", ticks(20))

    loaded = store.load("btc")

    assert loaded == sorted(loaded)


# -- staleness -------------------------------------------------------------


def test_points_older_than_the_retention_window_are_not_saved(store):
    old = [(time.time() - 7200, 64000.0)]

    assert store.save("btc", old) == 0


def test_points_older_than_the_window_are_not_loaded(store):
    """A container down for an hour must not come back and compute a
    600-second span across a 60-minute hole — `realized_vol` normalises each
    return by its own elapsed time, so one enormous gap drags the estimate
    toward zero."""
    store.save("btc", ticks(5), max_age_seconds=10_000)

    assert store.load("btc", max_age_seconds=1.0) == []


def test_pruning_bounds_the_table(store):
    store.save("btc", ticks(5), max_age_seconds=10_000)

    removed = store.prune(max_age_seconds=1.0)

    assert removed == 5
    assert store.load("btc", max_age_seconds=10_000) == []


def test_junk_is_refused(store):
    assert store.save("", ticks(3)) == 0
    assert store.save("btc", []) == 0
    assert store.load("") == []


# -- the client round trip -------------------------------------------------


def test_a_restart_no_longer_resets_the_clock(store):
    """The property the whole change exists for."""
    before = SpotPriceClient(price_store=store)
    for at, price in ticks(130):
        before.record_tick("btc", price, at)
    assert before.persist_history() > 0

    after = SpotPriceClient(price_store=store)          # "new container"
    restored = after.restore_history(["btc"])

    assert restored["btc"] > 100
    assert (after.history["btc"].span_seconds()
            >= CONFIG.risk.min_vol_span_seconds)


def test_the_restored_history_yields_a_usable_estimate(store):
    before = SpotPriceClient(price_store=store)
    for at, price in ticks(200):
        before.record_tick("btc", price, at)
    before.persist_history()

    after = SpotPriceClient(price_store=store)
    after.restore_history(["btc"])

    assert after.history["btc"].realized_vol(lookback_seconds=3600) is not None


def test_restoring_still_honours_the_outlier_filter(store):
    """Restoring is additive, not a bypass. A corrupt stored row must clear
    the same checks a live tick does."""
    now = time.time()
    store.save("btc", [(now - 60 + i, 64000.0) for i in range(10)]
               + [(now - 40, 5_000_000.0)])

    client = SpotPriceClient(price_store=store)
    client.restore_history(["btc"])

    prices = [p for _, p in client.history["btc"].points]
    assert 5_000_000.0 not in prices


def test_no_store_configured_is_not_an_error():
    """PERSIST_VOL_HISTORY=false must degrade to the old in-memory behaviour,
    not crash."""
    client = SpotPriceClient(price_store=None)

    assert client.restore_history() == {}
    assert client.persist_history() == 0


def test_nothing_stored_restores_nothing(store):
    client = SpotPriceClient(price_store=store)

    assert client.restore_history(["btc"]) == {}


def test_persisting_an_empty_buffer_writes_nothing(store):
    client = SpotPriceClient(price_store=store)

    assert client.persist_history() == 0


# -- the gate is unchanged -------------------------------------------------


def test_the_span_requirement_is_not_lowered():
    """This change keeps observations; it does not relax what they must add
    up to. If MIN_VOL_SPAN_SECONDS ever moves, that is a separate decision."""
    assert CONFIG.risk.min_vol_span_seconds == 600.0


def test_a_short_restored_history_still_declines(store):
    """Restoring 60 seconds of ticks does not make a market priceable."""
    client = SpotPriceClient(price_store=store)
    for at, price in ticks(12):
        client.record_tick("btc", price, at)
    client.persist_history()

    after = SpotPriceClient(price_store=store)
    after.restore_history(["btc"])

    assert (after.history["btc"].span_seconds()
            < CONFIG.risk.min_vol_span_seconds)


def test_restored_points_are_not_double_counted(store):
    """Restore then persist then restore again must not inflate the buffer,
    or the span would grow without any new observation."""
    client = SpotPriceClient(price_store=store)
    for at, price in ticks(50):
        client.record_tick("btc", price, at)
    client.persist_history()

    second = SpotPriceClient(price_store=store)
    second.restore_history(["btc"])
    second.persist_history()
    third = SpotPriceClient(price_store=store)
    third.restore_history(["btc"])

    assert len(third.history["btc"]) == len(second.history["btc"])


def test_history_is_a_plain_price_history_after_restore(store):
    store.save("btc", ticks(5))
    client = SpotPriceClient(price_store=store)
    client.restore_history(["btc"])

    assert isinstance(client.history["btc"], PriceHistory)
