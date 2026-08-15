"""
Standing-signal alert suppression.

Production: KXRAINSHARD2-26AUG15-SFO BUY NO alerted four times in one day —
07:34, 08:06, 10:05, 11:21 — each treated as a fresh approval.

The cause is NOT that paper orders go unpersisted. They are persisted, in
full, and the duplicate guard does find them (see
test_paper_orders_are_persisted_and_found below, and the 11:45 production log
line "live order 2bc2a12a ... already exists ... (state=dry_run)").

The cause is that `OrderIntent.intent_key()` includes an hourly
`dedupe_bucket`. Once the hour rolls, the same standing signal hashes to a
different intent key, no duplicate is found, a new paper order is written,
and notify_trade fires again. Four alerts, four different hour buckets.

That hourly retry is correct for *submission* — it is what lets an unchanged
signal be tried again later in the day. It is wrong for *alerting*, which had
inherited the submission clock by accident. So the fix lives at the alert
layer, on a key that excludes the time bucket.
"""
from __future__ import annotations


import pytest

from config import CONFIG
from core.order_state import OrderIntent, OrderState
from memory.order_store import OrderStore, SignalAlertStore, signal_key


TICKER = "KXRAINSHARD2-26AUG15-SFO"


@pytest.fixture
def alerts(tmp_path):
    return SignalAlertStore(str(tmp_path / "alerts.db"))


def _key():
    return signal_key(TICKER, "buy", "no", "llm")


# --------------------------------------------------------------------------
# the stated hypothesis, checked rather than assumed
# --------------------------------------------------------------------------

def test_paper_orders_are_persisted_and_found(tmp_path):
    """Paper orders DO persist — so a missing record is not the cause."""
    store = OrderStore(str(tmp_path / "orders.db"))
    intent = OrderIntent(ticker=TICKER, action="buy", side="no", count=91,
                         limit_price_cents=53.0, time_in_force="IOC",
                         source="llm", dedupe_bucket=1000)
    record = store.record_intent(intent)
    record.state = OrderState.DRY_RUN
    record.dry_run = True
    store.update_order(record)

    found = store.find_by_intent_key(intent.intent_key())
    assert found, "a paper order must be persisted and findable by intent key"
    assert found[0].state is OrderState.DRY_RUN
    assert found[0].dry_run is True


def test_the_hourly_bucket_is_what_defeats_the_order_guard():
    """The actual mechanism, pinned so it cannot be misdiagnosed again."""
    base = dict(ticker=TICKER, action="buy", side="no", count=91,
                limit_price_cents=53.0, time_in_force="IOC", source="llm")
    this_hour = OrderIntent(**base, dedupe_bucket=1000)
    next_hour = OrderIntent(**base, dedupe_bucket=1001)

    assert this_hour.intent_key() != next_hour.intent_key(), (
        "identical signal, different hour -> different intent key. This is "
        "why the order-level guard cannot suppress the repeat alert."
    )
    assert signal_key(TICKER, "buy", "no", "llm") == _key(), (
        "the alert key must NOT move with the clock"
    )


# --------------------------------------------------------------------------
# the requested behaviour: many passes, one alert
# --------------------------------------------------------------------------

def test_same_intent_across_many_passes_alerts_once(alerts):
    """Twenty passes over an hour with an unchanged signal: one alert."""
    now = 1_000_000.0
    fired = []

    for i in range(20):
        t = now + i * 30          # a pass every 30 seconds
        decision = alerts.evaluate(_key(), edge=0.33, price_cents=53.0, now=t)
        if decision.should_alert:
            fired.append((t, decision.reason))
            alerts.record(_key(), TICKER, "buy", "no", "llm",
                          edge=0.33, price_cents=53.0, now=t)

    assert len(fired) == 1, f"expected one alert, got {len(fired)}: {fired}"
    assert fired[0][1] == "new signal"


def test_the_four_production_alerts_collapse_to_one(alerts):
    """The exact production timeline: 07:34, 08:06, 10:05, 11:21."""
    day = 1_755_000_000.0
    times = [day + h * 3600 + m * 60
             for h, m in ((7, 34), (8, 6), (10, 5), (11, 21))]
    fired = []

    for t in times:
        # Unchanged signal — same edge, same price, every time.
        decision = alerts.evaluate(_key(), edge=0.33, price_cents=53.0, now=t)
        if decision.should_alert:
            fired.append(t)
            alerts.record(_key(), TICKER, "buy", "no", "llm",
                          edge=0.33, price_cents=53.0, now=t)

    # The reminder TTL legitimately breaks silence on the >1h gaps; what must
    # not happen is one alert per re-derivation.
    assert len(fired) < 4, "four identical alerts was the bug"
    assert fired[0] == times[0], "the first sighting must always alert"


# --------------------------------------------------------------------------
# suppression is never permanent
# --------------------------------------------------------------------------

def test_a_moved_edge_alerts_again(alerts):
    """The second half of the requested behaviour."""
    now = 1_000_000.0
    assert alerts.evaluate(_key(), 0.33, 53.0, now=now).should_alert
    alerts.record(_key(), TICKER, "buy", "no", "llm", 0.33, 53.0, now=now)

    # Same minute, unchanged -> silent.
    assert not alerts.evaluate(_key(), 0.33, 53.0, now=now + 60).should_alert

    # Edge moves past the threshold -> speaks, and says why.
    moved = 0.33 + CONFIG.telegram.alert_edge_move_threshold + 0.01
    decision = alerts.evaluate(_key(), moved, 53.0, now=now + 120)
    assert decision.should_alert
    assert "edge moved" in decision.reason
    assert "33.0%" in decision.reason, "must quote the previous edge"


def test_a_move_below_the_threshold_stays_quiet(alerts):
    now = 1_000_000.0
    alerts.record(_key(), TICKER, "buy", "no", "llm", 0.33, 53.0, now=now)
    just_under = 0.33 + CONFIG.telegram.alert_edge_move_threshold - 0.005
    assert not alerts.evaluate(_key(), just_under, 53.0, now=now + 60).should_alert


def test_a_moved_price_alerts_again(alerts):
    now = 1_000_000.0
    alerts.record(_key(), TICKER, "buy", "no", "llm", 0.33, 53.0, now=now)
    moved = 53.0 + CONFIG.telegram.alert_price_move_cents
    decision = alerts.evaluate(_key(), 0.33, moved, now=now + 60)
    assert decision.should_alert
    assert "price moved" in decision.reason


def test_the_reminder_ttl_breaks_a_long_silence(alerts):
    """A standing trade must not be forgotten, only quietened."""
    now = 1_000_000.0
    alerts.record(_key(), TICKER, "buy", "no", "llm", 0.33, 53.0, now=now)

    ttl = CONFIG.telegram.alert_reminder_seconds
    assert not alerts.evaluate(_key(), 0.33, 53.0, now=now + ttl - 60).should_alert

    decision = alerts.evaluate(_key(), 0.33, 53.0, now=now + ttl + 1)
    assert decision.should_alert
    assert "still standing" in decision.reason


def test_a_different_side_is_a_different_signal(alerts):
    now = 1_000_000.0
    alerts.record(_key(), TICKER, "buy", "no", "llm", 0.33, 53.0, now=now)
    other = signal_key(TICKER, "buy", "yes", "llm")
    assert alerts.evaluate(other, 0.33, 53.0, now=now + 60).should_alert, (
        "flipping side is a genuinely different trade"
    )


def test_quant_and_llm_signals_do_not_suppress_each_other(alerts):
    now = 1_000_000.0
    alerts.record(_key(), TICKER, "buy", "no", "llm", 0.33, 53.0, now=now)
    quant = signal_key(TICKER, "buy", "no", "quant")
    assert alerts.evaluate(quant, 0.33, 53.0, now=now + 60).should_alert


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def test_suppression_survives_a_restart(tmp_path):
    """Otherwise every redeploy re-announces every standing signal."""
    db = str(tmp_path / "alerts.db")
    now = 1_000_000.0

    SignalAlertStore(db).record(_key(), TICKER, "buy", "no", "llm", 0.33, 53.0, now=now)

    reopened = SignalAlertStore(db)          # fresh process
    assert not reopened.evaluate(_key(), 0.33, 53.0, now=now + 60).should_alert


def test_persisted_row_carries_what_the_next_decision_needs(alerts):
    now = 1_000_000.0
    alerts.record(_key(), TICKER, "buy", "no", "llm", 0.33, 53.0, now=now)
    alerts.record(_key(), TICKER, "buy", "no", "llm", 0.40, 55.0, now=now + 7200)

    row = alerts.get(_key())
    assert row["ticker"] == TICKER
    assert row["side"] == "no"
    assert row["last_edge"] == pytest.approx(0.40), "must hold the LATEST edge"
    assert row["last_price_cents"] == pytest.approx(55.0)
    assert row["alert_count"] == 2, "upsert must count, not duplicate the row"
    assert row["last_alerted_at"] == pytest.approx(now + 7200)


def test_missing_edge_does_not_crash_or_spam(alerts):
    """A quant proposal may carry no net edge; absence must not mean 'changed'."""
    now = 1_000_000.0
    alerts.record(_key(), TICKER, "buy", "no", "llm", None, None, now=now)
    assert not alerts.evaluate(_key(), None, None, now=now + 60).should_alert


# --------------------------------------------------------------------------
# the wiring, not just the store
# --------------------------------------------------------------------------

def test_run_once_records_the_alert_and_passes_a_reason(
    client, order_store, edge_store, account, execution, risk, ledger, tmp_path
):
    """Proves main.py actually consults the store, not just that it exists."""
    import main
    from tests.conftest import make_candidate
    from tests.test_pass_loop import (
        StubChecker, StubMaker, StubQuantMaker, StubScout, _positions_follow_fills,
    )

    CONFIG.risk.dry_run = False
    candidates = [make_candidate(ticker=TICKER)]
    _positions_follow_fills(client, candidates)

    alerts = SignalAlertStore(str(tmp_path / "alerts.db"))

    class Notifier:
        def __init__(self):
            self.trades = []

        def notify_trade(self, record, decision=None, reason=""):
            self.trades.append((record.ticker, reason))

        def __getattr__(self, _name):
            return lambda *a, **kw: None

    notifier = Notifier()
    filled = main.run_once(
        StubScout(candidates), StubMaker(), StubQuantMaker(), StubChecker(),
        risk, execution, ledger, account,
        notifier=notifier, alert_store=alerts,
    )

    assert filled == 1
    assert len(notifier.trades) == 1, "first sighting must alert"
    ticker, reason = notifier.trades[0]
    assert ticker == TICKER
    assert reason == "new signal", "the alert must carry why it fired"

    # And the store now holds the suppression state for the next pass. The
    # key is derived from the side actually traded, not assumed — this stub
    # proposes YES, and hard-coding "no" here would have passed against a
    # store that recorded nothing.
    submitted = client.place_order_calls[0]
    key = signal_key(TICKER, "buy", submitted["side"], "llm")
    row = alerts.get(key)
    assert row is not None, "main.py must record the alert, or nothing suppresses"
    assert row["ticker"] == TICKER
    assert row["side"] == submitted["side"]
    assert row["alert_count"] == 1

    # Second pass, unchanged signal, same hour: silent.
    decision = alerts.evaluate(key, row["last_edge"], row["last_price_cents"])
    assert not decision.should_alert, "an unchanged standing signal must go quiet"
