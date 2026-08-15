"""
P1-6: strict schemas for external and model data.

The brief asks specifically for coverage of malformed JSON, missing values,
NaN, infinity, out-of-range values and unknown verdicts. NaN is the one worth
staring at: every comparison against it is False, so a NaN confidence fails a
``>=`` threshold silently and a NaN probability makes every downstream edge
NaN without anything raising.
"""
from __future__ import annotations

import math
import time

import pytest

from config import CONFIG
from core.validation import (
    MarketDataInvalid,
    Quote,
    clamp_text,
    extract_json,
    finite,
    in_unit_interval,
    parse_timestamp,
    validate_checker_output,
    validate_maker_output,
    validate_market,
)


def market(**overrides):
    base = {
        "ticker": "KXTEST-25AUG14-A",
        "title": "Test market",
        "yes_bid": 48,
        "yes_ask": 52,
        "volume": 10_000,
        "close_time": "2030-12-31T00:00:00Z",
    }
    base.update(overrides)
    return base


# -- numeric primitives -----------------------------------------------------


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value):
    assert finite(value) is None
    assert in_unit_interval(value) is None


@pytest.mark.parametrize("value", ["nan", "inf", "Infinity", "-inf"])
def test_non_finite_strings_are_rejected(value):
    """json.loads turns bare NaN/Infinity tokens into real floats, and
    float("nan") parses these strings too."""
    assert finite(value) is None


@pytest.mark.parametrize("value", [None, "", "high", {}, [], object()])
def test_non_numeric_values_are_rejected(value):
    assert finite(value) is None


def test_booleans_are_not_numbers():
    """True == 1 in Python, so a bool would silently become a valid
    probability of 1.0 without this."""
    assert finite(True) is None
    assert in_unit_interval(False) is None


@pytest.mark.parametrize("value", [-0.01, 1.01, 2, -1, 1e9])
def test_out_of_range_probabilities_are_rejected(value):
    assert in_unit_interval(value) is None


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0, "0.73"])
def test_valid_probabilities_are_accepted(value):
    assert in_unit_interval(value) == pytest.approx(float(value))


def test_text_is_bounded():
    assert clamp_text("x" * 100, 10).startswith("x" * 10)
    assert "truncated from 100" in clamp_text("x" * 100, 10)
    assert clamp_text(None, 10) == ""
    assert clamp_text(12345, 10) == ""


def test_timestamp_parsing_handles_seconds_millis_and_iso():
    assert parse_timestamp(1_700_000_000) == 1_700_000_000
    assert parse_timestamp(1_700_000_000_000) == 1_700_000_000
    assert parse_timestamp("2023-11-14T22:13:20Z") == pytest.approx(1_700_000_000, abs=1)
    assert parse_timestamp("not a time") is None
    assert parse_timestamp(float("nan")) is None
    assert parse_timestamp(True) is None


# -- market data ------------------------------------------------------------


def test_a_well_formed_market_validates():
    valid = validate_market(market())
    assert valid.ticker == "KXTEST-25AUG14-A"
    assert valid.quote.yes_bid == 48
    assert valid.seconds_to_close > 0


@pytest.mark.parametrize("field_name", ["ticker", "title"])
def test_missing_required_strings_are_rejected(field_name):
    with pytest.raises(MarketDataInvalid):
        validate_market(market(**{field_name: None}))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "cheap"])
def test_non_finite_prices_are_rejected(bad):
    """Present but not a number is bad data. null is a different thing —
    see test_a_missing_bid_means_no_bid_not_bad_data."""
    with pytest.raises(MarketDataInvalid, match="yes_bid"):
        validate_market(market(yes_bid=bad))


def test_a_missing_bid_means_no_bid_not_bad_data():
    """VERIFIED AGAINST PRODUCTION: ~3,000 of every 5,000 open Kalshi markets
    return a null yes_bid. Treating null as malformed rejected the entire
    catalog and raised a data-quality alarm for what is just an untraded
    market. No bid means an effective bid of 0 — nobody will buy from us."""
    valid = validate_market(market(yes_bid=None))

    assert valid.quote.yes_bid == 0.0
    assert any("no resting bid" in w for w in valid.warnings)


def test_a_missing_ask_means_nothing_to_buy():
    valid = validate_market(market(yes_ask=None))

    assert valid.quote.yes_ask == 100.0
    assert any("no resting ask" in w for w in valid.warnings)


def test_a_market_with_no_quotes_at_all_is_untradeable_not_invalid():
    """It validates, then gets filtered downstream by the spread and volume
    gates — which is the honest outcome: the data is fine, the market is
    just not tradeable."""
    valid = validate_market(market(yes_bid=None, yes_ask=None))

    assert (valid.quote.yes_bid, valid.quote.yes_ask) == (0.0, 100.0)
    assert valid.quote.yes_ask - valid.quote.yes_bid == 100.0


@pytest.mark.parametrize("bid,ask", [(-1, 52), (48, 101), (150, 200)])
def test_prices_outside_zero_to_one_hundred_are_rejected(bid, ask):
    with pytest.raises(MarketDataInvalid, match="outside"):
        validate_market(market(yes_bid=bid, yes_ask=ask))


def test_a_crossed_book_is_rejected():
    """bid > ask happens around halts. The old code let it through as a
    negative spread, which passed the max-spread check and produced a
    meaningless midpoint."""
    with pytest.raises(MarketDataInvalid, match="crossed book"):
        validate_market(market(yes_bid=60, yes_ask=40))


def test_equal_bid_and_ask_is_allowed():
    """A zero spread is a locked market, not a malformed one."""
    valid = validate_market(market(yes_bid=50, yes_ask=50))
    assert valid.quote.yes_bid == valid.quote.yes_ask == 50


def test_untraded_market_quoting_zero_to_one_hundred_is_allowed():
    valid = validate_market(market(yes_bid=0, yes_ask=100))
    assert valid.quote.midpoint_cents == 50


def test_negative_volume_is_rejected():
    with pytest.raises(MarketDataInvalid, match="volume"):
        validate_market(market(volume=-1))


@pytest.mark.parametrize("junk", [float("nan"), "lots", None])
def test_unusable_volume_reads_as_no_data_not_zero(junk):
    """An unparseable liquidity value is "we don't know", not "illiquid".
    Zero would silently filter the market out; None makes Scout say so."""
    assert validate_market(market(volume=junk)).volume is None


def test_liquidity_falls_back_across_fields():
    """VERIFIED AGAINST PRODUCTION: reading only `volume` and defaulting a
    missing key to 0 reported all 79,947 open markets as zero-volume, so every
    one fell under MIN_LIQUIDITY_USD and the bot found nothing to trade."""
    raw = market()
    del raw["volume"]
    raw["volume_24h"] = 250
    raw["open_interest"] = 4000

    # Largest wins — the least likely to be a spuriously quiet window.
    assert validate_market(raw).volume == 4000


def test_no_liquidity_field_at_all_is_none():
    raw = market()
    del raw["volume"]
    assert validate_market(raw).volume is None


def test_scout_does_not_filter_markets_with_no_liquidity_data():
    """The bug shape that cost two rounds: an absent field becoming a value
    that filters everything out, with no error anywhere."""
    from config import CONFIG as _C
    from workers.scout import Scout
    from tests.test_categories import CategoryClient, _event, _market

    raw = _market("KXBTCD-25AUG14-B")
    del raw["volume"]
    client = CategoryClient([_event("KXBTCD-25AUG14", [raw])])
    _C.scout_categories = ["Crypto"]

    assert len(Scout(client).scan()) == 1


def test_an_already_closed_market_is_rejected():
    with pytest.raises(MarketDataInvalid, match="already closed"):
        validate_market(market(close_time="2020-01-01T00:00:00Z"))


def test_unparseable_close_time_is_rejected():
    with pytest.raises(MarketDataInvalid, match="close_time"):
        validate_market(market(close_time="soon"))


def test_between_strike_requires_both_bounds():
    with pytest.raises(MarketDataInvalid, match="between"):
        validate_market(market(strike_type="between", floor_strike=100))


def test_inverted_strike_range_is_rejected():
    with pytest.raises(MarketDataInvalid, match="floor strike"):
        validate_market(
            market(strike_type="between", floor_strike=200, cap_strike=100)
        )


def test_non_finite_strike_is_rejected():
    with pytest.raises(MarketDataInvalid, match="floor_strike"):
        validate_market(market(floor_strike=float("inf")))


def test_last_price_time_is_not_treated_as_quote_freshness():
    """VERIFIED AGAINST PRODUCTION. last_price_time is the last *trade* time,
    not the age of the order book. Using it as quote freshness rejected
    47,000 of 80,000 open markets with ages like "1577s old, limit 60s" — a
    market that last traded 26 minutes ago is perfectly tradeable, its bid
    and ask are current, it is simply quiet."""
    long_ago = time.time() - 3600

    valid = validate_market(market(last_price_time=long_ago))

    assert valid.quote.source == "scan"
    assert not valid.quote.is_stale()


def test_freshness_is_measured_from_our_own_read_time():
    """We have no exchange-side quote timestamp, so MAX_QUOTE_AGE_SECONDS
    bounds how long ago *we* fetched the price. That still stops a proposal
    being acted on minutes after the read; it just cannot say how long the
    book sat unchanged beforehand."""
    valid = validate_market(market())
    assert valid.quote.source == "scan"
    assert valid.quote.age_seconds < 1

    valid.quote.captured_at = time.time() - CONFIG.risk.max_quote_age_seconds - 5
    assert valid.quote.is_stale()


def test_a_missing_quote_timestamp_falls_back_to_read_time_with_a_warning():
    """Honest fallback: we can bound staleness by our own clock, but not by
    how long the quote had already been sitting on Kalshi's side."""
    valid = validate_market(market())
    assert valid.quote.source == "scan"
    assert any("no quote timestamp" in w for w in valid.warnings)


def test_there_is_no_exchange_quote_timestamp_to_use():
    """Kalshi returns no field that means "the book was current as of X".
    last_price_time is a trade time and is deliberately ignored, so every
    quote is stamped with our read time and says so."""
    valid = validate_market(market(last_price_time=time.time() - 5))

    assert valid.quote.source == "scan"
    assert any("no quote timestamp" in w for w in valid.warnings)


# -- Maker output -----------------------------------------------------------


def test_valid_maker_json_parses():
    out = validate_maker_output(
        '{"probability_yes": 0.7, "confidence": 0.8, "reasoning": "because"}'
    )
    assert out.probability_yes == 0.7


def test_maker_json_in_a_code_fence_still_parses():
    out = validate_maker_output(
        '```json\n{"probability_yes": 0.7, "confidence": 0.8, "reasoning": "x"}\n```'
    )
    assert out.probability_yes == 0.7


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "",
        "[1, 2, 3]",
        '{"probability_yes": 0.7}',                                  # missing fields
        '{"probability_yes": "high", "confidence": 0.8, "reasoning": "x"}',
        '{"probability_yes": 1.5, "confidence": 0.8, "reasoning": "x"}',
        '{"probability_yes": -0.2, "confidence": 0.8, "reasoning": "x"}',
        '{"probability_yes": NaN, "confidence": 0.8, "reasoning": "x"}',
        '{"probability_yes": Infinity, "confidence": 0.8, "reasoning": "x"}',
        '{"probability_yes": 0.7, "confidence": NaN, "reasoning": "x"}',
        '{"probability_yes": 0.7, "confidence": 0.8, "reasoning": ""}',
        '{"probability_yes": 0.7, "confidence": 0.8}',
    ],
)
def test_bad_maker_output_produces_no_proposal(payload):
    assert validate_maker_output(payload) is None


def test_maker_nan_would_have_been_silently_accepted_before():
    """Documents the hazard: NaN survives json.loads and float(), and every
    comparison against it is False, so a threshold check passes it through as
    'not below the threshold'."""
    import json

    parsed = json.loads('{"probability_yes": NaN}')
    assert math.isnan(parsed["probability_yes"])
    assert not (parsed["probability_yes"] < 0.04)  # the old gate
    assert validate_maker_output('{"probability_yes": NaN, "confidence": 0.8, '
                                 '"reasoning": "x"}') is None


def test_maker_reasoning_is_length_bounded():
    out = validate_maker_output(
        '{"probability_yes": 0.7, "confidence": 0.8, "reasoning": "%s"}' % ("x" * 50_000)
    )
    assert len(out.reasoning) < CONFIG.risk.max_reasoning_chars + 100


# -- Checker output ---------------------------------------------------------


def test_valid_checker_json_parses():
    out = validate_checker_output(
        '{"verdict": "approve", "confidence": 0.9, "reasoning": "sound"}'
    )
    assert (out.verdict, out.confidence) == ("approve", 0.9)


@pytest.mark.parametrize("verdict", ["approve", "reject", "abstain"])
def test_all_three_verdicts_are_accepted(verdict):
    out = validate_checker_output(
        '{"verdict": "%s", "confidence": 0.9, "reasoning": "x"}' % verdict
    )
    assert out.verdict == verdict


def test_verdict_case_and_whitespace_are_normalised():
    out = validate_checker_output(
        '{"verdict": "  APPROVE ", "confidence": 0.9, "reasoning": "x"}'
    )
    assert out.verdict == "approve"


@pytest.mark.parametrize(
    "payload",
    [
        '{"verdict": "approved", "confidence": 0.9, "reasoning": "x"}',
        '{"verdict": "APPROVE!", "confidence": 0.9, "reasoning": "x"}',
        '{"verdict": "yes", "confidence": 0.9, "reasoning": "x"}',
        '{"verdict": "maybe", "confidence": 0.9, "reasoning": "x"}',
        '{"verdict": 1, "confidence": 0.9, "reasoning": "x"}',
        '{"verdict": null, "confidence": 0.9, "reasoning": "x"}',
        '{"confidence": 0.9, "reasoning": "x"}',
    ],
)
def test_unknown_verdicts_abstain_rather_than_approve(payload):
    """No prefix matching, no 'approved' -> 'approve'. A model that has
    drifted off the contract abstains instead of having its output guessed."""
    out = validate_checker_output(payload)
    assert out.verdict == "abstain"
    assert out.confidence == 0.0


@pytest.mark.parametrize(
    "payload",
    [
        '{"verdict": "approve", "confidence": "very high", "reasoning": "x"}',
        '{"verdict": "approve", "confidence": NaN, "reasoning": "x"}',
        '{"verdict": "approve", "confidence": Infinity, "reasoning": "x"}',
        '{"verdict": "approve", "confidence": 1e9, "reasoning": "x"}',
        '{"verdict": "approve", "confidence": -1, "reasoning": "x"}',
        '{"verdict": "approve", "reasoning": "x"}',
    ],
)
def test_invalid_confidence_abstains(payload):
    out = validate_checker_output(payload)
    assert out.verdict == "abstain"


def test_malformed_json_abstains_and_does_not_raise():
    for payload in ["", "not json", "{oh no", None, 42, []]:
        assert validate_checker_output(payload).verdict == "abstain"


def test_a_huge_confidence_cannot_clear_the_threshold():
    """The concrete hazard: confidence 1e9 passes any >= check, so an
    out-of-range value would have approved every trade."""
    from workers.checker import Verdict
    from tests.conftest import make_verdict

    out = validate_checker_output(
        '{"verdict": "approve", "confidence": 1000000000, "reasoning": "x"}'
    )
    verdict = Verdict(proposal=make_verdict().proposal, verdict=out.verdict,
                      confidence=out.confidence, reasoning=out.reasoning)
    assert not verdict.approved


def test_extract_json_returns_none_for_non_objects():
    assert extract_json("[1,2]") is None
    assert extract_json(None) is None
    assert extract_json({"a": 1}) == {"a": 1}


# -- Quote ------------------------------------------------------------------


def test_quote_executable_price_is_side_aware():
    quote = Quote(yes_bid=48, yes_ask=52, captured_at=time.time())
    assert quote.executable_price_cents("yes") == 52
    assert quote.executable_price_cents("no") == 52
    assert quote.midpoint_cents == 50


def test_quote_staleness_uses_the_configured_limit():
    quote = Quote(yes_bid=48, yes_ask=52,
                  captured_at=time.time() - CONFIG.risk.max_quote_age_seconds - 1)
    assert quote.is_stale()
    assert not quote.is_stale(max_age=1e6)
