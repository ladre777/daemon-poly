"""
Recovering a verdict from a Checker response that ran out of room.

Production produced this several times an hour, on a deployment that already
had an explicit ``stop_reason == "max_tokens"`` guard::

    Checker returned unparseable JSON for KXCLARITYVOTE-26JUL-JAN01:
    {"verdict": "reject", "confidence": 0.65, "reasoning": "CLARITY Act is
     crypto market structure legislation with significant bipartisan momentum

A real verdict at a real confidence, thrown away because the prose was cut
off — and recorded in the calibration data as a parse error rather than as
the rejection it was.

The recovery is deliberately narrow. It parses a strict prefix of what the
model actually wrote; it never completes a field that was still being
written. And it is asymmetric: a recovered rejection is honoured, a recovered
approval is not.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from core.validation import repair_truncated_json, validate_checker_output

from tests.conftest import make_verdict

#: The shape production actually emits — cut off inside `reasoning`.
TRUNCATED_REJECT = (
    '{"verdict": "reject", "confidence": 0.65, "reasoning": "CLARITY Act is '
    'crypto market structure legislation with significant bipartisan momentum'
)
TRUNCATED_APPROVE = (
    '{"verdict": "approve", "confidence": 0.85, "reasoning": "The maker\'s '
    'estimate looks well supported by the'
)


# -- the repair itself -----------------------------------------------------


def test_complete_fields_before_the_cut_are_recovered():
    parsed = repair_truncated_json(TRUNCATED_REJECT)

    assert parsed == {"verdict": "reject", "confidence": 0.65}


def test_the_field_that_was_still_being_written_is_dropped_not_completed():
    """The half-written reasoning must not appear at all. Inventing a closing
    quote would fabricate a value the model never finished."""
    parsed = repair_truncated_json(TRUNCATED_REJECT)

    assert "reasoning" not in parsed


def test_a_complete_object_needs_no_repair_path():
    """Sanity: the repair is only ever reached after the strict parse failed,
    but it must not mangle valid input if it is."""
    parsed = repair_truncated_json('{"verdict": "reject", "confidence": 0.6, "x": 1}')

    assert parsed["verdict"] == "reject"
    assert parsed["confidence"] == 0.6


def test_a_cut_before_any_field_completed_recovers_nothing():
    assert repair_truncated_json('{"verdict": "rej') is None


def test_commas_inside_strings_are_not_treated_as_field_boundaries():
    """The bug this would have if it split on commas naively: reasoning text
    is full of them, and cutting inside a string yields nonsense."""
    raw = '{"verdict": "reject", "reasoning": "first, second, third", "conf'

    parsed = repair_truncated_json(raw)

    assert parsed == {"verdict": "reject", "reasoning": "first, second, third"}


def test_escaped_quotes_do_not_end_the_string_early():
    raw = '{"verdict": "reject", "reasoning": "he said \\"no\\", firmly", "c'

    parsed = repair_truncated_json(raw)

    assert parsed["reasoning"] == 'he said "no", firmly'


def test_nested_objects_do_not_confuse_the_depth_tracking():
    raw = '{"verdict": "reject", "meta": {"a": 1, "b": 2}, "confidence": 0.'

    parsed = repair_truncated_json(raw)

    assert parsed["verdict"] == "reject"
    assert parsed["meta"] == {"a": 1, "b": 2}
    assert "confidence" not in parsed


def test_prose_before_the_object_is_skipped():
    parsed = repair_truncated_json('Here is my answer:\n' + TRUNCATED_REJECT)

    assert parsed["verdict"] == "reject"


def test_non_strings_and_junk_recover_nothing():
    assert repair_truncated_json(None) is None
    assert repair_truncated_json(42) is None
    assert repair_truncated_json("no object here at all") is None
    assert repair_truncated_json("") is None


# -- what the Checker does with a recovered verdict ------------------------


def test_a_truncated_rejection_is_honoured():
    """It can only ever refuse a trade, so acting on it is safe — and it keeps
    a real judgement out of the calibration data as a parse error."""
    out = validate_checker_output(TRUNCATED_REJECT, ticker="KXTEST-1")

    assert out.verdict == "reject"
    assert out.confidence == 0.65


def test_a_truncated_rejection_is_marked_as_recovered():
    """So a ledger row cannot later be read as a complete judgement."""
    out = validate_checker_output(TRUNCATED_REJECT, ticker="KXTEST-1")

    assert "truncated" in out.reasoning.lower()


def test_a_truncated_approval_is_never_honoured():
    """The asymmetry. A cut-off approval would authorise real money on a
    response whose reasoning stopped early — and the caveat that would have
    changed the verdict is the part most likely to be missing."""
    out = validate_checker_output(TRUNCATED_APPROVE, ticker="KXTEST-1")

    assert out.verdict == "abstain"
    assert out.confidence == 0.0
    assert out.reasoning == "truncated_approval"


def test_a_truncated_approval_says_why_in_the_log(caplog):
    with caplog.at_level("WARNING"):
        validate_checker_output(TRUNCATED_APPROVE, ticker="KXTEST-1")

    assert "not an approval" in caplog.text


def test_a_response_with_no_recoverable_verdict_still_abstains():
    out = validate_checker_output('{"verdict": "rej', ticker="KXTEST-1")

    assert out.verdict == "abstain"
    assert out.reasoning == "parse_error"


def test_a_truncated_response_missing_confidence_abstains():
    """Recovery is not permission to fill gaps: without a confidence there is
    no verdict to act on."""
    out = validate_checker_output(
        '{"verdict": "reject", "reasoning": "some text here", "confidence": 0.',
        ticker="KXTEST-1",
    )

    assert out.verdict == "abstain"
    assert out.reasoning == "invalid_confidence"


def test_an_unparseable_response_logs_its_true_length(caplog):
    """A 200-char excerpt could not tell a token-cap cutoff from a
    complete-but-malformed answer — the logged payload ended mid-sentence
    either way. Those two readings have opposite fixes, and the ambiguity
    cost several investigations."""
    with caplog.at_level("WARNING"):
        validate_checker_output("total garbage, no object, " + "x" * 400,
                                ticker="KXTEST-1")

    assert "426 chars, complete" in caplog.text, (
        "the log must say the response was whole, not merely show its start"
    )


def test_an_over_long_response_says_the_logger_did_the_clipping(caplog):
    """The distinction that matters: a clip here means our logger stopped,
    not that the model did."""
    with caplog.at_level("WARNING"):
        validate_checker_output("no object here " + "x" * 8000, ticker="KXTEST-1")

    assert "clipped by the logger, NOT by the model" in caplog.text


def test_a_complete_response_is_not_marked_as_recovered():
    out = validate_checker_output(
        '{"verdict": "approve", "confidence": 0.9, "reasoning": "looks right"}',
        ticker="KXTEST-1",
    )

    assert out.verdict == "approve"
    assert "truncated" not in out.reasoning.lower()


# -- the Checker must know what year it is ---------------------------------


def test_the_prompt_states_todays_date(monkeypatch):
    """A false rejection traced to the Checker not knowing the date.

    On KXHIGHNY-26AUG17-T84 — a weather market, where our grounding is an
    official NWS forecast from the same station Kalshi settles against — the
    Checker rejected with:

        NWS forecasts don't extend 2+ years out, so the Maker's claimed
        'official forecast' for Aug 2026 is almost certainly a hallucination

    The forecast was real and had been pulled that morning. The Checker was
    reasoning from its training cutoff and concluded a correctly dated market
    was fabricated. That is a factual input error, not a judgement call, and
    it pushes the gate toward refusing exactly the trades we have the best
    case for.
    """
    from datetime import datetime, timezone

    from workers.checker import Checker

    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop here — the prompt is what is under test")

    class FakeClient:
        messages = FakeMessages()

    checker = Checker.__new__(Checker)
    checker._client = FakeClient()

    verdict = make_verdict()
    with pytest.raises(RuntimeError):
        checker.check(verdict.proposal)

    prompt = captured["messages"][0]["content"]
    today = f"{datetime.now(timezone.utc):%Y-%m-%d}"
    assert today in prompt, "the Checker must be told the current date"
    assert "authoritative" in prompt


def test_the_prompt_carries_the_market_close_time(monkeypatch):
    """Knowing today is only half of it — the Checker also has to see when
    the market resolves to judge whether a forecast horizon is plausible."""
    from workers.checker import Checker

    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop")

    class FakeClient:
        messages = FakeMessages()

    checker = Checker.__new__(Checker)
    checker._client = FakeClient()

    verdict = make_verdict()
    with pytest.raises(RuntimeError):
        checker.check(verdict.proposal)

    assert "Market closes:" in captured["messages"][0]["content"]


def test_no_threshold_moved_with_it():
    """This change informs the gate; it does not weaken it."""
    assert CONFIG.risk.checker_min_confidence >= 0.6
