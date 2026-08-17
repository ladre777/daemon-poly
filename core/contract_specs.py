"""
Per-ticker-family contract specifications for the quant path.

P1 item 8 asks that, for every ticker family, the external spot instrument be
verified against the Kalshi settlement definition: strike units, timezone,
observation window and contract semantics. This file is where that
verification is recorded — and, just as importantly, where it is recorded as
*absent*.

The previous mapping was five lines::

    SERIES_SPOT_MAP = {
        "KXBTC": ("btc", "crypto"),
        "KXGOLD": ("gold", "etf"),
        ...
    }

That asserts "the CoinGecko BTC/USD spot price settles this contract" with
nothing behind it. Several ways that goes wrong quietly:

- **Settlement source.** Kalshi's BTC contracts settle against a specific
  index at a specific time, not against whatever CoinGecko last printed. If
  the index is a TWAP over the final minutes, a point-in-time spot quote is
  the wrong input for the last stretch of the contract's life.
- **Units.** A gold contract may be struck on the GLD ETF price (~$250) or on
  spot gold per troy ounce (~$2,600). Feeding an ETF quote to a per-ounce
  strike does not error, it just prices every contract at ~0 or ~1.
- **Timezone and observation window.** "Highest price today" depends entirely
  on whose midnight and which exchange session.

So each family carries an explicit ``verified`` flag. Unverified families are
refused by the quant path unless ``QUANT_ALLOW_UNVERIFIED=true``, which exists
for demo experimentation and is off by default. Refusing is not a
conservatism tax: an unverified mapping produces confident-looking
probabilities from the wrong number, which is worse than no probability.

Crypto settlement, confirmed against the live API 2026-08-17
------------------------------------------------------------
Three families were pulled from ``GET /markets`` and their ``rules_primary``
text read directly. The help-page account below was right about the
mechanism and incomplete about everything else:

===========  ==========  ==========================  ============  ==========
family       index       strike basis                comparison    liquidity
===========  ==========  ==========================  ============  ==========
KXBTC15M     BRTI        opening 60s BRTI average    >=            1,575,916
KXBTCD       BRTI        fixed level                 >             2.00
KXETH        ETHUSD_RTI  fixed level                 >             0.00
===========  ==========  ==========================  ============  ==========

All three settle on a 60-second simple average of a CF Benchmarks Real-Time
Index. None carries the settlement value on the market record while open —
``expiration_value`` is ``""`` on all three — which refutes the claim that
15-minute markets settle from a value on the record.

Three things no secondary source described, each of which changes an
implementation:

1. **The index is per asset.** Bitcoin settles on BRTI, ether on ETHUSD_RTI
   (written "ERTI" in the rules prose). One shared "RTI" feed would price
   ether off the bitcoin index.
2. **KXBTC15M's strike is itself a 60-second average**, the one ending at
   the market's open, so both legs of the comparison are windowed averages
   on quarter-hour boundaries.
3. **The comparison operator differs** — ``greater_or_equal`` on KXBTC15M,
   ``greater`` on the other two.

Only KXBTC15M has any liquidity. The other two are filtered by
MIN_LIQUIDITY_USD before pricing is attempted, so they are confirmed but
commercially irrelevant today.

Prior inference, retained for context
-------------------------------------
The caution above turned out to be justified, and the specific guess in the
first bullet was right. Kalshi's crypto contracts settle on the **CF
Benchmarks Real-Time Index, averaged over the final 60 seconds before the
window closes** — not on a spot-price snapshot, and not on CoinGecko at all
(Kalshi Help Center, "Crypto Markets"). The RTI itself aggregates order data
across major exchanges once per second.

Two consequences, both encoded below:

1. ``KXBTCD`` previously recorded ``observation="point_in_time"``. That was
   wrong. It is a 60-second average, and the distinction matters most exactly
   where a point-in-time quote is least representative.
2. Spot pricing is least reliable inside that averaging window, so the quant
   path stops pricing crypto ``CRYPTO_SETTLEMENT_BLACKOUT_SECONDS`` before
   close regardless of the ``verified`` flag.

Kalshi relays the index on the authenticated ``cfbenchmarks_value``
WebSocket channel: ``avg_60s_data.value`` is the trailing 60-second average
(the current state, used for pricing) and ``last_60s_windowed_average_15min``
is the 60-second average ending at a quarter-hour boundary, published only in
the final minute before it. For KXBTC15M that windowed value is the settling
quantity itself — and, taken at the market's open, its strike. See
core/rti_client.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import CONFIG


@dataclass(frozen=True)
class ContractSpec:
    """What a ticker family settles on, and whether that has been checked."""

    #: Ticker prefix this applies to, matched case-insensitively.
    prefix: str
    #: Symbol passed to SpotPriceClient.
    symbol: str
    #: Which feed prices this family:
    #:   "kalshi_rti" — the CF Benchmarks index Kalshi relays, i.e. the
    #:                  instrument that actually settles the contract
    #:   "crypto"     — CoinGecko spot (a proxy; correct for nothing that
    #:                  settles on an index)
    #:   "etf"        — Yahoo chart endpoint
    source: str
    #: Units the STRIKE is denominated in, and the units our feed returns.
    #: A mismatch here is the failure that silently prices everything at 0 or 1.
    strike_units: str
    feed_units: str
    #: How Kalshi says the market settles, in prose. Written down so the next
    #: person can check it rather than re-deriving intent from code.
    settlement_definition: str
    #: Timezone the observation window is defined in.
    timezone: str
    #: Observation window semantics: "point_in_time", "daily_high",
    #: "daily_close", "twap", "rti_60s_average", or "unknown".
    observation: str
    #: Whether the quant path may PRICE this family with the feed configured
    #: in ``source``. This is the gate: usable() refuses the family unless
    #: this is True or QUANT_ALLOW_UNVERIFIED is set.
    #:
    #: Note carefully what this does NOT mean. It is not "we understand how
    #: this settles" — that is ``settlement_verified`` below. A family can
    #: have a fully confirmed settlement rule and still belong at False here,
    #: because knowing that a market settles on BRTI does not make a
    #: CoinGecko spot print a valid input for it. Setting this True while
    #: ``source`` still points at the wrong instrument is precisely the
    #: failure the flag exists to prevent.
    verified: bool = False
    #: Whether the SETTLEMENT RULE has been confirmed against Kalshi's own
    #: `rules_primary`/`rules_secondary` text from the live API — as opposed
    #: to inferred from a help page, a blog, or a ticker name.
    #:
    #: Deliberately separate from ``verified`` so that confirming how a
    #: market settles can be recorded as progress without silently unlocking
    #: pricing against a feed that has not caught up.
    settlement_verified: bool = False
    #: CF Benchmarks index identifier the family settles against, where one
    #: applies: "BRTI" (bitcoin) or "ETHUSD_RTI" (ether). Empty when the
    #: family does not settle on an index. Kalshi's rules text writes the
    #: ether index informally as "ERTI"; the API index_id is ETHUSD_RTI and
    #: they are the same index.
    settlement_index: str = ""
    #: How the strike is defined, which is NOT the same question as how
    #: settlement is measured:
    #:   "fixed_level"          — a static price written into the ticker
    #:   "opening_60s_average"  — another 60s index average, taken at open
    #:   "" — not applicable or unconfirmed
    strike_basis: str = ""
    #: What specifically still needs checking.
    caveat: str = ""

    @property
    def units_match(self) -> bool:
        return self.strike_units == self.feed_units


#: Ordered most-specific-prefix-first so KXBTC15M and KXBTCD match before
#: the generic KXBTC.
CONTRACT_SPECS: tuple[ContractSpec, ...] = (
    ContractSpec(
        prefix="KXBTC15M",
        symbol="btc",
        # Still "crypto" (CoinGecko) because that is what the feed actually
        # is today. Changing this string without changing the feed would be
        # a lie in the one place the code trusts. It also drives the
        # settlement blackout in quant_maker._in_settlement_blackout, which
        # keys on source == "crypto" — see the caveat.
        source="kalshi_rti",
        strike_units="USD per BTC",
        feed_units="USD per BTC",
        settlement_definition=(
            "15-minute bitcoin up/down market. CONFIRMED against the live "
            "API on 2026-08-17 from rules_primary on "
            "KXBTC15M-26AUG170100-00, quoted verbatim: 'If the simple "
            "average of the sixty seconds of CF Benchmarks' BRTI before "
            "1:00 AM EDT on Aug 17, 2026 is at least the simple average of "
            "the sixty seconds of CF Benchmarks' BRTI before 12:45 AM EDT "
            "on August 17, 2026, then the market resolves to Yes.' "
            "rules_secondary adds that the final value is 'rounded to the "
            "nearest 2 decimal places' and warns explicitly that a spot "
            "source such as Google or Coinbase is NOT what settles it."
        ),
        timezone="US/Eastern",
        observation="rti_60s_average",
        verified=True,
        settlement_verified=True,
        settlement_index="BRTI",
        # The part neither the help page nor the secondary source described:
        # the strike is not a price level, it is ANOTHER 60-second BRTI
        # average — the one ending at the market's own open. Both legs
        # therefore land on quarter-hour boundaries (04:45Z and 05:00Z on
        # the observed market), which is what makes the exchange's
        # `last_60s_windowed_average_15min` field the exact settling
        # quantity for both sides of the comparison.
        strike_basis="opening_60s_average",
        caveat=(
            "Priced off BRTI as relayed by Kalshi's authenticated "
            "cfbenchmarks_value channel, which is the settling instrument. "
            "The strike needs no feed: floor_strike IS the opening 60-second "
            "BRTI average, already computed and published by the exchange, "
            "so pricing reduces to P(terminal 60s BRTI average >= "
            "floor_strike). Two residual approximations, both bounded and "
            "tested: the terminal quantity is a 60-second time-average, so "
            "the effective diffusion horizon is shortened by 2w/3 rather "
            "than treating it as a point sample; and the comparison is "
            "greater_or_equal, not greater. If the RTI feed is cold or "
            "stale the quant path declines — it never falls back to spot."
        ),
    ),
    ContractSpec(
        prefix="KXBTCD",
        symbol="btc",
        source="kalshi_rti",
        strike_units="USD per BTC",
        feed_units="USD per BTC",
        settlement_definition=(
            "Daily bitcoin price market. CONFIRMED against the live API on "
            "2026-08-17 from rules_primary on KXBTCD-26AUG1717-T72749.99, "
            "quoted verbatim: 'If the simple average of the sixty seconds of "
            "CF Benchmarks' Bitcoin Real-Time Index (BRTI) before 5 PM EDT "
            "is above 72749.99 at 5 PM EDT on Aug 17, 2026, then the market "
            "resolves to Yes.' NOT a CoinGecko spot print, and NOT an "
            "instantaneous value."
        ),
        timezone="US/Eastern",
        # Corrected 2026-08-15 from "point_in_time" by inference from the
        # help page; that inference was confirmed verbatim by the exchange's
        # own rules text on 2026-08-17.
        observation="rti_60s_average",
        verified=True,
        settlement_verified=True,
        settlement_index="BRTI",
        # Unlike KXBTC15M, the strike here is a static level written into the
        # ticker (T72749.99), compared with `greater`, not `greater_or_equal`.
        strike_basis="fixed_level",
        caveat=(
            "Priced off BRTI via Kalshi's cfbenchmarks_value relay. Strike "
            "is the static level in the ticker, compared with `greater`. "
            "Commercially irrelevant today: every KXBTCD market observed had "
            "volume_fp 2.00 and no resting bid, so MIN_LIQUIDITY_USD filters "
            "it long before pricing is reached."
        ),
    ),
    ContractSpec(
        prefix="KXBTC",
        symbol="btc",
        source="crypto",
        strike_units="USD per BTC",
        feed_units="USD per BTC",
        settlement_definition=(
            "Catch-all for bitcoin families that are not KXBTC15M or KXBTCD. "
            "Those two are confirmed to settle on a 60-second BRTI average, "
            "so this one probably does too — but 'probably' is what this "
            "field exists to refuse, and the two confirmed families already "
            "differ from each other in strike basis and comparison operator."
        ),
        timezone="US/Eastern",
        observation="unknown",
        verified=False,
        settlement_verified=False,
        caveat=(
            "No rules text pulled for any ticker that lands here. Do not "
            "infer from KXBTC15M or KXBTCD — they disagree with each other "
            "on strike_basis (opening average vs fixed level) and on "
            "strike_type (greater_or_equal vs greater)."
        ),
    ),
    ContractSpec(
        prefix="KXETH",
        symbol="eth",
        source="kalshi_rti",
        strike_units="USD per ETH",
        feed_units="USD per ETH",
        settlement_definition=(
            "Hourly ether price market. CONFIRMED against the live API on "
            "2026-08-17 from rules_primary on KXETH-26AUG1702-T2594.99, "
            "quoted verbatim: 'If the simple average of the sixty seconds of "
            "CF Benchmarks' Ethereum Real-Time Index (ERTI) before 2 AM EDT "
            "is above 2594.99 at 2 AM EDT on Aug 17, 2026, then the market "
            "resolves to Yes.'"
        ),
        timezone="US/Eastern",
        observation="rti_60s_average",
        verified=True,
        settlement_verified=True,
        # The rules text says "ERTI"; that is informal shorthand. The index
        # identifier the API subscribes by is ETHUSD_RTI, and they are the
        # same index. Recorded under the API name because that is the one a
        # feed implementation has to send.
        settlement_index="ETHUSD_RTI",
        strike_basis="fixed_level",
        caveat=(
            "Settles on a DIFFERENT index from bitcoin — ETHUSD_RTI, not "
            "BRTI — so a feed keyed only on 'the RTI' would silently price "
            "ether off the bitcoin index. core/rti_client.INDEX_FOR_SYMBOL "
            "maps this per symbol and returns None for anything unmapped, "
            "so an unconfirmed asset gets no quote rather than bitcoin's. "
            "Every KXETH market observed is untraded (volume_fp 0.00), so it "
            "is filtered by MIN_LIQUIDITY_USD before pricing is reached."
        ),
    ),
    ContractSpec(
        prefix="KXSOL",
        symbol="sol",
        source="crypto",
        strike_units="USD per SOL",
        feed_units="USD per SOL",
        settlement_definition="Solana price threshold market.",
        timezone="US/Eastern",
        observation="unknown",
        verified=False,
        caveat="Observation window not confirmed.",
    ),
    ContractSpec(
        prefix="KXGOLD",
        symbol="gold",
        source="etf",
        # The unit mismatch that motivates this whole file. Left explicit and
        # unequal so units_match is False and the spec cannot be used until
        # someone resolves it.
        strike_units="USD per troy ounce",
        feed_units="USD per GLD share",
        settlement_definition=(
            "Gold price market. Kalshi's own app shows a GLD badge, but "
            "whether the STRIKE is quoted in GLD share price (~$250) or spot "
            "gold per troy ounce (~$2,600) is unconfirmed."
        ),
        timezone="US/Eastern",
        observation="unknown",
        verified=False,
        caveat=(
            "UNIT MISMATCH: a per-ounce strike against a per-share feed is a "
            "~10x error that prices every contract at ~0 or ~1 without "
            "raising anything. Must be resolved before this family trades."
        ),
    ),
    ContractSpec(
        prefix="KXSILVER",
        symbol="silver",
        source="etf",
        strike_units="USD per troy ounce",
        feed_units="USD per SLV share",
        settlement_definition="Silver price market; same ambiguity as gold.",
        timezone="US/Eastern",
        observation="unknown",
        verified=False,
        caveat="UNIT MISMATCH: see KXGOLD.",
    ),
)


def spec_for(ticker: str, series_ticker: str = "") -> Optional[ContractSpec]:
    """Find the contract spec for a ticker, or None if this family is unmapped.

    A **verified** spec is claimed only by an exact family match. An
    unverified one may still absorb related families by prefix, because all
    it can do to them is refuse them.

    VERIFIED AGAINST PRODUCTION, 2026-08-17. This was a plain ``startswith``
    over the table in order, and the ether families collided::

        KXETHY-26DEC31-T5000  -> KXETH  verified=True   <-- yearly
        KXETHD-26AUG17        -> KXETH  verified=True   <-- daily
        KXETH-26AUG1702-T...  -> KXETH  verified=True   <-- the one confirmed

    ``KXETH`` was confirmed against an *hourly* market — the rules text
    quoted in its own spec names a single hour, 2 AM EDT on one day. A yearly
    contract inheriting that confirmation is exactly what ``verified`` exists
    to prevent, and it was live: eighteen KXETHY candidates a pass reached
    the quant path as verified, held back only by a price-history gate that
    was minutes from clearing. Pricing a year-dated option with a
    60-second-average settlement model and a diffusion horizon of months is
    not a smaller edge, it is a wrong number.

    Bitcoin escaped this by luck of table order rather than by design: KXBTCY
    misses KXBTC15M and KXBTCD, then lands on the unverified KXBTC catch-all
    and is refused. The rule below makes that the intended outcome rather
    than an accident.

    An ether family with no exact spec now returns None — "no contract spec
    for this ticker family" — instead of borrowing the hourly one. There is
    no unverified KXETH catch-all for it to land on, and the refusal is the
    same either way.
    """
    for candidate in (series_ticker or "", ticker or ""):
        upper = (candidate or "").upper()
        if not upper:
            continue
        family = upper.split("-", 1)[0]
        for spec in CONTRACT_SPECS:
            if family == spec.prefix:
                return spec
        for spec in CONTRACT_SPECS:
            # Prefix fallback, unverified specs only. A catch-all may group
            # relatives it has never confirmed; a confirmation may not spread
            # to relatives it never covered.
            if not spec.verified and upper.startswith(spec.prefix):
                return spec
    return None


def usable(spec: Optional[ContractSpec]) -> tuple[bool, str]:
    """Whether the quant path may price this family, and why not if not."""
    if spec is None:
        return False, "no contract spec for this ticker family"
    if not spec.units_match:
        return False, (
            f"strike units ({spec.strike_units}) do not match feed units "
            f"({spec.feed_units}) — {spec.caveat}"
        )
    if not spec.verified and not CONFIG.risk.quant_allow_unverified:
        return False, (
            f"contract spec for {spec.prefix} is unverified against Kalshi's "
            f"settlement rules ({spec.caveat or 'no detail recorded'}). Set "
            f"QUANT_ALLOW_UNVERIFIED=true to price it anyway on demo."
        )
    return True, ""
