"""
The runner that makes the RTI feed non-inert.

`RTIFeed` is a cache; without something pushing frames into it every crypto
family returns None and the quant path declines. These tests cover the wiring
that stops that being the permanent state, and — more importantly — that
every way the wiring can fail still ends in "decline", never in "price it off
something else".
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from config import CONFIG
from core.rti_client import CHANNEL, RTIFeed, subscribe_command
from core.rti_runner import RTIFeedRunner


def frame(index_id="BRTI", value=64000.0, windowed=None, ts=None):
    payload = {
        "index_id": index_id,
        "avg_60s_data": {"value": value},
        "timestamp": ts if ts is not None else time.time(),
    }
    if windowed is not None:
        payload["last_60s_windowed_average_15min"] = windowed
    return {"type": CHANNEL, "msg": payload}


class FakeWS:
    """A Kalshi WebSocket that replays a scripted list of frames.

    ``fail_on_connect`` and ``raise_after`` model the two failures that matter:
    a socket that will not open, and one that drops mid-stream.
    """

    def __init__(self, frames, fail_on_connect=None, raise_after=None):
        self.frames = frames
        self.fail_on_connect = fail_on_connect
        self.raise_after = raise_after
        self.sent: list[dict] = []
        self.connected = False
        self.closed = False

    async def connect(self):
        if self.fail_on_connect:
            raise self.fail_on_connect
        self.connected = True

    async def send(self, cmd):
        self.sent.append(cmd)

    def messages(self):
        outer = self

        class _Iter:
            def __init__(self):
                self.i = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if outer.raise_after is not None and self.i == outer.raise_after:
                    raise ConnectionResetError("socket dropped")
                if self.i >= len(outer.frames):
                    raise StopAsyncIteration
                item = outer.frames[self.i]
                self.i += 1
                return item if isinstance(item, str) else json.dumps(item)

        return _Iter()

    async def close(self):
        self.closed = True


def run(runner):
    """Drive one supervised session synchronously, without the thread."""
    asyncio.run(runner._session())


# -- the subscribe frame ---------------------------------------------------


def test_subscribes_by_index_id_not_market_ticker():
    """The channel rejects market_tickers, which is why KalshiWebSocket's own
    subscribe() could not be reused."""
    ws = FakeWS([])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert len(ws.sent) == 1
    params = ws.sent[0]["params"]
    assert params["channels"] == [CHANNEL]
    assert "market_tickers" not in params
    assert set(params["index_ids"]) == {"BRTI", "ETHUSD_RTI"}


def test_subscribe_command_shape_matches_what_the_runner_sends():
    ws = FakeWS([])
    runner = RTIFeedRunner(ws_factory=lambda: ws, index_ids=["BRTI"])

    run(runner)

    expected = subscribe_command(["BRTI"], cmd_id=1)
    assert ws.sent[0] == expected


# -- frames reach the feed -------------------------------------------------


def test_a_relayed_value_becomes_a_priceable_quote():
    ws = FakeWS([frame(value=64123.5)])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert runner.feed.value_for_symbol("btc") == pytest.approx(64123.5)
    assert runner.is_ready


def test_both_indices_are_tracked_independently():
    ws = FakeWS([frame("BRTI", 64000.0), frame("ETHUSD_RTI", 3100.0)])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert runner.feed.value_for_symbol("btc") == pytest.approx(64000.0)
    assert runner.feed.value_for_symbol("eth") == pytest.approx(3100.0)


def test_the_quarter_hour_windowed_value_survives_the_envelope():
    """It is the settling number for KXBTC15M; losing it in transit would
    make after-the-fact settlement checks impossible."""
    ws = FakeWS([frame(windowed=64100.0)])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert runner.feed.latest("BRTI").windowed_15min == pytest.approx(64100.0)


def test_a_bare_frame_without_the_envelope_is_still_accepted():
    """Tolerated on purpose: a shape change on Kalshi's side should not
    silently drop every quote."""
    ws = FakeWS([frame()["msg"]])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert runner.feed.value_for_symbol("btc") is not None


# -- failure is always "decline", never "substitute" -----------------------


def test_a_refused_subscription_is_recorded_loudly_and_prices_nothing():
    """The likeliest production failure: an account not entitled to the
    channel. The funnel would otherwise just show crypto never proposing."""
    ws = FakeWS([{"type": "error", "msg": {"code": 6, "msg": "not authorized"}}])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert not runner.is_ready
    assert "not authorized" in runner.last_error
    assert runner.feed.value_for_symbol("btc") is None
    assert "NOT live" in runner.status()


def test_a_malformed_frame_is_rejected_rather_than_coerced():
    ws = FakeWS([{"type": CHANNEL, "msg": {"index_id": "BRTI",
                                           "avg_60s_data": {"value": "nonsense"}}}])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert runner.feed.frames_rejected == 1
    assert runner.feed.value_for_symbol("btc") is None


def test_unparseable_json_does_not_kill_the_session():
    ws = FakeWS(["{not json", frame(value=64000.0)])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert runner.feed.value_for_symbol("btc") == pytest.approx(64000.0)


def test_a_stale_value_stops_being_priceable():
    """A socket that connected and then went quiet is indistinguishable from
    a healthy one by connection state alone — only age tells them apart."""
    ws = FakeWS([frame(ts=time.time() - CONFIG.risk.max_spot_age_seconds - 30)])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert runner.feed.latest("BRTI") is not None, "the value was received"
    assert runner.feed.value_for_symbol("btc") is None, "but it is too old to use"
    assert not runner.is_ready


def test_the_socket_is_closed_even_when_the_stream_raises():
    ws = FakeWS([frame(), frame()], raise_after=1)
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    with pytest.raises(ConnectionResetError):
        run(runner)

    assert ws.closed, "a dropped session must not leak its socket"


# -- supervision -----------------------------------------------------------


def test_a_dropped_session_reconnects():
    sockets: list[FakeWS] = []

    def factory():
        # First socket drops immediately; the second delivers.
        ws = (FakeWS([], fail_on_connect=ConnectionResetError("nope"))
              if not sockets else FakeWS([frame()]))
        sockets.append(ws)
        return ws

    runner = RTIFeedRunner(ws_factory=factory)

    async def drive():
        task = asyncio.create_task(runner._supervise())
        # Long enough to cover the 1s first backoff.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if runner.is_ready:
                break
        runner._stopping.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert len(sockets) >= 2, "the supervisor must try again after a drop"
    assert runner.is_ready


def test_supervisor_stops_when_asked():
    runner = RTIFeedRunner(ws_factory=lambda: FakeWS([frame()]))
    runner._stopping.set()

    asyncio.run(runner._supervise())

    assert not runner.is_ready, "nothing ran"


def test_start_is_idempotent_and_stop_is_safe_without_start():
    runner = RTIFeedRunner(ws_factory=lambda: FakeWS([]))
    runner.stop()                          # never started — must not raise

    runner.start()
    first = runner._thread
    runner.start()
    assert runner._thread is first, "a second start must not spawn a second socket"
    runner.stop()


def test_the_thread_runs_and_feeds_the_shared_cache():
    """End to end through the real thread, since the scan loop reads the feed
    from a different thread than the one writing it."""
    runner = RTIFeedRunner(ws_factory=lambda: FakeWS([frame(value=63000.0)]))
    runner.start()
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline and not runner.is_ready:
            time.sleep(0.02)
        assert runner.feed.value_for_symbol("btc") == pytest.approx(63000.0)
    finally:
        runner.stop()


# -- what the operator is told ---------------------------------------------


def test_status_says_crypto_is_unpriced_and_that_this_is_safe():
    runner = RTIFeedRunner(ws_factory=lambda: FakeWS([]))

    status = runner.status()

    assert "NOT live" in status
    assert "suppresses trades" in status, (
        "an operator reading this at 3am must know it is not a money risk"
    )


def test_status_reports_health_once_frames_arrive():
    ws = FakeWS([frame()])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert "live" in runner.status()
    assert "NOT live" not in runner.status()


def test_a_recovered_feed_clears_the_recorded_error():
    ws = FakeWS([{"type": "error", "msg": "transient"}, frame()])
    runner = RTIFeedRunner(ws_factory=lambda: ws)

    run(runner)

    assert runner.last_error is None
    assert runner.is_ready


# -- integration with the pricing path -------------------------------------


def test_spot_client_prices_rti_families_off_the_runner_feed():
    from core.spot_price_client import SpotPriceClient

    feed = RTIFeed()
    feed.apply_frame(frame()["msg"])
    client = SpotPriceClient(rti_feed=feed)

    quote = client.get_quote("btc", source="kalshi_rti")

    assert quote is not None
    assert quote.price == pytest.approx(64000.0)


def test_spot_client_declines_rather_than_falling_back_when_no_feed_is_wired():
    """The whole point. Without this the pricing change is a no-op that
    quietly keeps using CoinGecko."""
    from core.spot_price_client import SpotPriceClient

    client = SpotPriceClient(rti_feed=None)

    assert client.get_quote("btc", source="kalshi_rti") is None
