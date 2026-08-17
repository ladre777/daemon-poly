"""
Crypto settlement specs, pinned against real API payloads.

Two competing claims were in circulation: that all crypto settles on a
60-second CF Benchmarks RTI average, and that 15-minute BTC markets instead
settle from a value carried on the market record. Rather than pick one, three
open markets were pulled from the live API on 2026-08-17 and their
`rules_primary` text read directly (tests/fixtures/kalshi_crypto_markets.py).

The first claim is right about the mechanism for all three families. The
second is refuted — `expiration_value` is empty on every open market. Both
missed three things that change implementations: the index differs per asset,
KXBTC15M's strike is itself a 60-second average, and the comparison operator
is not the same across families.

These tests exist so that none of that has to be re-derived, and so a future
edit cannot quietly unlearn it.
"""
from __future__ import annotations

import pytest

from config import CONFIG
from core.contract_specs import CONTRACT_SPECS, spec_for, usable
from core.validation import _best_liquidity

from tests.fixtures.kalshi_crypto_markets import (
    ALL_CRYPTO_MARKETS,
    KXBTC15M,
    KXBTCD,
    KXETH,
)


# --------------------------------------------------------------------------
# what the exchange actually said
# --------------------------------------------------------------------------

@pytest.mark.parametrize("market", ALL_CRYPTO_MARKETS, ids=lambda m: m["ticker"])
def test_every_family_settles_on_a_sixty_second_rti_average(market):
    rules = market["rules_primary"]
    assert "simple average of the sixty seconds" in rules
    assert "Real-Time Index" in rules or "BRTI" in rules
    assert "60 RTI prices are collected" in market["rules_secondary"]


@pytest.mark.parametrize("market", ALL_CRYPTO_MARKETS, ids=lambda m: m["ticker"])
def test_no_open_market_carries_its_settlement_value(market):
    """Refutes the 'value on the market record' claim, for every family.

    The field exists but is empty while the market is tradeable, so it is a
    post-settlement audit field and useless as a pricing input.
    """
    assert market["expiration_value"] == ""
    assert market["result"] == ""
    assert market["status"] == "active"


@pytest.mark.parametrize("market", ALL_CRYPTO_MARKETS, ids=lambda m: m["ticker"])
def test_the_exchange_warns_against_the_feed_we_currently_use(market):
    """rules_secondary names spot sources as the wrong instrument — which is
    exactly what spot_price_client feeds the quant path today."""
    assert "Coinbase" in market["rules_secondary"]
    assert "is based on CF Benchmarks" in market["rules_secondary"]


def test_bitcoin_and_ether_settle_on_different_indices():
    """The trap a single shared 'RTI' feed would fall into."""
    assert "BRTI" in KXBTC15M["rules_primary"]
    assert "BRTI" in KXBTCD["rules_primary"]
    assert "Ethereum Real-Time Index (ERTI)" in KXETH["rules_primary"]
    assert "BRTI" not in KXETH["rules_primary"]


def test_the_fifteen_minute_strike_is_itself_an_average_not_a_level():
    """The part no secondary source described.

    KXBTC15M compares two 60-second averages. The other families compare one
    average against a static number written into the ticker.
    """
    assert KXBTC15M["rules_primary"].count("simple average of the sixty seconds") == 2
    assert KXBTCD["rules_primary"].count("simple average of the sixty seconds") == 1
    assert "T72749.99" in KXBTCD["ticker"], "fixed strike is in the ticker"
    assert "T2594.99" in KXETH["ticker"]


def test_the_comparison_operator_differs_between_families():
    assert KXBTC15M["strike_type"] == "greater_or_equal"
    assert KXBTCD["strike_type"] == "greater"
    assert KXETH["strike_type"] == "greater"


def test_both_legs_of_the_fifteen_minute_market_land_on_quarter_hours():
    """Why `last_60s_windowed_average_15min` is the settling quantity.

    That field is published only in the final minute before a :00/:15/:30/:45
    close. Both the settlement leg (close) and the strike leg (open) sit on
    quarter-hour boundaries, so it covers both sides of the comparison.
    """
    from datetime import datetime

    for iso in (KXBTC15M["close_time"], KXBTC15M["open_time"]):
        minute = datetime.fromisoformat(iso.replace("Z", "+00:00")).minute
        assert minute in (0, 15, 30, 45), f"{iso} is not a quarter-hour boundary"


# --------------------------------------------------------------------------
# specs record it, and record it honestly
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ticker,prefix,index,basis",
    [
        (KXBTC15M["ticker"], "KXBTC15M", "BRTI", "opening_60s_average"),
        (KXBTCD["ticker"], "KXBTCD", "BRTI", "fixed_level"),
        (KXETH["ticker"], "KXETH", "ETHUSD_RTI", "fixed_level"),
    ],
)
def test_the_spec_records_the_confirmed_mechanism(ticker, prefix, index, basis):
    spec = spec_for(ticker)
    assert spec.prefix == prefix
    assert spec.observation == "rti_60s_average"
    assert spec.settlement_verified is True
    assert spec.settlement_index == index
    assert spec.strike_basis == basis


def test_the_fifteen_minute_spec_matches_before_the_generic_bitcoin_one():
    """Ordering is load-bearing: KXBTC15M starts with 'KXBTC'."""
    prefixes = [s.prefix for s in CONTRACT_SPECS]
    assert prefixes.index("KXBTC15M") < prefixes.index("KXBTC")
    assert prefixes.index("KXBTCD") < prefixes.index("KXBTC")
    assert spec_for(KXBTC15M["ticker"]).prefix == "KXBTC15M"


# --------------------------------------------------------------------------
# the safety property: confirming the RULE must not unlock PRICING
# --------------------------------------------------------------------------

@pytest.mark.parametrize("market", ALL_CRYPTO_MARKETS, ids=lambda m: m["ticker"])
def test_verified_moves_only_together_with_the_feed(market):
    """The invariant the two flags exist to hold apart.

    Confirming a settlement rule does not, on its own, make a family
    priceable — knowing a market settles on BRTI does not make a CoinGecko
    print a valid input for it. These families became priceable only when the
    feed moved to the settling instrument, and both facts are asserted
    together so a future edit cannot flip the gate while leaving the feed
    behind.
    """
    spec = spec_for(market["ticker"])
    assert spec.settlement_verified is True, "rule confirmed from rules_primary"
    assert spec.source == "kalshi_rti", "and priced off the settling index"
    assert spec.verified is True

    CONFIG.risk.quant_allow_unverified = False
    ok, _ = usable(spec)
    assert ok is True


def test_no_family_is_verified_while_reading_a_proxy_feed():
    """Guards the direction that loses money.

    A spot-fed family must never be marked verified, whatever is known about
    how it settles.
    """
    for spec in CONTRACT_SPECS:
        if spec.verified:
            assert spec.source != "crypto", (
                f"{spec.prefix} is verified while reading CoinGecko spot"
            )


def test_the_caveat_names_the_index_each_family_settles_on():
    """A reader must be able to tell BRTI from ETHUSD_RTI at the spec."""
    assert "BRTI" in spec_for(KXBTC15M["ticker"]).caveat
    assert "BRTI" in spec_for(KXBTCD["ticker"]).caveat
    eth_caveat = spec_for(KXETH["ticker"]).caveat
    assert "ETHUSD_RTI" in eth_caveat and "not " in eth_caveat


def test_the_blackout_survived_the_feed_change():
    """The trap flagged in 2a, now closed.

    _in_settlement_blackout used to key on source == "crypto". Moving these
    families to "kalshi_rti" would have silently disabled the 60-second
    settlement blackout for the exact markets it was written for — the gate
    would have switched itself off during the change it was meant to survive.
    It now keys on the settlement mechanism, which is a property of the
    contract rather than of where we happen to read a number.
    """
    from workers.quant_maker import QuantMaker
    from core.spot_price_client import SpotPriceClient

    quant = QuantMaker(SpotPriceClient())
    for ticker in (KXBTC15M["ticker"], KXBTCD["ticker"], KXETH["ticker"]):
        spec = spec_for(ticker)
        assert spec.source == "kalshi_rti"
        assert quant._in_settlement_blackout(spec, 30.0) is True, (
            f"{spec.prefix} lost its settlement blackout in the feed change"
        )


def test_unconfirmed_families_are_untouched():
    """KXSOL was never queried and may not inherit confidence from the
    families that were.

    KXBTC used to be listed here too. It was confirmed on 2026-08-17 from its
    own rules text — hourly, 60-second BRTI average, fixed strike — so it no
    longer belongs in this list. KXSOL still does.
    """
    for prefix in ("KXSOL",):
        spec = next(s for s in CONTRACT_SPECS if s.prefix == prefix)
        assert spec.settlement_verified is False
        assert spec.verified is False
        assert spec.settlement_index == ""


# --------------------------------------------------------------------------
# liquidity: why only one of these families is commercially relevant
# --------------------------------------------------------------------------

def test_liquidity_survives_only_via_the_fallback_fields():
    """Load-bearing and previously untested.

    All three markets report liquidity_dollars "0.0000". The 15-minute book
    clears the floor solely because _best_liquidity takes the max across the
    fallback fields and picks up volume_fp. Any "cleanup" that made this read
    liquidity_dollars alone would silently drop the entire tradeable crypto
    book, and nothing would fail loudly.
    """
    assert KXBTC15M["liquidity_dollars"] == "0.0000"
    assert _best_liquidity(KXBTC15M) == pytest.approx(1_575_916.05)


def test_only_the_fifteen_minute_family_clears_the_liquidity_floor():
    floor = CONFIG.risk.min_liquidity_usd
    assert _best_liquidity(KXBTC15M) >= floor
    assert _best_liquidity(KXBTCD) < floor, "volume 2.00"
    assert _best_liquidity(KXETH) < floor, "volume 0.00"


def test_close_time_is_the_anchor_the_blackout_needs():
    """Three expiry-ish timestamps; only close_time matches the rules text.

    expiration_time is seven days out. Anchoring the settlement blackout to
    it would put the blackout a week after the market stopped trading.
    """
    from datetime import datetime

    def ts(iso):
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    close = ts(KXBTC15M["close_time"])
    assert ts(KXBTC15M["expiration_time"]) - close == pytest.approx(7 * 86400)
    assert ts(KXBTC15M["expected_expiration_time"]) - close == pytest.approx(300)

    blackout_starts = close - CONFIG.risk.crypto_settlement_blackout_seconds
    assert blackout_starts <= close - 60, (
        "the blackout must fully cover the 60-second settlement window"
    )


# -- verification must not spread by prefix --------------------------------
#
# VERIFIED AGAINST PRODUCTION, 2026-08-17. spec_for was a plain startswith
# over the table in order, so every ether family claimed the one spec that
# had actually been confirmed — against an HOURLY market whose rules text
# names a single hour, 2 AM EDT on one day.


def test_a_yearly_ether_family_does_not_inherit_the_hourly_confirmation():
    """The live bug. Eighteen KXETHY candidates a pass were reaching the quant
    path as verified, held back only by a price-history gate that was minutes
    from clearing."""
    assert spec_for("KXETHY-26DEC31-T5000", "") is None


def test_a_daily_ether_family_does_not_inherit_it_either():
    assert spec_for("KXETHD-26AUG17-T3000", "") is None


def test_the_confirmed_hourly_ether_family_still_matches():
    spec = spec_for("KXETH-26AUG1702-T2594.99", "")

    assert spec is not None
    assert spec.prefix == "KXETH"
    assert spec.verified


def test_other_bitcoin_families_are_refused_as_unmapped():
    """These used to land on the unverified KXBTC catch-all. Now that KXBTC
    is confirmed, a verified spec is claimed only by an exact family match
    (see spec_for), so they match nothing.

    The outcome is unchanged — they were refused then and are refused now —
    and "no contract spec for this ticker family" is the more honest message:
    nobody has read the rules for a yearly or monthly bitcoin market.
    """
    for ticker in ("KXBTCY-26DEC31", "KXBTCMAXMON-26", "KXBTC2026200-26"):
        spec = spec_for(ticker, "")
        assert spec is None, f"{ticker} must not inherit the hourly confirmation"
        assert not usable(spec)[0]


def test_the_hourly_family_is_confirmed_from_its_own_rules():
    """Verified from rules_primary on KXBTC-26AUG1712-T72299.99, not inferred
    from the neighbouring families — which disagree with each other."""
    spec = spec_for("KXBTC-26AUG1712-T72299.99", "")

    assert spec.prefix == "KXBTC"
    assert spec.verified and spec.settlement_verified
    assert spec.settlement_index == "BRTI"
    assert spec.source == "kalshi_rti"
    assert spec.observation == "rti_60s_average"
    assert spec.strike_basis == "fixed"


def test_the_hourly_family_gets_the_settlement_blackout():
    """The blackout keys on observation, so confirming this family switches
    it on — which is the point. Pricing the final minute off a spot read of
    an average that is still forming is exactly what it exists to stop."""
    from workers.quant_maker import QuantMaker

    spec = spec_for("KXBTC-26AUG1712-T72299.99", "")

    assert QuantMaker(spot_client=object())._in_settlement_blackout(spec, 30.0)
    assert not QuantMaker(spot_client=object())._in_settlement_blackout(spec, 3600.0)


def test_bitcoins_confirmed_families_are_unaffected():
    """They escaped the collision by luck of table order; this pins the
    outcome as intended rather than accidental."""
    assert spec_for("KXBTC15M-26AUG1707-B111500", "").prefix == "KXBTC15M"
    assert spec_for("KXBTCD-26AUG17-B100000", "").prefix == "KXBTCD"


def test_no_family_can_reach_a_verified_spec_without_matching_it_exactly():
    """Stated as the invariant, so a future table edit cannot reopen this."""
    from core.contract_specs import CONTRACT_SPECS

    for spec in CONTRACT_SPECS:
        if not spec.verified:
            continue
        impostor = f"{spec.prefix}XX-26AUG17-T1"
        found = spec_for(impostor, "")
        assert found is None or not found.verified, (
            f"{impostor} claimed the verified {spec.prefix} spec"
        )


def test_the_series_ticker_is_matched_on_the_same_rule():
    assert spec_for("SOMETHING-1", series_ticker="KXETHY") is None
    assert spec_for("SOMETHING-1", series_ticker="KXETH").verified


def test_junk_input_maps_to_nothing():
    assert spec_for("", "") is None
    assert spec_for(None, None) is None
