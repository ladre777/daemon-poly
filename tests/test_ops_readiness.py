"""
Operational safety that has nothing to do with whether an edge is real.

Three things went wrong in ways the trading logic could not have caught:

- $49.98 left the account and nothing said so. The balance was read every
  pass and compared to nothing.
- The kill switch was loaded once at construction, so a flag set from outside
  the process was invisible until the next restart — and a restart is a
  redeploy, which is what a kill switch exists to avoid needing.
- There was no way for a human to trip it at all.

These pin the fixes. None of them touch a gate, a threshold, or a price.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from core.telegram_commands import TelegramCommandListener
from memory.edge_store import EdgeStore
from memory.order_store import OrderStore


@pytest.fixture
def store():
    d = tempfile.mkdtemp()
    return EdgeStore(os.path.join(d, "edges.db"))


@pytest.fixture
def order_store():
    d = tempfile.mkdtemp()
    return OrderStore(os.path.join(d, "orders.db"))


# -- balance baseline ------------------------------------------------------


def test_no_baseline_is_not_a_balance_of_zero(store):
    """The distinction that decides whether the first pass alerts.

    A brand-new database has never seen a balance. Reporting that as 0.0
    would make the first reconcile look like the account had been drained.
    """
    assert store.load_last_balance() is None


def test_a_zero_balance_is_recorded_as_zero_not_absent(store):
    """$0.00 is a fact, and currently the true one. It must round-trip."""
    store.set_last_balance(0.0)

    assert store.load_last_balance() == 0.0
    assert store.load_last_balance() is not None


def test_the_baseline_survives_reopening_the_database(store):
    """Railway restarts on every push; an in-memory baseline would re-alert."""
    store.set_last_balance(4998.0)

    reopened = EdgeStore(store.db_path)

    assert reopened.load_last_balance() == 4998.0


def test_the_balance_baseline_and_the_kill_switch_do_not_clobber_each_other(store):
    """Both live in the same single row, written by different code paths."""
    store.set_kill_switch(True, "daily loss")
    store.set_last_balance(1234.0)

    assert store.load_kill_switch()["tripped"] is True
    assert store.load_kill_switch()["reason"] == "daily loss"
    assert store.load_last_balance() == 1234.0

    store.set_last_balance(0.0)
    assert store.load_kill_switch()["tripped"] is True


def test_the_baseline_timestamp_bounds_the_fill_window(store):
    assert store.load_last_balance_at() is None

    store.set_last_balance(100.0)

    assert store.load_last_balance_at() is not None


def test_fills_recorded_since_is_none_safe(order_store):
    """A missing timestamp must read as 'no fills explain this', which
    prompts a look, rather than raising inside an alert path."""
    assert order_store.fills_recorded_since(None) == 0
    assert order_store.fills_recorded_since(0) == 0


# -- the balance check itself ----------------------------------------------


class FakeSnapshot:
    def __init__(self, balance_cents, reconciled_at=1000.0):
        self.balance_cents = balance_cents
        self.reconciled_at = reconciled_at


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify_balance_change(self, previous_usd, current_usd, bot_fills_since):
        self.calls.append((previous_usd, current_usd, bot_fills_since))


def check(snapshot, store, order_store=None, notifier=None):
    from main import _check_balance_change
    _check_balance_change(snapshot, store, order_store, notifier)


def test_the_first_observation_records_a_baseline_and_stays_quiet(store):
    notifier = RecordingNotifier()

    check(FakeSnapshot(4998.0), store, notifier=notifier)

    assert notifier.calls == []
    assert store.load_last_balance() == 4998.0


def test_a_drain_to_zero_alerts(store):
    """The exact event that passed silently."""
    store.set_last_balance(4998.0)
    notifier = RecordingNotifier()

    check(FakeSnapshot(0.0), store, notifier=notifier)

    assert len(notifier.calls) == 1
    previous, current, fills = notifier.calls[0]
    assert previous == pytest.approx(49.98)
    assert current == 0.0
    assert fills == 0


def test_a_deposit_alerts_too(store):
    """A refund landing is what re-enables trading after a $0 bankroll."""
    store.set_last_balance(0.0)
    notifier = RecordingNotifier()

    check(FakeSnapshot(10_000.0), store, notifier=notifier)

    assert len(notifier.calls) == 1
    previous, current, _ = notifier.calls[0]
    assert previous == 0.0
    assert current == pytest.approx(100.0)


def test_a_move_below_the_threshold_is_ignored(store):
    store.set_last_balance(5000.0)
    notifier = RecordingNotifier()

    check(FakeSnapshot(5050.0), store, notifier=notifier)  # 50c

    assert notifier.calls == []


def test_the_baseline_advances_so_one_move_alerts_once(store):
    store.set_last_balance(5000.0)
    notifier = RecordingNotifier()

    check(FakeSnapshot(0.0), store, notifier=notifier)
    check(FakeSnapshot(0.0), store, notifier=notifier)
    check(FakeSnapshot(0.0), store, notifier=notifier)

    assert len(notifier.calls) == 1


def test_an_unreadable_store_does_not_stop_the_pass():
    """Alerting is not allowed to become a reason not to trade."""
    class Broken:
        def load_last_balance(self):
            raise RuntimeError("disk gone")

    notifier = RecordingNotifier()
    check(FakeSnapshot(0.0), Broken(), notifier=notifier)  # must not raise

    assert notifier.calls == []


# -- kill switch: the re-read ----------------------------------------------


def _guardrail(store):
    from workers.risk_guardrail import RiskGuardrail
    d = tempfile.mkdtemp()
    return RiskGuardrail(
        bankroll_usd=100.0, store=store,
        order_store=OrderStore(os.path.join(d, "o.db")),
    )


def test_a_switch_tripped_after_construction_is_noticed(store):
    """The defect: the flag was read once in __init__ and never again, so an
    operator halt did nothing until the next redeploy."""
    risk = _guardrail(store)
    assert risk.check_kill_switch(bankroll_usd=100.0) is False

    # Somebody else — the Telegram listener, another process — sets it.
    store.set_kill_switch(True, "manual halt via Telegram")

    assert risk.check_kill_switch(bankroll_usd=100.0) is True


def test_the_switch_latches_and_a_stale_false_cannot_clear_it(store):
    """OR, not assignment. A hand-edited or stale False in the database must
    not resurrect a bot that halted on drawdown in this same session."""
    risk = _guardrail(store)
    store.set_kill_switch(True, "manual halt")
    assert risk.check_kill_switch(bankroll_usd=100.0) is True

    store.set_kill_switch(False)

    assert risk.check_kill_switch(bankroll_usd=100.0) is True


def test_only_an_explicit_reset_clears_the_switch(store):
    risk = _guardrail(store)
    store.set_kill_switch(True, "manual halt")
    assert risk.check_kill_switch(bankroll_usd=100.0) is True

    risk.reset_kill_switch()

    assert risk.check_kill_switch(bankroll_usd=100.0) is False


def test_an_unreadable_switch_does_not_fake_a_halt_or_raise(store):
    """Storage being sick must not halt trading, and must not crash the risk
    check — the drawdown rule after it still works and has to run."""
    risk = _guardrail(store)

    class Broken:
        def load_kill_switch(self):
            raise RuntimeError("db locked")

    risk.store = Broken()

    assert risk.check_kill_switch(bankroll_usd=100.0) is False


# -- kill switch: the trip path --------------------------------------------


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeHTTP:
    """Serves one batch of updates, then nothing."""

    def __init__(self, updates):
        self._batches = [updates, []]
        self.requests = []

    def get(self, url, params=None):
        self.requests.append((url, params))
        batch = self._batches.pop(0) if self._batches else []
        return FakeResponse({"ok": True, "result": batch})


def _message(text, chat_id="42", update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _listener(updates, chat_id="42"):
    halted = []
    replies = []
    listener = TelegramCommandListener(
        on_halt=lambda reason: halted.append(reason),
        status_provider=lambda: "STATUS OK",
        send=replies.append,
        bot_token="token",
        chat_id=chat_id,
        http=FakeHTTP(updates),
    )
    return listener, halted, replies


def test_halt_from_the_authorised_chat_trips_the_switch():
    listener, halted, replies = _listener([_message("/halt")])

    for update in listener._get_updates(timeout=0):
        listener._handle(update)

    assert len(halted) == 1
    assert "manual halt via Telegram" in halted[0]
    assert any("Halted" in r for r in replies)


def test_halt_from_any_other_chat_is_ignored_silently():
    """This path reaches the trading loop, and anyone can message a bot they
    find. A reply would confirm the bot is live and listening."""
    listener, halted, replies = _listener(
        [_message("/halt", chat_id="99999")], chat_id="42"
    )

    for update in listener._get_updates(timeout=0):
        listener._handle(update)

    assert halted == []
    assert replies == []
    assert listener.rejected == 1


def test_there_is_no_resume_command():
    """Halting by accident is safe; resuming by accident is not."""
    listener, halted, replies = _listener([_message("/resume")])

    for update in listener._get_updates(timeout=0):
        listener._handle(update)

    assert halted == []
    # It must be actively unrecognised, not silently swallowed.
    assert any("Unrecognised" in r for r in replies)


def test_status_reports_without_changing_anything():
    listener, halted, replies = _listener([_message("/status")])

    for update in listener._get_updates(timeout=0):
        listener._handle(update)

    assert halted == []
    assert replies == ["STATUS OK"]


def test_a_command_with_the_bot_username_still_matches():
    """Group chats deliver "/halt@my_bot"."""
    listener, halted, _ = _listener([_message("/halt@daemon_kalshi_bot now please")])

    for update in listener._get_updates(timeout=0):
        listener._handle(update)

    assert len(halted) == 1


def test_a_failed_halt_says_so_rather_than_confirming():
    """A halt that silently failed is worse than no halt path: the operator
    stops watching."""
    replies = []

    def explode(reason):
        raise RuntimeError("db locked")

    listener = TelegramCommandListener(
        on_halt=explode,
        status_provider=lambda: "STATUS OK",
        send=replies.append,
        bot_token="token",
        chat_id="42",
        http=FakeHTTP([_message("/halt")]),
    )

    for update in listener._get_updates(timeout=0):
        listener._handle(update)

    assert any("HALT FAILED" in r for r in replies)
    assert not any("Halted" in r and "FAILED" not in r for r in replies)


def test_a_broken_status_provider_does_not_kill_the_listener():
    replies = []
    listener = TelegramCommandListener(
        on_halt=lambda r: None,
        status_provider=lambda: 1 / 0,
        send=replies.append,
        bot_token="token",
        chat_id="42",
        http=FakeHTTP([_message("/status")]),
    )

    for update in listener._get_updates(timeout=0):
        listener._handle(update)

    assert any("unavailable" in r for r in replies)


def test_the_listener_is_disabled_without_credentials():
    listener = TelegramCommandListener(
        on_halt=lambda r: None, status_provider=lambda: "",
        bot_token="", chat_id="",
    )

    assert listener.enabled is False
    assert listener.start() is False


def test_the_update_offset_advances_so_a_halt_is_not_replayed():
    listener, _, _ = _listener([_message("/halt", update_id=7)])

    for update in listener._get_updates(timeout=0):
        listener._offset = update["update_id"] + 1

    assert listener._offset == 8


# -- CI gating -------------------------------------------------------------


def test_ci_runs_on_changes_to_every_directory_it_lints():
    """A scripts-only change was linted by CI but never triggered it, so with
    deploy gating on it would ship unchecked."""
    workflow = open(".github/workflows/kalshi-checks.yml", encoding="utf-8").read()
    trigger_block = workflow.split("jobs:")[0]

    linted = [d for d in ("core", "workers", "memory", "backtest", "tests", "scripts")
              if d in workflow.split("ruff check")[1].split("\n")[0]]
    for directory in linted:
        assert f'"{directory}/**"' in trigger_block, (
            f"CI lints {directory}/ but no push to it triggers a run"
        )


# -- Checker truncation: a budget failure must not look like bad JSON ------
#
# Restored after the 2026-08-21 rewrite dropped it. A truncated verdict is
# ALSO unparseable JSON, so whichever check runs second never fires. If the
# parser runs first the symptom is "the model answered badly" and the fix
# (raise the budget) is invisible — which is exactly what happened three
# times before the original stop_reason check was written.


def _proposal_for_truncation():
    from workers.maker import Proposal
    from tests.conftest import make_candidate
    return Proposal(
        candidate=make_candidate(ticker="KXBTCD-26AUG2516-T78999.99"),
        maker_probability=0.62, maker_confidence=0.8, reasoning="stub",
    )


def _checker_whose_llm_raises(exc):
    from workers.checker import Checker

    class Raising:
        def complete(self, *a, **kw):
            raise exc

    checker = Checker.__new__(Checker)
    checker._llm = Raising()
    return checker


def test_a_truncated_verdict_is_reported_as_truncation(caplog):
    from core.llm_client import LLMTruncated

    checker = _checker_whose_llm_raises(LLMTruncated("gemini", "gemini-3.5-flash-lite", 1200))

    with caplog.at_level("ERROR"):
        verdict = checker.check(_proposal_for_truncation())

    assert verdict.verdict == "abstain"
    assert "truncated" in verdict.reasoning
    assert "1200" in verdict.reasoning
    # Must NOT be filed as a generic LLM error or a parse failure.
    assert "llm_error" not in verdict.reasoning
    assert "parse_error" not in verdict.reasoning


def test_the_truncation_log_names_the_lever_to_turn(caplog):
    from core.llm_client import LLMTruncated

    checker = _checker_whose_llm_raises(LLMTruncated("anthropic", "claude-haiku-4-5", 1200))

    with caplog.at_level("ERROR"):
        checker.check(_proposal_for_truncation())

    assert "CHECKER_MAX_TOKENS" in caplog.text
    assert "TRUNCATED" in caplog.text


def test_a_truncated_verdict_still_fails_closed():
    """Fail-closed is the property that must survive regardless of how the
    failure is labelled: a cut-off answer never becomes an approval."""
    from core.llm_client import LLMTruncated

    checker = _checker_whose_llm_raises(LLMTruncated("gemini", "m", 1200))

    assert checker.check(_proposal_for_truncation()).approved is False


def test_a_non_truncation_error_still_takes_the_generic_path():
    """The new branch must not swallow ordinary failures."""
    checker = _checker_whose_llm_raises(RuntimeError("connection reset"))

    verdict = checker.check(_proposal_for_truncation())

    assert verdict.verdict == "abstain"
    assert "llm_error" in verdict.reasoning
    assert "truncated" not in verdict.reasoning
