"""
Building the volatility estimate from the index's own tick stream.

Production, with the CF Benchmarks feed confirmed live and delivering roughly
two observations a second::

    Only 0s of price history for eth (need 600s) — declining rather than
    pricing off noise
    Pass funnel: ... quant 18 (no proposal 18, below edge 0) ...

Every one of the eighteen crypto candidates declined for want of history,
while the data needed to build that history streamed past unrecorded. The
estimate was fed only by ``get_quote``, once per symbol per scan pass — at a
five-minute poll that is an hour of uninterrupted uptime to reach a
600-second span, and the buffer is in memory, so every redeploy set it back
to zero.

Two things have to be true for this to work, and the second is the one that
is easy to miss: ticks must reach the history, *and* they must be spaced out
before they get there.
"""
from __future__ import annotations

import time

import pytest

from config import CONFIG
from core.rti_runner import SYMBOL_FOR_INDEX, RTIFeedRunner
from core.spot_price_client import SpotPriceClient

from tests.test_rti_runner import FakeWS, frame, run


@pytest.fixture
def spot():
    CONFIG.risk.rti_tick_sample_seconds = 5.0
    return SpotPriceClient()


# -- the sampling interval -------------------------------------------------


def test_ticks_closer_together_than_the_interval_are_dropped(spot):
    """The buffer holds 500 points. Storing every frame at two per second
    gives it about four minutes of span — permanently short of the 600
    seconds the estimator needs, no matter how long the process runs."""
    now = time.time()

    stored = [spot.record_tick("btc", 64000.0 + i, now + i * 0.5)
              for i in range(20)]

    assert sum(stored) == 2, "20 frames over 10s at a 5s interval is 2 points"


def test_ticks_at_the_interval_are_all_kept(spot):
    now = time.time()

    stored = [spot.record_tick("btc", 64000.0 + i, now + i * 5.0)
              for i in range(10)]

    assert all(stored)
    assert len(spot.history["btc"]) == 10


def test_the_sampled_buffer_spans_long_enough_to_satisfy_the_estimator(spot):
    """The point of the interval, stated as the property it exists for."""
    now = time.time()
    # 121 points at 5s spacing = 600s of span; 120 would be 595 and just miss.
    for i in range(130):
        spot.record_tick("btc", 64000.0 + (i % 7), now + i * 5.0)

    assert spot.history["btc"].span_seconds() >= CONFIG.risk.min_vol_span_seconds


def test_a_zero_interval_stores_every_tick(spot):
    CONFIG.risk.rti_tick_sample_seconds = 0.0
    now = time.time()

    stored = [spot.record_tick("btc", 64000.0 + i, now + i * 0.1)
              for i in range(10)]

    assert all(stored)


def test_symbols_are_sampled_independently(spot):
    now = time.time()

    assert spot.record_tick("btc", 64000.0, now)
    assert spot.record_tick("eth", 3100.0, now), (
        "eth must not be throttled by a btc tick a moment earlier"
    )


# -- the recorder itself ---------------------------------------------------


def test_a_rejected_tick_does_not_advance_the_sampling_clock(spot):
    """Otherwise one bad print suppresses the next full interval of good
    ones, and the buffer grows more slowly than configured."""
    now = time.time()
    spot.record_tick("btc", 64000.0, now)
    for i in range(5):
        spot.record_tick("btc", 64000.0 + i, now + 6.0 + i * 5.0)

    # A wild print is refused by the outlier filter...
    assert not spot.record_tick("btc", 1_000_000.0, now + 40.0)
    # ...and the next good one at a valid spacing still lands.
    assert spot.record_tick("btc", 64010.0, now + 45.0)


def test_a_tick_with_no_symbol_is_refused(spot):
    assert not spot.record_tick("", 64000.0)
    assert not spot.record_tick(None, 64000.0)


def test_a_nonsense_price_is_refused(spot):
    assert not spot.record_tick("btc", 0.0)
    assert not spot.record_tick("btc", -1.0)
    assert not spot.record_tick("btc", float("nan"))


# -- wiring: frames reach the history --------------------------------------


def test_index_ids_map_back_to_their_symbols():
    assert SYMBOL_FOR_INDEX["BRTI"] == "btc"
    assert SYMBOL_FOR_INDEX["ETHUSD_RTI"] == "eth"


def test_a_relayed_frame_lands_in_the_volatility_history(spot):
    now = time.time()
    frames = [frame("BRTI", 64000.0 + i, ts=now + i * 6.0) for i in range(4)]
    runner = RTIFeedRunner(ws_factory=lambda: FakeWS(frames),
                           on_quote=spot.record_tick)

    run(runner)

    assert len(spot.history["btc"]) == 4
    assert runner.ticks_recorded == 4


def test_both_indices_build_their_own_history(spot):
    now = time.time()
    frames = [frame("BRTI", 64000.0, ts=now),
              frame("ETHUSD_RTI", 3100.0, ts=now)]
    runner = RTIFeedRunner(ws_factory=lambda: FakeWS(frames),
                           on_quote=spot.record_tick)

    run(runner)

    assert len(spot.history["btc"]) == 1
    assert len(spot.history["eth"]) == 1


def test_the_feed_survives_a_recorder_that_raises():
    """A failure here is a lost data point, not a lost feed. The socket must
    keep running whatever the consumer does with a tick."""
    def explode(*a, **kw):
        raise RuntimeError("history is broken")

    runner = RTIFeedRunner(ws_factory=lambda: FakeWS([frame(), frame()]),
                           on_quote=explode)

    run(runner)

    assert runner.is_ready, "the index value is still cached and priceable"
    assert runner.ticks_recorded == 0


def test_an_unmapped_index_is_not_recorded_against_a_guess(spot):
    runner = RTIFeedRunner(ws_factory=lambda: FakeWS([frame("SOMETHING_ELSE")]),
                           on_quote=spot.record_tick)

    run(runner)

    assert spot.history == {}


def test_no_recorder_wired_is_not_an_error(spot):
    """The feed's own job — caching the settling value — does not depend on
    anything consuming the tick stream."""
    runner = RTIFeedRunner(ws_factory=lambda: FakeWS([frame()]))

    run(runner)

    assert runner.is_ready
    assert runner.ticks_recorded == 0


# -- end to end: enough ticks make the market priceable --------------------


def test_a_streamed_history_reaches_a_usable_volatility_estimate(spot):
    """The property the whole change exists for: after ten minutes of feed at
    the sampling interval, the estimator has what it needs."""
    now = time.time() - 1200
    for i in range(200):
        # A gently varying series — constant prices give zero variance and no
        # usable estimate, which is correct but not what is under test.
        spot.record_tick("btc", 64000.0 * (1 + 0.0004 * ((i * 7) % 11 - 5)),
                         now + i * 5.0)

    history = spot.history["btc"]

    assert history.span_seconds() >= CONFIG.risk.min_vol_span_seconds
    assert len(history) >= CONFIG.risk.min_vol_observations
    assert history.realized_vol(lookback_seconds=3600) is not None


def test_history_is_read_safely_while_the_feed_writes(spot):
    """PriceHistory is written from the feed thread and read from the scan
    loop. Before the lock, `realized_vol` could walk the deque while it
    resized."""
    import threading

    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        i = 0
        base = time.time()
        while not stop.is_set() and i < 4000:
            spot.record_tick("btc", 64000.0 + (i % 5), base + i * 5.0)
            i += 1

    def reader():
        try:
            for _ in range(2000):
                spot.history.get("btc") and spot.history["btc"].realized_vol()
        except BaseException as e:            # noqa: BLE001 - that is the test
            errors.append(e)

    spot.record_tick("btc", 64000.0, time.time())
    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    stop.set()

    assert not errors, f"concurrent access raised: {errors[0]!r}"
