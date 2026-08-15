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

Crypto settlement, confirmed 2026-08-15
---------------------------------------
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

Kalshi's own API appears to expose the index directly, via an authenticated
``cfbenchmarks_value`` WebSocket channel carrying the trailing 60-second
average and — in the final minute before a quarter-hour close — the windowed
average that *is* the settlement input. Verifying that (and whether it exists
on demo) is the prerequisite for ever setting ``verified=True`` on a crypto
family; pricing these off CoinGecko spot cannot get there.
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
    #: "crypto" (CoinGecko) or "etf" (Yahoo chart endpoint).
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
    #: True only when a human has checked this against Kalshi's own rules
    #: page. Nothing in this repo has been, so every entry ships False.
    verified: bool = False
    #: What specifically still needs checking.
    caveat: str = ""

    @property
    def units_match(self) -> bool:
        return self.strike_units == self.feed_units


#: Ordered most-specific-prefix-first so KXBTCD matches before KXBTC.
CONTRACT_SPECS: tuple[ContractSpec, ...] = (
    ContractSpec(
        prefix="KXBTCD",
        symbol="btc",
        source="crypto",
        strike_units="USD per BTC",
        feed_units="USD per BTC",
        settlement_definition=(
            "Daily bitcoin price market. Settles on the CF Benchmarks "
            "Bitcoin Real-Time Index (BRTI), averaged over the final 60 "
            "seconds before the window closes — NOT a CoinGecko spot print, "
            "and NOT an instantaneous value."
        ),
        timezone="US/Eastern",
        # Corrected 2026-08-15. This said "point_in_time", which was wrong:
        # the settlement value is a 60-second mean of a once-per-second index.
        observation="rti_60s_average",
        verified=False,
        caveat=(
            "Settlement mechanism now confirmed, but the feed is still "
            "CoinGecko spot, which is not the settling instrument. Verifying "
            "this family means sourcing the BRTI itself — see the module "
            "docstring on Kalshi's cfbenchmarks_value channel — not "
            "re-checking the strike units."
        ),
    ),
    ContractSpec(
        prefix="KXBTC",
        symbol="btc",
        source="crypto",
        strike_units="USD per BTC",
        feed_units="USD per BTC",
        settlement_definition="Bitcoin price threshold market.",
        timezone="US/Eastern",
        observation="unknown",
        verified=False,
        caveat="Observation window not confirmed against Kalshi's rules page.",
    ),
    ContractSpec(
        prefix="KXETH",
        symbol="eth",
        source="crypto",
        strike_units="USD per ETH",
        feed_units="USD per ETH",
        settlement_definition="Ether price threshold market.",
        timezone="US/Eastern",
        observation="unknown",
        verified=False,
        caveat="Observation window not confirmed.",
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
    """Find the contract spec for a ticker, or None if this family is unmapped."""
    for candidate in (series_ticker or "", ticker or ""):
        upper = candidate.upper()
        for spec in CONTRACT_SPECS:
            if upper.startswith(spec.prefix):
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
