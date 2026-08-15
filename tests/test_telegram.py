"""
Telegram alerting.

The property that matters most here is negative: alerting must never be able
to affect trading. Most of these tests are about what does NOT happen when
Telegram misbehaves.
"""
from __future__ import annotations

import time

import httpx
import pytest

from config import CONFIG
from core.order_state import OrderIntent, OrderState
from core.telegram_client import MAX_MESSAGE_CHARS, TelegramClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {"ok": True}
        self.text = text or "{}"

    def json(self):
        return self._payload


class FakeHTTP:
    """Records posts. `behaviour` may be a status code, or an exception."""

    def __init__(self, behaviour=200):
        self.behaviour = behaviour
        self.posts = []

    def post(self, url, json=None, **kw):
        self.posts.append({"url": url, "json": json})
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        if isinstance(self.behaviour, int):
            if self.behaviour == 429:
                return FakeResponse(429, {"parameters": {"retry_after": 0.01}})
            return FakeResponse(self.behaviour, text="error body")
        return FakeResponse()

    def close(self):
        pass


@pytest.fixture
def http():
    return FakeHTTP()


@pytest.fixture
def client(http):
    CONFIG.telegram.min_interval_seconds = 0.0
    CONFIG.telegram.throttle_seconds = 0.0
    c = TelegramClient(bot_token="tok", chat_id="42", http=http)
    yield c
    c.close(flush=False)


def drain(client, timeout=2.0):
    """Wait for the worker thread to finish delivering."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client._queue.empty() and (client.sent or client.failed):
            time.sleep(0.05)
            return
        time.sleep(0.01)


# -- enable/disable ---------------------------------------------------------


def test_disabled_without_credentials():
    c = TelegramClient(bot_token="", chat_id="", http=FakeHTTP())
    assert c.enabled is False
    assert c.send("anything") is False
    assert c._worker is None, "no thread should start when disabled"


def test_disabled_with_only_a_token():
    assert TelegramClient(bot_token="tok", chat_id="", http=FakeHTTP()).enabled is False


def test_every_method_is_safe_when_disabled():
    """A checkout with no Telegram setup must run normally and say nothing."""
    c = TelegramClient(bot_token="", chat_id="", http=FakeHTTP())
    c.notify_startup(env="demo", dry_run=True, strategy="taker", balance_usd=0,
                     positions=0, exposure_usd=0, bankroll_usd=0)
    c.notify_kill_switch("reason", -5.0, 1000.0)
    c.notify_systemic_error("auth", "detail")
    c.notify_stalled("scan", 900)
    c.notify_shutdown("SIGTERM")
    assert c.flush() is True
    c.close()


# -- delivery ---------------------------------------------------------------


def test_a_message_reaches_the_api(client, http):
    client.send("hello")
    drain(client)

    assert client.sent == 1
    assert http.posts[0]["json"]["chat_id"] == "42"
    assert http.posts[0]["json"]["text"] == "hello"
    assert "/bottok/sendMessage" in http.posts[0]["url"]


def test_long_messages_are_truncated(client, http):
    client.send("x" * 10_000)
    drain(client)

    text = http.posts[0]["json"]["text"]
    assert len(text) < 10_000
    assert "truncated from 10000" in text
    assert len(text) <= MAX_MESSAGE_CHARS + 100


# -- failures never propagate ----------------------------------------------


@pytest.mark.parametrize(
    "behaviour",
    [
        401,                                        # bad token
        400,                                        # chat not found
        403,                                        # user never messaged the bot
        500,                                        # Telegram broken
        429,                                        # rate limited
        httpx.ConnectError("no route to host"),
        httpx.ReadTimeout("timed out"),
    ],
)
def test_api_failures_are_swallowed(behaviour):
    """Every one of these is a setup or outage problem the operator must fix.
    None may raise into the trading loop."""
    http = FakeHTTP(behaviour)
    CONFIG.telegram.min_interval_seconds = 0.0
    c = TelegramClient(bot_token="tok", chat_id="42", http=http)
    try:
        assert c.send("hello") is True, "queueing succeeds even if delivery won't"
        drain(c)
        assert c.sent == 0
        assert c.failed >= 1
    finally:
        c.close(flush=False)


def test_a_raising_http_client_does_not_kill_the_worker():
    """If the worker thread died, every later alert would queue forever and
    the operator would go blind with no signal that it had happened."""
    http = FakeHTTP(RuntimeError("something unexpected"))
    CONFIG.telegram.min_interval_seconds = 0.0
    c = TelegramClient(bot_token="tok", chat_id="42", http=http)
    try:
        c.send("first")
        drain(c)
        http.behaviour = 200
        c.send("second")
        drain(c)
        assert c.sent == 1, "worker survived the exception and delivered the next one"
    finally:
        c.close(flush=False)


def test_send_never_raises_even_with_a_broken_queue(client, monkeypatch):
    def explode(*a, **kw):
        raise RuntimeError("queue is broken")

    monkeypatch.setattr(client._queue, "put_nowait", explode)
    assert client.send("hello") is False  # logged, not raised


# -- non-blocking -----------------------------------------------------------


def test_send_returns_immediately_even_when_delivery_is_slow():
    """The loop runs on a 30s cycle; a synchronous send with an 8s timeout
    would eat a quarter of that budget, and all of it if the API hangs."""
    class SlowHTTP(FakeHTTP):
        def post(self, url, json=None, **kw):
            time.sleep(0.5)
            return super().post(url, json=json, **kw)

    CONFIG.telegram.min_interval_seconds = 0.0
    c = TelegramClient(bot_token="tok", chat_id="42", http=SlowHTTP())
    try:
        started = time.time()
        for _ in range(5):
            c.send("hello")
        elapsed = time.time() - started
        assert elapsed < 0.1, f"send() blocked for {elapsed:.2f}s"
    finally:
        c.close(flush=False)


def test_a_full_queue_drops_rather_than_blocking(http):
    CONFIG.telegram.queue_maxsize = 5
    CONFIG.telegram.min_interval_seconds = 0.0
    # No worker: the queue fills and stays full.
    c = TelegramClient(bot_token="tok", chat_id="42", http=http, start_worker=False)

    started = time.time()
    for i in range(50):
        c.send(f"msg {i}")
    elapsed = time.time() - started

    assert elapsed < 1.0, "a full queue must never block the caller"
    assert c.dropped > 0
    assert c._queue.qsize() <= 5, "bounded — no unbounded buffering in a long-lived process"


def test_the_newest_message_survives_a_full_queue(http):
    """During an incident the most recent state is the useful one."""
    CONFIG.telegram.queue_maxsize = 2
    c = TelegramClient(bot_token="tok", chat_id="42", http=http, start_worker=False)

    for i in range(6):
        c.send(f"msg {i}")

    remaining = [c._queue.get_nowait().text for _ in range(c._queue.qsize())]
    assert "msg 5" in remaining


# -- throttling -------------------------------------------------------------


def test_keyed_alerts_are_throttled(client, http):
    CONFIG.telegram.throttle_seconds = 60.0

    accepted = [client.send("reconciliation failed", key="recon") for _ in range(10)]
    drain(client)

    assert accepted.count(True) == 1
    assert client.throttled == 9


def test_unkeyed_alerts_are_never_throttled(client):
    CONFIG.telegram.throttle_seconds = 60.0
    assert all(client.send(f"trade {i}") for i in range(5))


def test_different_keys_do_not_throttle_each_other(client):
    CONFIG.telegram.throttle_seconds = 60.0
    assert client.send("a", key="one") is True
    assert client.send("b", key="two") is True


def test_throttle_expires(client):
    CONFIG.telegram.throttle_seconds = 0.05
    assert client.send("a", key="k") is True
    assert client.send("a", key="k") is False
    time.sleep(0.06)
    assert client.send("a", key="k") is True


# -- message content --------------------------------------------------------


def order_record(filled=10, requested=10, dry_run=False, state=OrderState.FILLED):
    intent = OrderIntent(ticker="KXTEST-1", action="buy", side="yes", count=requested,
                         limit_price_cents=52.0, time_in_force="IOC")
    from core.order_state import OrderRecord

    return OrderRecord(
        client_order_id=intent.client_order_id(), intent_key=intent.intent_key(),
        ticker="KXTEST-1", action="buy", side="yes", requested_count=requested,
        limit_price_cents=52.0, time_in_force="IOC", state=state,
        filled_count=filled, avg_fill_price_cents=52.0 if filled else None,
        fees_cents=1.75 * filled, dry_run=dry_run,
    )


def test_a_live_fill_is_labelled_as_filled(client, http):
    client.notify_trade(order_record())
    drain(client)
    text = http.posts[0]["json"]["text"]
    assert "FILLED" in text and "PAPER" not in text
    assert "KXTEST-1" in text and "YES" in text and "10/10" in text


def test_a_paper_trade_is_clearly_labelled(client, http):
    client.notify_trade(order_record(dry_run=True, state=OrderState.DRY_RUN))
    drain(client)
    text = http.posts[0]["json"]["text"]
    assert "PAPER TRADE" in text
    assert "no real order sent" in text


def test_a_zero_fill_is_distinguished_from_a_fill(client, http):
    client.notify_trade(order_record(filled=0, state=OrderState.CANCELLED))
    drain(client)
    assert "NO FILL" in http.posts[0]["json"]["text"]


def test_a_partial_fill_is_distinguished(client, http):
    client.notify_trade(order_record(filled=3, state=OrderState.PARTIALLY_FILLED))
    drain(client)
    text = http.posts[0]["json"]["text"]
    assert "PARTIAL" in text and "3/10" in text


def test_kill_switch_alert_carries_the_reason_and_pnl(client, http):
    client.notify_kill_switch("realized daily PnL -105.00 breached limit -100.00",
                              -105.0, 1000.0)
    drain(client)
    text = http.posts[0]["json"]["text"]
    assert "KILL SWITCH" in text
    assert "-105.00" in text
    assert "breached limit" in text
    assert "survives restart" in text


def test_startup_alert_states_the_mode_unambiguously(client, http):
    client.notify_startup(env="prod", dry_run=False, strategy="taker",
                          balance_usd=250.0, positions=2, exposure_usd=40.0,
                          bankroll_usd=250.0)
    drain(client)
    text = http.posts[0]["json"]["text"]
    assert "LIVE — REAL ORDERS" in text
    assert "prod" in text


def test_startup_alert_marks_paper_mode(client, http):
    client.notify_startup(env="demo", dry_run=True, strategy="taker",
                          balance_usd=0, positions=0, exposure_usd=0, bankroll_usd=0)
    drain(client)
    assert "PAPER" in http.posts[0]["json"]["text"]


def test_stall_alert_names_what_stalled(client, http):
    client.notify_stalled("reconciliation", 1800)
    drain(client)
    text = http.posts[0]["json"]["text"]
    assert "RECONCILIATION" in text and "30 MINUTES" in text


# -- risk integration -------------------------------------------------------


def test_the_kill_switch_alerts_when_it_trips(edge_store, order_store, http):
    from workers.risk_guardrail import KillSwitchTripped, RiskGuardrail
    from tests.conftest import make_verdict
    from tests.test_risk import snapshot

    CONFIG.telegram.min_interval_seconds = 0.0
    CONFIG.telegram.kill_switch_throttle_seconds = 0.0
    notifier = TelegramClient(bot_token="tok", chat_id="42", http=http)
    try:
        order_store.record_settlement(
            settlement_key="k1", ticker="KXA-1", realized_pnl=-500.0,
            settled_at=time.time(), fill_id=None,
        )
        risk = RiskGuardrail(1000.0, store=edge_store, order_store=order_store,
                             notifier=notifier)
        with pytest.raises(KillSwitchTripped):
            risk.evaluate(make_verdict(), snapshot())
        drain(notifier)

        assert notifier.sent == 1
        assert "KILL SWITCH" in http.posts[0]["json"]["text"]
    finally:
        notifier.close(flush=False)


def test_a_broken_notifier_does_not_stop_the_kill_switch(edge_store, order_store):
    """The halt must take effect even if alerting is completely broken."""
    from workers.risk_guardrail import KillSwitchTripped, RiskGuardrail
    from tests.conftest import make_verdict
    from tests.test_risk import snapshot

    class BrokenNotifier:
        def notify_kill_switch(self, *a, **kw):
            raise RuntimeError("telegram exploded")

    order_store.record_settlement(
        settlement_key="k1", ticker="KXA-1", realized_pnl=-500.0,
        settled_at=time.time(), fill_id=None,
    )
    risk = RiskGuardrail(1000.0, store=edge_store, order_store=order_store,
                         notifier=BrokenNotifier())

    with pytest.raises(KillSwitchTripped):
        risk.evaluate(make_verdict(), snapshot())
    assert edge_store.load_kill_switch()["tripped"] is True


# -- health watchdog --------------------------------------------------------


def test_health_alerts_when_scans_stop():
    import main

    CONFIG.telegram.stall_alert_seconds = 60.0
    sent = []

    class Recorder:
        def notify_stalled(self, what, seconds):
            sent.append((what, seconds))

    health = main.Health(Recorder())
    health.last_scan_at = time.time() - 3600
    health.last_reconcile_at = time.time()

    health.check_stalled()

    assert [w for w, _ in sent] == ["scan"]


def test_health_stays_quiet_while_progress_is_being_made():
    import main

    CONFIG.telegram.stall_alert_seconds = 60.0
    sent = []

    class Recorder:
        def notify_stalled(self, what, seconds):
            sent.append(what)

    health = main.Health(Recorder())
    health.check_stalled()
    assert sent == []


def test_the_watchdog_can_be_disabled():
    import main

    CONFIG.telegram.stall_alert_seconds = 0
    sent = []

    class Recorder:
        def notify_stalled(self, what, seconds):
            sent.append(what)

    health = main.Health(Recorder())
    health.last_scan_at = 0
    health.check_stalled()
    assert sent == []


def test_alert_helper_swallows_notifier_exceptions():
    import main

    class Broken:
        def notify_trade(self, *a, **kw):
            raise RuntimeError("boom")

    main._alert(Broken(), "notify_trade", None)   # must not raise
    main._alert(None, "notify_trade", None)       # no notifier configured
