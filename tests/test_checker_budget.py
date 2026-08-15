"""
Checker token budget, truncation detection, and the duplicate-block alert.

Both bugs here were found in live demo logs, and both had the same shape: the
system produced a wrong-but-plausible outcome instead of an error, so the
logs read as normal operation.

1. Verdicts were cut off mid-JSON. The first fix raised max_tokens 500 -> 1500
   and it recurred at 1500, because `max_tokens` caps thinking AND answer
   together and claude-sonnet-5 thinks by default — deliberation expanded to
   fill each larger budget. Nothing checked `stop_reason`, so a truncated
   answer was reported as "unparseable JSON": a budget failure wearing the
   costume of a bad model response.

2. An approved trade produced no Telegram alert, because the duplicate-order
   guard `continue`d past the notification.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from workers.checker import Checker
from workers.maker import Proposal

from tests.conftest import make_candidate


class _Block:
    def __init__(self, text):
        self.text = text


class _ThinkingBlock:
    def __init__(self, thinking):
        self.thinking = thinking


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _RecordingClient:
    """Stands in for anthropic.Anthropic, capturing the request."""

    def __init__(self, response):
        self._response = response
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _checker_with(response):
    checker = Checker.__new__(Checker)
    checker._client = _RecordingClient(response)
    return checker


def _proposal():
    return Proposal(
        candidate=make_candidate(ticker="KXRAINSHARD2-26AUG15-SFO"),
        maker_probability=0.15,
        maker_confidence=0.8,
        reasoning="stub",
    )


# --------------------------------------------------------------------------
# the budget itself
# --------------------------------------------------------------------------

def test_thinking_is_bounded_so_it_cannot_eat_the_whole_budget():
    """The actual root cause: max_tokens is shared with thinking.

    Without an effort bound, raising max_tokens just gives deliberation more
    room and the answer is still truncated — which is exactly what happened
    at 500 and again at 1500.
    """
    checker = _checker_with(_Resp([_Block('{"verdict": "approve", '
                                         '"confidence": 0.8, "reasoning": "ok"}')]))
    checker.check(_proposal())

    request = checker._client.calls[0]
    assert request["output_config"]["effort"] == CONFIG.models.checker_effort
    assert CONFIG.models.checker_effort in {"low", "medium"}, (
        "the Checker is a small fixed-shape judgement; high effort spends the "
        "budget on deliberation the verdict then has no room for"
    )


def test_budget_has_room_for_thinking_and_a_full_verdict():
    assert CONFIG.models.checker_max_tokens >= 4000, (
        "must cover bounded thinking plus a complete JSON verdict; 1500 "
        "truncated real verdicts in production"
    )


# --------------------------------------------------------------------------
# a long verdict survives intact — the test the fix is for
# --------------------------------------------------------------------------

def test_a_long_verdict_is_not_cut_off():
    """Reproduces the production shape: a genuinely long, complete answer.

    The reasoning here is longer than every verdict that was truncated in the
    live logs. With `stop_reason: end_turn` the response is complete, and the
    Checker must return it whole rather than discarding it.
    """
    long_reasoning = (
        "San Francisco in mid-August is firmly in its dry season, with "
        "historical rain probability on any given day typically under five "
        "percent, making a fifty percent market price look badly calibrated "
        "at first glance. " * 12
    ).strip()
    payload = (
        '{"verdict": "approve", "confidence": 0.75, "reasoning": "%s"}'
        % long_reasoning
    )

    checker = _checker_with(_Resp([_ThinkingBlock("deliberating"), _Block(payload)]))
    verdict = checker.check(_proposal())

    assert verdict.verdict == "approve"
    assert verdict.confidence == pytest.approx(0.75)
    assert verdict.reasoning.startswith("San Francisco in mid-August")

    # Two different truncations meet here and must not be confused. The API
    # cutting a response off mid-JSON is a bug (the verdict is lost).
    # Validation clamping the stored reasoning is a deliberate storage bound,
    # and it says so in the text — so a clamped verdict is still a complete,
    # tradeable verdict.
    if len(long_reasoning) > CONFIG.risk.max_reasoning_chars:
        assert "truncated from" in verdict.reasoning, (
            "a storage clamp must be marked, so it is never mistaken for an "
            "answer the model failed to finish"
        )
    assert verdict.verdict == "approve", (
        "the clamp must not affect the verdict — that is what drives the trade"
    )


def test_the_exact_production_verdict_parses():
    """The SEA verdict from the live logs, completed rather than truncated."""
    payload = (
        '{"verdict": "approve", "confidence": 0.72, "reasoning": "Seattle '
        'mid-August climatology genuinely shows low precipitation frequency '
        '(historically ~10-20% of days with measurable rain), so a market '
        'pinned at 50% is plausibly mispriced."}'
    )
    verdict = _checker_with(_Resp([_Block(payload)])).check(_proposal())

    assert verdict.verdict == "approve"
    assert verdict.confidence == pytest.approx(0.72)
    assert "mispriced" in verdict.reasoning


# --------------------------------------------------------------------------
# truncation is detected, not misreported
# --------------------------------------------------------------------------

def test_truncation_is_reported_as_truncation_not_as_bad_json(caplog):
    """The misdiagnosis that made this recur.

    A cut-off verdict was logged as "unparseable JSON", which reads as a
    model-quality problem and sends the investigation after the prompt
    instead of the budget.
    """
    truncated = '{"verdict": "approve", "confidence": 0.72, "reasoning": "Seattle mid-Au'
    checker = _checker_with(_Resp([_Block(truncated)], stop_reason="max_tokens"))

    with caplog.at_level("ERROR"):
        verdict = checker.check(_proposal())

    assert verdict.verdict == "abstain", "a truncated verdict is never traded on"
    assert verdict.confidence == 0.0
    assert "truncated" in verdict.reasoning
    logged = caplog.text.lower()
    assert "cap" in logged and "budget" in logged, (
        "the log must name the budget as the cause, not the model's output"
    )
    assert "checker_max_tokens" in logged, "must name the knob that fixes it"


def test_a_complete_response_is_not_flagged_as_truncated():
    payload = '{"verdict": "reject", "confidence": 0.9, "reasoning": "no edge"}'
    verdict = _checker_with(_Resp([_Block(payload)], stop_reason="end_turn")).check(
        _proposal()
    )
    assert verdict.verdict == "reject"
    assert "truncated" not in verdict.reasoning


def test_truncation_check_precedes_json_parsing():
    """Order matters: a truncated payload is also unparseable.

    If parsing ran first, truncation would still be reported as bad JSON and
    the fix would be invisible.
    """
    checker = _checker_with(_Resp([_Block("not json at all")], stop_reason="max_tokens"))
    verdict = checker.check(_proposal())
    assert "truncated" in verdict.reasoning


def test_prompt_asks_for_a_bounded_reasoning_field():
    from workers.checker import SYSTEM_PROMPT

    assert "4 sentences" in SYSTEM_PROMPT
    assert "cut off" in SYSTEM_PROMPT.lower(), (
        "the model should know a long answer is discarded entirely"
    )


# --------------------------------------------------------------------------
# duplicate-blocked approvals are not silent
# --------------------------------------------------------------------------

def test_duplicate_block_sends_an_alert():
    """Production: edge #88 was approved for 91 contracts and alerted nothing.

    Execution correctly refused to stack a second order on an intent it
    already held — but the guard `continue`d past the notification, so the
    ledger said "approved" and the operator's phone stayed silent.
    """
    from core.telegram_client import TelegramClient

    client = TelegramClient.__new__(TelegramClient)
    sent = []
    client.send = lambda text, key=None, throttle_seconds=None: sent.append((text, key))

    client.notify_duplicate_blocked(
        "KXRAINSHARD2-26AUG15-SFO",
        "live order 2bc2a12a already exists for KXRAINSHARD2-26AUG15-SFO no x91 @ 53c",
    )

    assert len(sent) == 1
    text, key = sent[0]
    assert "KXRAINSHARD2-26AUG15-SFO" in text
    assert "ALREADY HOLDING" in text
    assert key == "duplicate:KXRAINSHARD2-26AUG15-SFO", (
        "keyed so a signal persisting for hours costs one message, not one per pass"
    )
