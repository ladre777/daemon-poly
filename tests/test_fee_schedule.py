"""
Tests for the transcribed Kalshi fee schedule.

The central test is :func:`test_every_published_table_value_is_reproduced`.
It asserts the module against all 42 values Kalshi actually prints, not
against a formula reimplemented from the same reading of the prose that
produced the code. If the transcription is wrong, that test is what catches
it, and it is the only test here whose expected values were typed from the
document rather than derived.

Everything else guards a way the schedule could be misapplied: rounding in
the wrong place, a default substituted for a fact, an ambiguity resolved by
guessing.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core import fee_schedule as fs
from core.fee_schedule import Unavailable

#: Transcribed verbatim from the General Trading Fees Table, pages 4-5 of
#: the schedule effective 2026-07-07.
#: (price of 1 contract, fee for 1 contract, fee for 100 contracts)
PUBLISHED_TABLE = [
    ("0.01", "0.01", "0.07"), ("0.05", "0.01", "0.34"),
    ("0.10", "0.01", "0.63"), ("0.15", "0.01", "0.90"),
    ("0.20", "0.02", "1.12"), ("0.25", "0.02", "1.32"),
    ("0.30", "0.02", "1.47"), ("0.35", "0.02", "1.60"),
    ("0.40", "0.02", "1.68"), ("0.45", "0.02", "1.74"),
    ("0.50", "0.02", "1.75"), ("0.55", "0.02", "1.74"),
    ("0.60", "0.02", "1.68"), ("0.65", "0.02", "1.60"),
    ("0.70", "0.02", "1.47"), ("0.75", "0.02", "1.32"),
    ("0.80", "0.02", "1.12"), ("0.85", "0.01", "0.90"),
    ("0.90", "0.01", "0.63"), ("0.95", "0.01", "0.34"),
    ("0.99", "0.01", "0.07"),
]


# ---------------------------------------------------------------------------
# the published table is the authority
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("price,fee_1,fee_100", PUBLISHED_TABLE)
def test_every_published_table_value_is_reproduced(price, fee_1, fee_100):
    """All 42 printed values, against a standard (unlisted) series."""
    assert fs.fee_dollars(price, 1, series="KXBTCD") == Decimal(fee_1)
    assert fs.fee_dollars(price, 100, series="KXBTCD") == Decimal(fee_100)


def test_the_table_is_symmetric_because_the_document_says_so():
    """Asserted from the published values, not from the formula.

    P(1-P) is symmetric analytically, but that is not the claim being
    tested — the claim is that Kalshi's *printed* fees are symmetric, and
    they are: $0.05 and $0.95 both cost $0.34 per hundred, $0.01 and $0.99
    both cost $0.07.
    """
    by_price = {p: (f1, f100) for p, f1, f100 in PUBLISHED_TABLE}
    pairs = [("0.01", "0.99"), ("0.05", "0.95"), ("0.10", "0.90"),
             ("0.15", "0.85"), ("0.20", "0.80"), ("0.25", "0.75"),
             ("0.30", "0.70"), ("0.35", "0.65"), ("0.40", "0.60"),
             ("0.45", "0.55")]
    for lo, hi in pairs:
        assert by_price[lo] == by_price[hi], f"{lo} and {hi} differ in the table"
        assert (fs.fee_dollars(lo, 100, series="KXBTCD")
                == fs.fee_dollars(hi, 100, series="KXBTCD"))


# ---------------------------------------------------------------------------
# boundaries and rounding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("price,expected", [
    ("0.01", "0.01"), ("0.05", "0.01"), ("0.50", "0.02"),
    ("0.95", "0.01"), ("0.99", "0.01"),
])
def test_boundary_prices_for_a_single_contract(price, expected):
    assert fs.fee_dollars(price, 1, series="KXBTCD") == Decimal(expected)


def test_rounding_is_applied_once_to_the_whole_order_not_per_contract():
    """The distinction this module exists for.

    ``C`` sits inside the round-up in the published formula. Ten contracts
    at 50c is one rounding of $0.175, not ten roundings of $0.0175.
    """
    ten = fs.fee_dollars("0.50", 10, series="KXBTCD")
    assert ten == Decimal("0.18")          # ceil(0.07 * 10 * 0.25)
    # Per-contract rounding would charge 10 x $0.02 = $0.20 instead.
    assert ten < fs.fee_dollars("0.50", 1, series="KXBTCD") * 10


def test_rounding_direction_is_up_never_nearest():
    """A raw fee just above a cent boundary must cost the next whole cent.

    Rounding to nearest here would understate a cost that is subtracted
    from edge, which is the direction that puts on trades unable to clear
    their own fees.
    """
    # 3 contracts at 50c: 0.07 * 3 * 0.25 = 0.0525 -> up to 0.06, not 0.05.
    assert fs.fee_dollars("0.50", 3, series="KXBTCD") == Decimal("0.06")
    # 100 at 45c: 1.7325 -> 1.74, not 1.73.
    assert fs.fee_dollars("0.45", 100, series="KXBTCD") == Decimal("1.74")


def test_an_exact_cent_is_not_pushed_to_the_next_one():
    """Ceiling must be a no-op on a value already on the boundary."""
    # 100 at 50c is exactly 1.75.
    assert fs.fee_dollars("0.50", 100, series="KXBTCD") == Decimal("1.75")
    # 100 at 30c is exactly 1.47.
    assert fs.fee_dollars("0.30", 100, series="KXBTCD") == Decimal("1.47")


def test_a_fee_bearing_order_always_costs_at_least_a_cent():
    """Consequence of ceiling the aggregate: there is no sub-cent fee."""
    for price in ("0.01", "0.02", "0.99"):
        assert fs.fee_dollars(price, 1, series="KXBTCD") >= Decimal("0.01")


def test_cents_helper_agrees_with_the_dollar_form():
    assert fs.fee_cents(50, 100, series="KXBTCD") == Decimal("175")
    assert fs.fee_cents(1, 1, series="KXBTCD") == Decimal("1")


# ---------------------------------------------------------------------------
# multipliers
# ---------------------------------------------------------------------------

def test_the_families_this_bot_trades_are_all_standard():
    """None appear in the Non-Standard table, so all take the default M=1.

    This is the answer to "is crypto charged more?" — it is not.
    """
    for series in ("KXBTC", "KXBTCD", "KXBTC15M", "KXETH", "KXETHD",
                   "KXWTI", "KXHIGHNY", "KXHIGHCHI"):
        assert series not in fs.SERIES_FEES
        assert fs.multipliers_for(series, side="taker") == 1


def test_the_yearly_crypto_series_are_fee_free_not_more_expensive():
    """KXBTCY and KXETHY carry M=0 on both sides — the opposite of the
    third-party claim that crypto is charged a higher multiplier."""
    for series in ("KXBTCY", "KXETHY"):
        assert fs.multipliers_for(series, side="taker") == 0
        assert fs.multipliers_for(series, side="maker") == 0
        assert fs.fee_dollars("0.50", 100, series=series) == Decimal("0.00")


def test_a_published_zero_is_a_number_not_an_absence():
    """M=0 means the fee really is zero. That must be Decimal('0.00'), not
    Unavailable — refusing to state a fee the document states would be as
    wrong as inventing one."""
    got = fs.fee_dollars("0.50", 100, series="KXBTCY")
    assert isinstance(got, Decimal)
    assert got == Decimal("0.00")


def test_maker_is_free_by_default_on_standard_markets():
    """The maker multiplier defaults to 0, so standard markets carry no
    maker fee at all."""
    assert fs.multipliers_for("KXBTCD", side="maker") == 0
    assert fs.fee_dollars("0.50", 100, series="KXBTCD",
                          side="maker") == Decimal("0.00")


def test_where_a_maker_fee_applies_it_is_a_quarter_of_taker():
    """0.0175 / 0.07 = 0.25 exactly. PGA Tour carries maker M=1."""
    assert fs.MAKER_RATE == fs.TAKER_RATE / 4
    taker = fs.fee_dollars("0.50", 100, series="KXPGATOUR", side="taker")
    maker = fs.fee_dollars("0.50", 100, series="KXPGATOUR", side="maker")
    assert taker == Decimal("1.75")
    assert maker == Decimal("0.44")     # 0.4375 rounded up
    assert maker < taker


def test_golf_is_a_listed_series_and_standard():
    """KXPGATOUR is the one family this bot trades that IS listed."""
    assert fs.SERIES_FEES["KXPGATOUR"].taker_multiplier == 1
    assert fs.SERIES_FEES["KXPGATOUR"].maker_multiplier == 1


def test_series_is_parsed_from_a_full_ticker():
    assert fs.series_of("KXBTCD-26AUG2717-B80125") == "KXBTCD"
    assert fs.series_of("KXPGATOUR-26AUG27-SCHEF") == "KXPGATOUR"
    assert fs.series_of("KXBTCD") == "KXBTCD"
    assert fs.series_of("") == ""


# ---------------------------------------------------------------------------
# refusing to guess
# ---------------------------------------------------------------------------

def test_an_ambiguous_series_yields_unavailable_never_a_number():
    """KXMVE's row did not extract with two legible columns. A guess there
    would be a silently wrong cost on every leg of a combo."""
    got = fs.fee_dollars("0.50", 100, series="KXMVE")
    assert isinstance(got, Unavailable)
    assert "12" in got.reason


def test_perpetual_futures_yield_unavailable():
    """Priced in basis points on a volume tier the ledger does not carry."""
    got = fs.fee_dollars("0.50", 100, series="KXPERPBTC")
    assert isinstance(got, Unavailable)
    assert "trailing volume" in got.reason or "tiered" in got.reason


def test_a_missing_series_code_yields_unavailable():
    assert isinstance(fs.multipliers_for("", side="taker"), Unavailable)
    assert isinstance(fs.fee_dollars("0.50", 1, series=""), Unavailable)


def test_an_impossible_price_yields_unavailable():
    for bad in ("-0.10", "1.50"):
        assert isinstance(fs.fee_dollars(bad, 1, series="KXBTCD"), Unavailable)


def test_zero_or_negative_count_yields_unavailable():
    for bad in (0, -5, None):
        assert isinstance(fs.fee_dollars("0.50", bad, series="KXBTCD"),
                          Unavailable)


def test_unavailable_refuses_to_be_arithmetic():
    """Same guarantee as reporting.evidence.Unavailable: an unverified fee
    must not quietly become 0.0 inside a cost calculation."""
    u = fs.fee_dollars("0.50", 100, series="KXMVE")
    with pytest.raises(TypeError):
        _ = u + Decimal("1")     # type: ignore[operator]
    with pytest.raises(TypeError):
        _ = float(u)             # type: ignore[arg-type]


def test_an_unknown_side_is_a_programming_error_not_a_silent_default():
    with pytest.raises(ValueError):
        fs.multipliers_for("KXBTCD", side="both")


# ---------------------------------------------------------------------------
# provenance and absent claims
# ---------------------------------------------------------------------------

def test_provenance_records_the_source_and_dates():
    p = fs.provenance()
    assert p["url"] == "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
    assert p["effective_date"] == "2026-07-07"
    assert p["retrieved_date"] == "2026-08-27"
    assert p["taker_rate"] == "0.07"
    assert p["maker_rate"] == "0.0175"


def test_no_per_contract_cap_is_asserted():
    """A $0.035 cap appears in third-party write-ups and nowhere in the
    document. Recording it as None with a note keeps the absence visible
    rather than merely unmentioned."""
    p = fs.provenance()
    assert p["per_contract_cap"] is None
    assert "not supported" in p["per_contract_cap_note"]
    # And no cap actually binds: 100 contracts at 50c would exceed
    # 100 x $0.035 = $3.50 only above that, but check nothing clamps.
    assert fs.fee_dollars("0.50", 1000, series="KXBTCD") == Decimal("17.50")


def test_the_rounding_discrepancy_is_recorded_not_hidden():
    """The prose says centicent, the table says cent. The table is
    implemented and the conflict is stated."""
    note = fs.provenance()["rounding_note"]
    assert "centicent" in note
    assert "42/42" in note


def test_the_transcribed_table_covers_the_expected_series_count():
    """A crude guard against a truncated transcription: the non-standard
    table ran to roughly ninety series across pages 6-11."""
    assert len(fs.SERIES_FEES) >= 85
    for entry in fs.SERIES_FEES.values():
        assert entry.maker_multiplier in (0, 1)
        assert entry.taker_multiplier in (0, 1)
